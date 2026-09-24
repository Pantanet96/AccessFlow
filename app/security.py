"""Password hashing helpers (bcrypt via passlib) and the password policy."""
from passlib.context import CryptContext

from app.i18n import N_

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


PASSWORD_MIN = 12
PASSWORD_MAX_BYTES = 72  # bcrypt silently ignores everything past this

# Only entries of PASSWORD_MIN+ chars matter: shorter ones fail on length.
# ponytail: a handful of the usual suspects, not a breach corpus; add a real
# list (or a k-anonymity lookup) if accounts other than the superadmin get
# local passwords.
_COMMON = frozenset("""
123456789012 1234567890123 12345678901234 123123123123 111111111111
qwertyuiopas qwerty123456 1q2w3e4r5t6y 1qaz2wsx3edc qwertyuiop12
password1234 password12345 passwordpassword password123! administrator
administrator1 iloveyou1234 welcome12345 letmein12345 changeme1234
abcdefghijkl abc123abc123 plexplexplex accessflow12 accessflow123
""".split())


def password_problem(password: str, username: str | None = "") -> str | None:
    """Why `password` fails the policy (untranslated msgid), or None if it passes."""
    if len(password) < PASSWORD_MIN:
        return N_("The password must be at least 12 characters.")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return N_("The password must be at most 72 bytes.")
    low = password.lower()
    name = (username or "").strip().lower()
    if len(name) >= 3 and name in low:
        return N_("The password must not contain the username.")
    if low in _COMMON or len(set(low)) <= 3:
        return N_("This password is too common.")
    return None


def safe_next_url(next_url: str, default: str) -> str:
    r"""Keep a ?next= / form-supplied redirect on this site.

    Browsers strip tab/CR/LF out of URLs before resolving them, so "/\tevil.com"
    turns into "//evil.com" -- drop those characters first, then accept only a
    single-slash absolute path ("//host" and "/\host" are both host-relative).
    """
    url = next_url.strip().translate({9: None, 10: None, 13: None})
    if url.startswith("/") and not url.startswith(("//", "/\\")):
        return url
    return default
