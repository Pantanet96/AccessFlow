"""TOTP second factor (RFC 6238) for local logins, plus one-time recovery codes.

State lives in the encrypted settings store under `mfa:<user_id>` (and the
not-yet-confirmed secret under `mfa_pending:<user_id>`): same Fernet key and
same key rotation as every other secret, no schema change.
"""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

from sqlmodel import Session

from app.services import settings_store

STEP = 30             # seconds per code
DIGITS = 6
RECOVERY_CODES = 10


def _key(user_id: int) -> str:
    return f"mfa:{user_id}"


def _pending_key(user_id: int) -> str:
    return f"mfa_pending:{user_id}"


# ---- TOTP ----

def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _code(secret: str, counter: int) -> str:
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10 ** DIGITS:0{DIGITS}d}"


def match_step(secret: str, code: str, after: int = -1, now: float | None = None) -> int | None:
    """Time step `code` is valid for (one step of clock drift either way), or
    None. Steps <= `after` are refused: a code works once, even inside its 30s."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // STEP)
    for step in (current - 1, current, current + 1):
        if step > after and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str, issuer: str = "AccessFlow") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"


def qr_svg(uri: str) -> str:
    import segno

    # Black on white whatever the theme: authenticator apps read that best.
    return segno.make(uri, error="m").svg_inline(scale=5, dark="#000", light="#fff")


# ---- recovery codes ----

def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").encode()).hexdigest()


def _new_recovery_codes() -> list[str]:
    # 48 random bits each: far beyond online guessing, and the login throttle
    # applies to them as to TOTP codes.
    return ["-".join(secrets.token_hex(2) for _ in range(3)) for _ in range(RECOVERY_CODES)]


# ---- per-user state ----

def _load(session: Session, user_id: int) -> dict | None:
    raw = settings_store.get_value(session, _key(user_id))
    return json.loads(raw) if raw else None


def enabled(session: Session, user_id: int) -> bool:
    return _load(session, user_id) is not None


def start_setup(session: Session, user_id: int) -> str:
    """Secret for the enrolment page, kept server-side until confirmed."""
    secret = new_secret()
    settings_store.set_value(session, _pending_key(user_id), secret)
    return secret


def pending_secret(session: Session, user_id: int) -> str | None:
    return settings_store.get_value(session, _pending_key(user_id))


def enable(session: Session, user_id: int, code: str) -> list[str] | None:
    """Confirm the pending secret with a code from the app. Returns the
    recovery codes (shown once, only their hashes are kept) or None."""
    secret = pending_secret(session, user_id)
    step = match_step(secret, code) if secret else None
    if step is None:
        return None
    codes = _new_recovery_codes()
    settings_store.set_value(session, _key(user_id), json.dumps({
        "secret": secret, "last_step": step, "recovery": [_hash(c) for c in codes],
    }))
    settings_store.delete_value(session, _pending_key(user_id))
    return codes


def disable(session: Session, user_id: int) -> None:
    settings_store.delete_value(session, _key(user_id))
    settings_store.delete_value(session, _pending_key(user_id))


def verify(session: Session, user_id: int, code: str) -> str | None:
    """Check a login code: "totp", "recovery" (that code is now spent) or None."""
    state = _load(session, user_id)
    if state is None:
        return None
    step = match_step(state["secret"], code, after=state.get("last_step", -1))
    if step is not None:
        state["last_step"] = step
        kind = "totp"
    elif (h := _hash(code)) in state["recovery"]:
        state["recovery"].remove(h)
        kind = "recovery"
    else:
        return None
    settings_store.set_value(session, _key(user_id), json.dumps(state))
    return kind


def recovery_left(session: Session, user_id: int) -> int:
    state = _load(session, user_id)
    return len(state["recovery"]) if state else 0
