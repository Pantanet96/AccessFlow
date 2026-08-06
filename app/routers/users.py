"""User administration routes (list, manager, role, soft-delete)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from app.auth.deps import require_capability, require_role, require_user
from app.db import get_session
from app.i18n import gettext as _
from app.models import AppUser, Plan, Role, Subscription, SubscriptionStatus, utcnow
from app.permissions import Capability
from app.config import get_settings
from app.services import access_service, audit, plex_import, plex_service, users as users_svc
from app.services.plex_service import PlexNotConnected
from app.templating import templates

router = APIRouter()


def _next_redirect(next_url: str, default: str = "/users") -> RedirectResponse:
    is_safe = (
        next_url.startswith("/")
        and not next_url.startswith("//")
        and not next_url.startswith("/\\")
    )
    safe = next_url if is_safe else default
    return RedirectResponse(safe, status_code=303)


def _render_list(request, viewer, session, error=None, message=None,
                 stale=None, status_code=200):
    user_rows = users_svc.list_users_for(session, viewer)
    sub_info, plans_in_use = _subscription_info(session, [u.id for u in user_rows])
    candidates = (
        users_svc.manager_candidates(session)
        if viewer.role in (Role.superadmin, Role.admin)
        else []
    )
    managers_by_id = {c.id: c.real_name for c in users_svc.manager_candidates(session)}
    is_admin = viewer.role in (Role.superadmin, Role.admin)
    needs_sub = (
        plex_import.users_without_active_subscription(session) if is_admin else []
    )
    # Warn-only: libraries added/removed on Plex vs what the app shares.
    lib_drift = access_service.library_drift(session) if is_admin else {}
    return templates.TemplateResponse(
        request,
        "users/list.html",
        {
            "current_user": viewer,
            "users": user_rows,
            "candidates": candidates,
            "managers_by_id": managers_by_id,
            "roles": [r for r in Role if r != Role.superadmin],
            "can_manage_roles": viewer.role == Role.superadmin,
            "can_assign_manager": is_admin,
            "can_delete": is_admin,
            "can_import": is_admin,
            "can_rename": viewer.role != Role.user,
            "needs_sub": needs_sub,
            "lib_drift": lib_drift,
            "sub_info": sub_info,
            "plans_in_use": plans_in_use,
            "stale": stale or [],
            "error": error,
            "message": message,
        },
        status_code=status_code,
    )


def _subscription_info(session, user_ids):
    """Map user_id -> {plan, expiry, cat} for the list's Plan/Expiry columns.
    cat is one of: unlimited, expired, soon (<=7d), later. Prefers the active
    sub over an expired one if both somehow exist."""
    if not user_ids:
        return {}, []
    subs = session.exec(
        select(Subscription)
        .where(Subscription.user_id.in_(user_ids))
        .where(Subscription.status.in_(
            (SubscriptionStatus.active, SubscriptionStatus.expired)))
    ).all()
    plan_ids = {s.plan_id for s in subs}
    plan_map = {
        p.id: p for p in
        session.exec(select(Plan).where(Plan.id.in_(plan_ids))).all()
    } if plan_ids else {}
    today = utcnow().date()
    info, plans_in_use = {}, set()
    for s in subs:
        is_active = s.status == SubscriptionStatus.active
        prev = info.get(s.user_id)
        if prev and prev["_active"] and not is_active:
            continue
        if s.expiry_at is None:
            cat = "unlimited"
        else:
            dleft = (s.expiry_at.date() - today).days
            cat = "expired" if dleft < 0 else ("soon" if dleft <= 7 else "later")
        plan = plan_map.get(s.plan_id)
        name = plan.name if plan else "—"
        info[s.user_id] = {
            "plan": name, "expiry": s.expiry_at, "cat": cat, "_active": is_active,
        }
        if plan:
            plans_in_use.add(name)
    return info, sorted(plans_in_use)


@router.get("/users", response_class=HTMLResponse)
def users_list(
    request: Request,
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    if viewer.role == Role.user:
        return RedirectResponse("/profile", status_code=303)
    return _render_list(request, viewer, session)


@router.post("/users/import-plex", response_class=HTMLResponse)
def import_plex_users(
    request: Request,
    actor: AppUser = Depends(require_capability(Capability.invite_user)),
    session: Session = Depends(get_session),
):
    try:
        result = plex_import.import_plex_users(session)
    except PlexNotConnected:
        return _render_list(
            request, actor, session, error=_("Plex is not connected."), status_code=400
        )
    audit.record(session, actor.id, "import_plex_users", detail=result)
    return _render_list(
        request,
        actor,
        session,
        message=_("Imported %(c)d user(s), skipped %(s)d existing.")
        % {"c": result["created"], "s": result["skipped"]},
        stale=result.get("stale"),
    )


@router.post("/users/resync-libraries", response_class=HTMLResponse)
def resync_libraries(
    request: Request,
    actor: AppUser = Depends(require_capability(Capability.invite_user)),
    session: Session = Depends(get_session),
):
    """Re-apply each managed user's configured libraries to Plex now (manual
    counterpart of the daily job). Never auto-shares newly added libraries."""
    try:
        plex_service.list_sections(force=True)  # refresh before diffing/applying
        result = access_service.resync_libraries(session)
    except PlexNotConnected:
        return _render_list(
            request, actor, session, error=_("Plex is not connected."), status_code=400
        )
    audit.record(session, actor.id, "resync_libraries", detail=result)
    return _render_list(
        request,
        actor,
        session,
        message=_(
            "Libraries synced: %(u)d updated, %(s)d unchanged, %(p)d stale refs removed."
        )
        % {"u": result["updated"], "s": result["skipped"], "p": result["pruned"]},
    )


@router.post("/users/{user_id}/manager")
def set_manager(
    user_id: int,
    manager_id: str = Form(""),
    next: str = Form("/users"),
    actor: AppUser = Depends(require_role(Role.superadmin, Role.admin)),
    session: Session = Depends(get_session),
):
    target = session.get(AppUser, user_id)
    if target is None or not users_svc.can_manage_user(actor, target):
        return _next_redirect(next)
    mid = int(manager_id) if manager_id else None
    if mid is not None:
        valid_ids = {c.id for c in users_svc.manager_candidates(session)}
        if mid not in valid_ids or mid == target.id:
            mid = target.manager_id  # ignore invalid choice
    users_svc.assign_manager(session, target, mid)
    audit.record(
        session, actor.id, "assign_manager", "app_user", user_id, {"manager_id": mid}
    )
    return _next_redirect(next)


@router.post("/users/{user_id}/name")
def set_name(
    user_id: int,
    real_name: str = Form(""),
    next: str = Form("/users"),
    actor: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    if actor.role == Role.user:
        return _next_redirect(next)
    target = session.get(AppUser, user_id)
    # Same hierarchy as every other mutation: act only on someone strictly below
    # you (moderators scoped to their own users) — never a peer or the superadmin.
    if target is None or not users_svc.can_manage_user(actor, target):
        return _next_redirect(next)
    users_svc.rename(session, target, real_name)
    audit.record(
        session, actor.id, "rename_user", "app_user", user_id,
        {"name": target.real_name},
    )
    return _next_redirect(next)


@router.post("/users/{user_id}/role")
def set_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    next: str = Form("/users"),
    actor: AppUser = Depends(require_capability(Capability.manage_roles)),
    session: Session = Depends(get_session),
):
    target = session.get(AppUser, user_id)
    # Cannot change the role of yourself, a peer, or a superior (hierarchy);
    # cannot touch a superadmin nor promote anyone to superadmin.
    if (
        target is None
        or target.role == Role.superadmin
        or not users_svc.can_manage_user(actor, target)
    ):
        return _next_redirect(next)
    try:
        new_role = Role(role)
    except ValueError:
        return _next_redirect(next)
    # No promoting at or above the actor's own rank (e.g. an admin minting an
    # admin); superadmin can never be granted via this route.
    from app.permissions import outranks

    if new_role == Role.superadmin or not outranks(actor.role, new_role):
        return _next_redirect(next)
    users_svc.change_role(session, target, new_role)
    audit.record(
        session, actor.id, "change_role", "app_user", user_id, {"role": new_role.value}
    )
    return _next_redirect(next)


@router.post("/users/{user_id}/delete")
def delete_user(
    request: Request,
    user_id: int,
    next: str = Form("/users"),
    actor: AppUser = Depends(require_capability(Capability.delete_user)),
    session: Session = Depends(get_session),
):
    target = session.get(AppUser, user_id)
    # Hierarchy: delete only someone strictly below you. Never the superadmin,
    # never yourself, never a peer (e.g. an admin deleting another admin).
    if (
        target is None
        or target.role == Role.superadmin
        or target.id == actor.id
        or not users_svc.can_manage_user(actor, target)
    ):
        return _next_redirect(next)
    try:
        users_svc.soft_delete(session, target)
    except users_svc.OrphanError:
        return _render_list(
            request,
            actor,
            session,
            error=_(
                "Cannot delete a manager with assigned users. Reassign them first."
            ),
            status_code=400,
        )
    if get_settings().plex_revoke_on_delete and target.plex_email:
        try:
            plex_service.remove_friend(target.plex_email)
        except Exception:  # noqa: BLE001 - revocation is best-effort
            pass
    # Always revoke Overseerr access on removal (best-effort, no-op if off).
    access_service.remove_from_overseerr(target)
    audit.record(session, actor.id, "delete_user", "app_user", user_id)
    return _next_redirect(next)
