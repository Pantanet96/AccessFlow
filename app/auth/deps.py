"""FastAPI auth dependencies: current user, login + capability guards."""
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

from app.auth.session import COOKIE_NAME, read_token
from app.db import get_session
from app.models import AppUser, Role
from app.permissions import Capability, has_capability


def get_current_user(
    request: Request, session: Session = Depends(get_session)
) -> AppUser | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    parsed = read_token(token)
    if parsed is None:
        return None
    uid, gen = parsed
    user = session.get(AppUser, uid)
    if user is None or not user.is_active:
        return None
    # Reject cookies issued before the last logout / password change.
    if (getattr(user, "session_gen", 0) or 0) != gen:
        return None
    return user


def require_user(user: AppUser | None = Depends(get_current_user)) -> AppUser:
    if user is None:
        # 303 -> browser redirects to login; body ignored.
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def require_capability(cap: Capability):
    def _dep(user: AppUser = Depends(require_user)) -> AppUser:
        if not has_capability(user, cap):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return user

    return _dep


def require_role(*roles: Role):
    def _dep(user: AppUser = Depends(require_user)) -> AppUser:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return user

    return _dep
