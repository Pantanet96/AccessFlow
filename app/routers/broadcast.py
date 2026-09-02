"""Manual broadcast, Telegram + email (admin + superadmin)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app.auth.deps import require_role, require_user
from app.db import get_session
from app.security import safe_next_url
from app.models import AppUser, Role
from app.services import audit
from app.services import broadcast as broadcast_svc
from app.templating import templates

router = APIRouter()


@router.get("/broadcast", response_class=HTMLResponse)
def broadcast_form(
    request: Request,
    viewer: AppUser = Depends(require_role(Role.superadmin, Role.admin)),
):
    return templates.TemplateResponse(
        request, "broadcast.html", {"current_user": viewer, "sent": None, "roles": list(Role)}
    )


@router.post("/broadcast", response_class=HTMLResponse)
def broadcast_send(
    request: Request,
    message: str = Form(...),
    only_role: str = Form(""),
    viewer: AppUser = Depends(require_role(Role.superadmin, Role.admin)),
    session: Session = Depends(get_session),
):
    role = Role(only_role) if only_role else None
    count = broadcast_svc.broadcast(session, message, only_role=role)
    audit.record(
        session, viewer.id, "broadcast",
        detail={"recipients": count, "only_role": only_role or None},
    )
    return templates.TemplateResponse(
        request,
        "broadcast.html",
        {"current_user": viewer, "sent": count, "roles": list(Role)},
    )


@router.post("/broadcast/dismiss")
def broadcast_dismiss(
    id: int = Form(...),
    next: str = Form("/"),
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    viewer.dismissed_broadcast_id = id
    session.add(viewer)
    session.commit()
    safe_next = safe_next_url(next, "/")
    return RedirectResponse(safe_next, status_code=303)
