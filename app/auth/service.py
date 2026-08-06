"""Authentication services (local username/password)."""
from sqlmodel import Session, select

from app.models import AppUser
from app.security import dummy_verify, verify_password


def authenticate_local(
    session: Session, username: str, password: str
) -> AppUser | None:
    user = session.exec(
        select(AppUser).where(AppUser.username == username)
    ).first()
    if user is None or not user.password_hash or not user.is_active:
        # Burn an equivalent bcrypt verify so an unknown/passwordless username
        # isn't distinguishable from a wrong password by response timing.
        dummy_verify()
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
