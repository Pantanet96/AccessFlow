"""SuperAdmin plan management routes."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app.auth.deps import require_role
from app.db import get_session
from app.i18n import gettext as _
from app.models import AppUser, Plan, Role
from app.services import audit, plex_service
from app.services import plans as plans_svc
from app.templating import templates

router = APIRouter()


def _euros_to_cents(value: str) -> int:
    try:
        return int(round(float(value.replace(",", ".")) * 100))
    except (ValueError, AttributeError):
        return 0


def _int_or_none(value: str) -> int | None:
    return int(value) if value and value.isdigit() and int(value) > 0 else None


def _render(request, viewer, session, error=None):
    rows = plans_svc.list_all(session)
    in_use = {p.id: plans_svc.is_in_use(session, p.id) for p in rows}
    import json as _json

    plan_libs = {
        p.id: (_json.loads(p.libraries) if p.libraries else []) for p in rows
    }
    return templates.TemplateResponse(
        request,
        "plans.html",
        {
            "current_user": viewer,
            "plans": rows,
            "in_use": in_use,
            "plan_libs": plan_libs,
            "sections": plex_service.list_sections_safe(),
            "error": error,
        },
    )


@router.get("/plans", response_class=HTMLResponse)
def plans_page(
    request: Request,
    viewer: AppUser = Depends(require_role(Role.superadmin)),
    session: Session = Depends(get_session),
):
    return _render(request, viewer, session)


@router.post("/plans")
def create_plan(
    name: str = Form(...),
    plan_type: str = Form("paid"),
    price: str = Form("0"),
    duration_months: str = Form(""),
    duration_days: str = Form(""),
    libraries: list[str] = Form(default=[]),
    viewer: AppUser = Depends(require_role(Role.superadmin)),
    session: Session = Depends(get_session),
):
    plans_svc.create_plan(
        session,
        name=name,
        plan_type=plan_type,
        price_cents=_euros_to_cents(price),
        duration_months=_int_or_none(duration_months),
        duration_days=_int_or_none(duration_days),
        libraries=libraries or None,
    )
    audit.record(session, viewer.id, "create_plan", detail={"name": name, "type": plan_type})
    return RedirectResponse("/plans", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/plans/{plan_id}/edit")
def edit_plan(
    plan_id: int,
    name: str = Form(...),
    price: str = Form("0"),
    duration_months: str = Form(""),
    duration_days: str = Form(""),
    active: str = Form("false"),
    libraries: list[str] = Form(default=[]),
    viewer: AppUser = Depends(require_role(Role.superadmin)),
    session: Session = Depends(get_session),
):
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404)
    plans_svc.update_plan(
        session,
        plan,
        name=name,
        price_cents=_euros_to_cents(price),
        duration_months=_int_or_none(duration_months),
        duration_days=_int_or_none(duration_days),
        active=(active == "on"),
        libraries=libraries or None,
    )
    audit.record(session, viewer.id, "edit_plan", "plan", plan_id)
    return RedirectResponse("/plans", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/plans/{plan_id}/delete")
def delete_plan(
    request: Request,
    plan_id: int,
    viewer: AppUser = Depends(require_role(Role.superadmin)),
    session: Session = Depends(get_session),
):
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404)
    try:
        plans_svc.delete_plan(session, plan)
    except plans_svc.PlanInUse:
        return _render(
            request,
            viewer,
            session,
            error=_("Cannot delete a plan in use. Deactivate it instead."),
        )
    audit.record(session, viewer.id, "delete_plan", "plan", plan_id)
    return RedirectResponse("/plans", status_code=status.HTTP_303_SEE_OTHER)
