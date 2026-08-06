"""Password hashing helpers (bcrypt via passlib)."""
from passlib.context import CryptContext

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _pwd.verify(password, password_hash)


# A valid bcrypt hash of a throwaway value, computed once. Verifying against it
# burns the same ~bcrypt time as a real check, so a missing/passwordless account
# can't be told apart from a wrong password by response timing. See dummy_verify.
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-login")


def dummy_verify() -> None:
    """Run a throwaway verify to equalize timing when there is no real hash to
    check (unknown username / Plex-only account). Result intentionally ignored."""
    try:
        _pwd.verify("x", _DUMMY_HASH)
    except Exception:  # noqa: BLE001 - timing side-effect only; never raise
        pass
