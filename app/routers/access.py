"""Access lifecycle actions: suspend / reactivate / remove-from-plex / grace / libraries."""
from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.auth.deps import require_role, require_user
from app.db import get_session
from app.models import AppUser, Role
from app.services import access_service, audit
from app.services import users as users_svc

router = APIRouter()


def _redirect(user_id: int) -> RedirectResponse:
    return RedirectResponse(
        f"/users/{user_id}/subscription", status_code=status.HTTP_303_SEE_OTHER
    )


def _managed_target(session: Session, viewer: AppUser, user_id: int) -> AppUser:
    target = session.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    if not users_svc.can_manage_user(viewer, target):
        raise HTTPException(status_code=403)
    return target


@router.post("/users/{user_id}/suspend")
def suspend(
    user_id: int,
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    target = _managed_target(session, viewer, user_id)
    access_service.suspend(session, target)
    audit.record(session, viewer.id, "suspend_access", "app_user", user_id)
    return _redirect(user_id)


@router.post("/users/{user_id}/reactivate")
def reactivate(
    user_id: int,
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    target = _managed_target(session, viewer, user_id)
    access_service.reactivate(session, target)
    audit.record(session, viewer.id, "reactivate_access", "app_user", user_id)
    return _redirect(user_id)


@router.post("/users/{user_id}/grace")
def set_grace(
    user_id: int,
    grace_days: str = Form("0"),
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    target = _managed_target(session, viewer, user_id)
    days = int(grace_days) if grace_days.isdigit() else 0
    target.grace_days = max(0, min(15, days))
    session.add(target)
    session.commit()
    audit.record(
        session, viewer.id, "set_grace", "app_user", user_id, {"grace_days": target.grace_days}
    )
    return _redirect(user_id)


@router.post("/users/{user_id}/remove-plex")
def remove_plex(
    user_id: int,
    viewer: AppUser = Depends(require_role(Role.superadmin, Role.admin)),
    session: Session = Depends(get_session),
):
    target = _managed_target(session, viewer, user_id)
    access_service.remove_from_plex(session, target)
    audit.record(session, viewer.id, "remove_from_plex", "app_user", user_id)
    return _redirect(user_id)


@router.post("/users/{user_id}/libraries")
def set_libraries(
    user_id: int,
    libraries: list[str] = Form(default=[]),
    viewer: AppUser = Depends(require_role(Role.superadmin, Role.admin)),
    session: Session = Depends(get_session),
):
    target = _managed_target(session, viewer, user_id)
    access_service.apply_libraries(session, target, libraries)
    audit.record(session, viewer.id, "set_libraries", "app_user", user_id)
    return _redirect(user_id)
