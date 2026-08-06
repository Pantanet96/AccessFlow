"""Audit trail + sent-notifications history.

Audit tab: SuperAdmin/Admin only. Notifications tab: also moderators, scoped to
their own users.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.auth.deps import require_role
from app.db import get_session
from app.models import AppUser, NotificationType, Role
from app.services import audit
from app.templating import templates

router = APIRouter()


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    tab: str = "activity",
    user: str | None = None,
    type: str | None = None,
    status: str | None = None,
    viewer: AppUser = Depends(
        require_role(Role.superadmin, Role.admin, Role.moderator)
    ),
    session: Session = Depends(get_session),
):
    is_moderator = viewer.role == Role.moderator
    # Moderators only ever see notifications (the audit tab is admin-only), and only
    # for their own users.
    if is_moderator:
        tab = "notifications"
    for_manager_id = viewer.id if is_moderator else None

    ctx = {"current_user": viewer, "tab": tab, "is_moderator": is_moderator}

    if tab == "notifications":
        ntype = None
        if type:
            try:
                ntype = NotificationType(type)
            except ValueError:
                ntype = None  # unknown value -> ignore the type filter
        user_id = None
        if user:
            try:
                user_id = int(user)
            except ValueError:
                user_id = None  # unknown value -> ignore the recipient filter
        ctx.update({
            "notifications": audit.list_notifications(
                session, for_manager_id=for_manager_id,
                user_id=user_id, ntype=ntype, status=status,
            ),
            "recipients": audit.notification_recipients(session, for_manager_id),
            "ntypes": audit.notification_type_options(),
            "f_user": user_id,
            "f_type": type or "",
            "f_status": status or "",
        })
    else:
        ctx["entries"] = audit.list_recent(session, limit=300)

    return templates.TemplateResponse(request, "audit.html", ctx)
