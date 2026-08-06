"""Subscription detail + plan change + two-step renewal routes."""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from app import runtime_config
from app.auth.deps import require_capability, require_user
from app.db import get_session
from app.models import AppUser, Plan, Renewal, RenewalStatus, Role, Subscription
from app.permissions import Capability, has_capability
from app.services import audit, notifications
from app.services import subscriptions as sub_svc
from app.services import users as users_svc
from app.templating import templates

router = APIRouter()


def _load_target(session: Session, viewer: AppUser, user_id: int) -> AppUser:
    target = session.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    # Anyone may VIEW their own subscription page (managing it is a separate
    # check — can_manage_user — enforced by the mutating routes below).
    if viewer.id == target.id:
        return target
    if viewer.role == Role.user:
        raise HTTPException(status_code=403)
    if not users_svc.can_manage_user(viewer, target):
        raise HTTPException(status_code=403)
    return target


@router.get("/users/{user_id}/subscription", response_class=HTMLResponse)
def subscription_detail(
    request: Request,
    user_id: int,
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    target = _load_target(session, viewer, user_id)
    sub = sub_svc.get_active_subscription(session, target.id)
    plan = session.get(Plan, sub.plan_id) if sub else None
    renewals = sub_svc.list_renewals(session, sub.id) if sub else []
    # Acting on the sub (plan/renew/remind/access) requires hierarchy rights;
    # nobody manages their own plan (except the superadmin owner).
    can_manage = (
        viewer.role != Role.user and users_svc.can_manage_user(viewer, target)
    )
    can_renew = (
        can_manage
        and has_capability(viewer, Capability.renew_subscription)
        and sub is not None
        and plan is not None
        and plan.is_paid
        and not plan.is_trial
        and not plan.is_unlimited
    )
    # Reminder is meaningful for any sub with an expiry the viewer can act on
    # (broader than can_renew, which excludes trial/unlimited).
    can_remind = (
        can_manage
        and has_capability(viewer, Capability.renew_subscription)
        and sub is not None
        and sub.expiry_at is not None
    )
    reminder_sent = request.query_params.get("sent")
    can_change_paid = can_manage and has_capability(viewer, Capability.change_plan_paid)
    can_change_any = can_manage and has_capability(viewer, Capability.change_plan_any)
    plans = sub_svc.list_plans(session) if (can_change_paid or can_change_any) else []
    is_admin = viewer.role in (Role.superadmin, Role.admin)
    can_rename = viewer.role != Role.user and can_manage
    can_manage_roles = viewer.role == Role.superadmin and target.role != Role.superadmin
    can_assign_manager = is_admin and target.role == Role.user
    can_delete = (
        is_admin and target.id != viewer.id
        and target.role != Role.superadmin and target.is_active
        and users_svc.can_manage_user(viewer, target)
    )
    manager_candidates = (
        users_svc.manager_candidates(session) if can_assign_manager else []
    )
    roles = [r for r in Role if r != Role.superadmin]
    sections = []
    user_libraries = []
    if is_admin:
        from app.services import access_service, plex_service

        sections = plex_service.list_sections_safe()
        user_libraries = access_service.libraries_for(session, target)
    return templates.TemplateResponse(
        request,
        "subscriptions/detail.html",
        {
            "current_user": viewer,
            "target": target,
            "sub": sub,
            "plan": plan,
            "renewals": renewals,
            "plans": plans,
            "can_renew": can_renew,
            "can_remind": can_remind,
            "reminder_sent": reminder_sent,
            "can_change_paid": can_change_paid,
            "can_change_any": can_change_any,
            "can_manage": can_manage,
            "is_admin": is_admin,
            "can_rename": can_rename,
            "can_manage_roles": can_manage_roles,
            "can_assign_manager": can_assign_manager,
            "can_delete": can_delete,
            "manager_candidates": manager_candidates,
            "roles": roles,
            "sections": sections,
            "user_libraries": user_libraries,
            "plan_by_id": {p.id: p for p in sub_svc.list_plans(session, only_active=False)},
        },
    )


@router.post("/users/{user_id}/subscription/plan")
def change_or_create_plan(
    user_id: int,
    plan_slug: str = Form(...),
    trial_days: str = Form(""),
    start_date: str = Form(""),
    periods: int = Form(1),
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    target = _load_target(session, viewer, user_id)
    # Hierarchy + self rule: you may set a plan only for someone strictly below
    # you (managers only for their own users); nobody sets their own plan.
    if not users_svc.can_manage_user(viewer, target):
        raise HTTPException(status_code=403)

    plan = sub_svc.get_plan_by_slug(session, plan_slug)
    if plan is None:
        raise HTTPException(status_code=404)

    # Paid plan -> change_plan_paid (moderator OK); free/trial/F&F -> change_plan_any.
    needed = Capability.change_plan_paid if plan.is_paid else Capability.change_plan_any
    if not has_capability(viewer, needed):
        raise HTTPException(status_code=403)

    days = int(trial_days) if (plan.is_trial and trial_days.isdigit()) else None
    periods = max(1, periods)  # paid first-setup: pay N periods up-front in one go
    # Manual start: first assignment, or moving Trial/F&F -> paid, or fixing a
    # wrong start (e.g. users already on the server at first setup).
    start = None
    if start_date:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            start = None
    existing = sub_svc.get_active_subscription(session, target.id)
    if existing is None:
        new_sub = sub_svc.create_subscription(
            session, target, plan, trial_days=days, start=start, periods=periods
        )
        # First paid setup is an up-front payment: log it as a paid renewal so it
        # shows in earnings/history (no-op for trial/F&F). Without this the
        # initial payment was invisible in the reports page.
        sub_svc.record_setup_payment(
            session, new_sub, plan, actor_id=viewer.id,
            collected_by=target.manager_id, periods=periods,
        )
        action = "create_subscription"
    else:
        sub_svc.change_plan(
            session, existing, plan, trial_days=days, start=start, periods=periods,
            actor_id=viewer.id, collected_by=target.manager_id,
        )
        action = "change_plan"
    audit.record(
        session, viewer.id, action, "app_user", user_id, {"plan": plan.slug}
    )
    # Plan changed -> re-assert Overseerr permissions (trial = view-only, no
    # requests; paid/F&F = full). Best-effort, no-op when Overseerr is off.
    from app.services import access_service

    access_service.sync_overseerr_permissions(session, target)
    return RedirectResponse(
        f"/users/{user_id}/subscription", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/users/{user_id}/subscription/expiry")
def set_expiry(
    user_id: int,
    expiry_date: str = Form(...),
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Admin/superadmin manual expiry override (corrections / exceptions).
    Sets the date only; access is enforced by the daily job and the
    suspend/reactivate buttons."""
    if viewer.role not in (Role.superadmin, Role.admin):
        raise HTTPException(status_code=403)
    target = _load_target(session, viewer, user_id)
    if not users_svc.can_manage_user(viewer, target):
        raise HTTPException(status_code=403)
    sub = sub_svc.get_active_subscription(session, target.id)
    if sub is None:
        raise HTTPException(status_code=404)
    try:
        sub.expiry_at = datetime.strptime(expiry_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")
    session.add(sub)
    session.commit()
    audit.record(
        session, viewer.id, "set_expiry", "subscription", sub.id,
        {"expiry": expiry_date},
    )
    return RedirectResponse(
        f"/users/{user_id}/subscription", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/requests", response_class=HTMLResponse)
def renewal_requests(
    request: Request,
    viewer: AppUser = Depends(require_capability(Capability.renew_subscription)),
    session: Session = Depends(get_session),
):
    pendings = session.exec(
        select(Renewal)
        .where(Renewal.status == "pending")
        .order_by(Renewal.created_at)
    ).all()
    rows = []
    for r in pendings:
        sub = session.get(Subscription, r.subscription_id)
        if sub is None:
            continue
        user = session.get(AppUser, sub.user_id)
        if user is None:
            continue
        if viewer.role == Role.moderator and user.manager_id != viewer.id:
            continue
        plan = session.get(Plan, r.plan_id)
        rows.append({"renewal": r, "user": user, "plan": plan})
    # "To collect" side: paid subs expiring within the digest window that don't yet
    # have a pending renewal (mutually exclusive with the pending rows above).
    lookahead = runtime_config.digest_lookahead()
    manager_id = None if viewer.role in (Role.superadmin, Role.admin) else viewer.id
    collectables = notifications.collectables(
        session, manager_id=manager_id, lookahead=lookahead
    )
    total_cents = sum(c["amount_cents"] for c in collectables)
    return templates.TemplateResponse(
        request, "requests.html", {
            "current_user": viewer,
            "rows": rows,
            "collectables": collectables,
            "collectables_total": f"{total_cents / 100:.2f}",
            "lookahead": lookahead,
        }
    )


@router.post("/subscriptions/{sub_id}/request-renewal")
def request_renewal(
    sub_id: int,
    causale: str = Form(""),
    periods: int = Form(1),
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    sub = session.get(Subscription, sub_id)
    if sub is None or sub.user_id != viewer.id:
        raise HTTPException(status_code=403)
    if sub_svc.has_pending_renewal(session, sub.id):
        return RedirectResponse("/", status_code=303)
    try:
        renewal = sub_svc.create_renewal(
            session, sub, actor_id=viewer.id, collected_by=viewer.manager_id,
            periods=periods,
        )
    except ValueError:
        return RedirectResponse("/", status_code=303)
    if causale.strip():
        renewal.causale = causale.strip()
        session.add(renewal)
        session.commit()
    audit.record(session, viewer.id, "request_renewal", "subscription", sub_id)
    return RedirectResponse("/", status_code=303)


@router.post("/renewals/{renewal_id}/delete")
def delete_renewal(
    renewal_id: int,
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    renewal = session.get(Renewal, renewal_id)
    if renewal is None:
        raise HTTPException(status_code=404)
    if renewal.status != RenewalStatus.pending:
        raise HTTPException(status_code=400)  # only pending requests are deletable
    sub = session.get(Subscription, renewal.subscription_id)
    if sub is None:
        raise HTTPException(status_code=404)
    target = session.get(AppUser, sub.user_id)
    is_owner = viewer.id == sub.user_id
    is_manager = (
        has_capability(viewer, Capability.renew_subscription)
        and target is not None
        and users_svc.can_manage_user(viewer, target)
    )
    if not (is_owner or is_manager):
        raise HTTPException(status_code=403)
    session.delete(renewal)
    session.commit()
    audit.record(session, viewer.id, "delete_renewal", "renewal", renewal_id)
    dest = "/" if viewer.role == Role.user else "/requests"
    return RedirectResponse(dest, status_code=status.HTTP_303_SEE_OTHER)


def _load_sub_for_renew(
    session: Session, viewer: AppUser, sub: Subscription | None
) -> AppUser:
    if sub is None:
        raise HTTPException(status_code=404)
    target = session.get(AppUser, sub.user_id)
    if target is None:
        raise HTTPException(status_code=404)
    if not has_capability(viewer, Capability.renew_subscription):
        raise HTTPException(status_code=403)
    if not users_svc.can_manage_user(viewer, target):
        raise HTTPException(status_code=403)
    return target


@router.post("/subscriptions/{sub_id}/renew")
def create_renewal(
    sub_id: int,
    periods: int = Form(1),
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    sub = session.get(Subscription, sub_id)
    target = _load_sub_for_renew(session, viewer, sub)
    try:
        sub_svc.create_renewal(
            session, sub, actor_id=viewer.id, collected_by=target.manager_id,
            periods=periods,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record(
        session, viewer.id, "create_renewal", "subscription", sub_id,
        {"periods": max(1, periods)},
    )
    return RedirectResponse(
        f"/users/{sub.user_id}/subscription", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/subscriptions/{sub_id}/remind")
def send_reminder_now(
    sub_id: int,
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    sub = session.get(Subscription, sub_id)
    _load_sub_for_renew(session, viewer, sub)  # reuse renew/collect 404/403 perms
    sent = notifications.notify_expiry_manual(session, sub)
    audit.record(
        session, viewer.id, "send_reminder", "subscription", sub_id, {"sent": sent}
    )
    return RedirectResponse(
        f"/users/{sub.user_id}/subscription?sent={sent}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/renewals/{renewal_id}/pay")
def pay_renewal(
    renewal_id: int,
    causale: str = Form(...),
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    renewal = session.get(Renewal, renewal_id)
    if renewal is None:
        raise HTTPException(status_code=404)
    sub = session.get(Subscription, renewal.subscription_id)
    _load_sub_for_renew(session, viewer, sub)
    try:
        sub_svc.mark_renewal_paid(session, renewal, causale=causale)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Payment received -> ensure access is (re)granted on Plex + Overseerr.
    target = session.get(AppUser, sub.user_id)
    if target is not None:
        from app.services import access_service

        access_service.reactivate(session, target)
    audit.record(
        session, viewer.id, "pay_renewal", "renewal", renewal_id, {"causale": causale}
    )
    return RedirectResponse(
        f"/users/{sub.user_id}/subscription", status_code=status.HTTP_303_SEE_OTHER
    )
