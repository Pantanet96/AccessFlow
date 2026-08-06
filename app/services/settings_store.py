"""Encrypted key/value runtime settings (Fernet, key derived from secret)."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session, select

from app.config import get_settings
from app.models import AppSetting


def _fernet_from(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


# After a key rotation the on-disk rows are re-encrypted with the NEW secret, but
# this running process still derives the OLD one from settings until it restarts.
# Without this override, get_value would fail to decrypt (secrets read as unset)
# and any set_value would write with the old key -> a row unreadable post-restart.
# rotate_secret sets this so the process uses the new key immediately; a fresh
# process starts with None and reads the (now updated) APP_SECRET_KEY normally.
_active_secret_override: str | None = None

# Bumped on every set_value/delete_value so callers (runtime_config's cache)
# can tell whether their cached read is still fresh without re-hitting the DB.
_generation: int = 0


def generation() -> int:
    return _generation


def invalidate() -> None:
    global _generation
    _generation += 1


def _active_secret() -> str:
    return _active_secret_override or get_settings().app_secret_key


def _fernet() -> Fernet:
    return _fernet_from(_active_secret())


def get_value(session: Session, key: str) -> str | None:
    row = session.get(AppSetting, key)
    if row is None:
        return None
    try:
        return _fernet().decrypt(row.value.encode()).decode()
    except InvalidToken:
        return None


def set_value(session: Session, key: str, value: str | None) -> None:
    if value is None or value == "":
        delete_value(session, key)
        return
    enc = _fernet().encrypt(value.encode()).decode()
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=enc)
    else:
        row.value = enc
    session.add(row)
    session.commit()
    invalidate()


def delete_value(session: Session, key: str) -> None:
    row = session.get(AppSetting, key)
    if row is not None:
        session.delete(row)
        session.commit()
        invalidate()


def rotate_secret(session: Session, new_secret: str) -> int:
    """Re-encrypt every stored value from the current key to one derived from
    new_secret. Returns the number of rows rotated.

    Decrypt-all-first, then write: if any row can't be read with the current
    key the whole rotation aborts (raises InvalidToken) so we never half-rotate
    and strand values. Rows that are already dead under the current key would
    raise here too — but get_value already treats those as gone, so a healthy
    store rotates cleanly. After this runs, set APP_SECRET_KEY=new_secret and
    restart: the running process still derives the OLD key until then.
    """
    global _active_secret_override
    cur_f = _fernet()
    new_f = _fernet_from(new_secret)
    rows = list(session.exec(select(AppSetting)))
    plains = [(row, cur_f.decrypt(row.value.encode())) for row in rows]
    for row, plain in plains:
        row.value = new_f.encrypt(plain).decode()
        session.add(row)
    session.commit()
    # Switch the running process to the new key so reads/writes match on-disk
    # state immediately (no corruption window until the operator restarts).
    _active_secret_override = new_secret
    return len(plains)
