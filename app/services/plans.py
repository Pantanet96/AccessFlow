"""SuperAdmin plan management (paid / trial / family&friends)."""
import json
import re

from sqlmodel import Session, select

from app.models import Plan, Renewal, Subscription
from app.services.subscriptions import TRIAL_MAX_DAYS

PLAN_TYPES = ("paid", "trial", "family_friends")


class PlanInUse(Exception):
    """Raised when deleting a plan still referenced by subscriptions/renewals."""


def _apply_type(plan: Plan, plan_type: str, price_cents, months, days) -> None:
    if plan_type == "trial":
        plan.is_trial = True
        plan.is_unlimited = False
        plan.is_paid = False
        plan.price_cents = 0
        plan.duration_months = None
        plan.duration_days = min(days or 0, TRIAL_MAX_DAYS) or None
    elif plan_type == "family_friends":
        plan.is_trial = False
        plan.is_unlimited = True
        plan.is_paid = False
        plan.price_cents = 0
        plan.duration_months = None
        plan.duration_days = None
    else:  # paid
        plan.is_trial = False
        plan.is_unlimited = False
        plan.is_paid = True
        plan.price_cents = max(0, price_cents or 0)
        plan.duration_months = months or None
        plan.duration_days = days or None


def _libs_json(libraries) -> str | None:
    return json.dumps(libraries) if libraries else None


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "plan"


def _unique_slug(session: Session, base: str, exclude_id: int | None = None) -> str:
    slug = base
    i = 2
    while True:
        existing = session.exec(select(Plan).where(Plan.slug == slug)).first()
        if existing is None or existing.id == exclude_id:
            return slug
        slug = f"{base}_{i}"
        i += 1


def list_all(session: Session) -> list[Plan]:
    return list(session.exec(select(Plan).order_by(Plan.price_cents)).all())


def is_in_use(session: Session, plan_id: int) -> bool:
    if session.exec(
        select(Subscription).where(Subscription.plan_id == plan_id)
    ).first():
        return True
    return (
        session.exec(select(Renewal).where(Renewal.plan_id == plan_id)).first()
        is not None
    )


def create_plan(
    session: Session,
    *,
    name: str,
    plan_type: str,
    price_cents: int = 0,
    duration_months: int | None = None,
    duration_days: int | None = None,
    libraries: list[str] | None = None,
) -> Plan:
    if plan_type not in PLAN_TYPES:
        plan_type = "paid"
    plan = Plan(
        name=name.strip(),
        slug=_unique_slug(session, slugify(name)),
        active=True,
        libraries=_libs_json(libraries),
    )
    _apply_type(plan, plan_type, price_cents, duration_months, duration_days)
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan


def _current_type(plan: Plan) -> str:
    if plan.is_trial:
        return "trial"
    if plan.is_unlimited:
        return "family_friends"
    return "paid"


def update_plan(
    session: Session,
    plan: Plan,
    *,
    name: str,
    price_cents: int,
    duration_months: int | None,
    duration_days: int | None,
    active: bool,
    libraries: list[str] | None = None,
) -> Plan:
    if name.strip():
        plan.name = name.strip()
    plan.active = active
    plan.libraries = _libs_json(libraries)
    # Type is fixed after creation; re-apply type-specific fields.
    _apply_type(plan, _current_type(plan), price_cents, duration_months, duration_days)
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan


def delete_plan(session: Session, plan: Plan) -> None:
    if is_in_use(session, plan.id):
        raise PlanInUse()
    session.delete(plan)
    session.commit()
