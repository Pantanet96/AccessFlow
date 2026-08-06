"""Link a Telegram chat to an AppUser via a compact signed deep-link token.

Telegram's `?start=` deep-link parameter only accepts [A-Za-z0-9_-] and max 64
chars. An itsdangerous token is too long and contains '.' separators, so
Telegram silently drops it and the user lands on the bot with no token. We pack
(uid, timestamp) + an HMAC into a ~24-char base64url string instead.
"""
import base64
import hmac
import struct
import time
from hashlib import sha256

from sqlmodel import Session, select

from app.config import get_settings
from app.models import AppUser

LINK_SALT = b"tg-link"
LINK_MAX_AGE = 3600  # 1 hour
_SIG_LEN = 10        # truncated HMAC bytes


def _secret() -> bytes:
    return get_settings().app_secret_key.encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def _sig(msg: bytes, bind: str) -> bytes:
    # `bind` ties the token to the user's telegram_id AT MINT TIME. A successful
    # link changes telegram_id, so the same token no longer verifies afterwards:
    # this makes the token effectively single-use and non-replayable (e.g. a
    # leaked/forwarded welcome-email link can't be redeemed a second time).
    payload = LINK_SALT + msg + bind.encode()
    return hmac.new(_secret(), payload, sha256).digest()[:_SIG_LEN]


def make_link_token(user_id: int, bind: str = "") -> str:
    msg = struct.pack(">II", user_id, int(time.time()))  # 8 bytes
    return _b64e(msg + _sig(msg, bind))  # 18 bytes -> 24 chars, no '.', Telegram-safe


def read_link_token(token: str, bind: str = "", max_age: int = LINK_MAX_AGE) -> dict | None:
    try:
        raw = _b64d(token)
    except (ValueError, TypeError):
        return None
    if len(raw) != 8 + _SIG_LEN:
        return None
    msg, sig = raw[:8], raw[8:]
    if not hmac.compare_digest(sig, _sig(msg, bind)):
        return None
    user_id, ts = struct.unpack(">II", msg)
    if time.time() - ts > max_age:
        return None
    return {"uid": user_id}


def _peek_uid(token: str) -> int | None:
    """Extract the (unverified) user id so we can load the user and learn the
    `bind` value needed to verify the signature. The HMAC is checked afterwards."""
    try:
        raw = _b64d(token)
    except (ValueError, TypeError):
        return None
    if len(raw) != 8 + _SIG_LEN:
        return None
    return struct.unpack(">II", raw[:8])[0]


def link_telegram(session: Session, token: str, chat_id) -> AppUser | None:
    uid = _peek_uid(token)
    if uid is None:
        return None
    user = session.get(AppUser, uid)
    if user is None:
        return None
    # Verify with the user's CURRENT telegram_id as the binding: matches only if
    # unchanged since the token was minted -> single-use per link.
    if read_link_token(token, bind=user.telegram_id or "") is None:
        return None
    chat = str(chat_id)
    if user.telegram_id == chat:
        return user  # already linked to this chat -> idempotent success
    # Never let one Telegram chat control two accounts (no DB unique constraint:
    # legacy rows may already collide). Reject if another user holds this chat.
    other = session.exec(
        select(AppUser).where(AppUser.telegram_id == chat, AppUser.id != user.id)
    ).first()
    if other is not None:
        return None
    user.telegram_id = chat
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
