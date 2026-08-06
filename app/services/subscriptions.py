"""Core subscription + two-step renewal engine.

Rules:
- Expiry on renewal: new_expiry = max(today, current_expiry) + plan_duration.
- F&F (is_unlimited): expiry is None (never expires); not renewable.
- Trial: expiry = start + trial_days (<= TRIAL_MAX_DAYS); not renewable.
- Changing a plan never grants paid time: F&F clears expiry; switching away
  from unlimited sets expiry to now (a renewal then adds time); paid->paid
  keeps the current expiry (new price/duration apply at the next renewal).
"""
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta
from sqlmodel import Session, select

from app.models import (
    AppUser,
    Plan,
    Renewal,
    RenewalStatus,
    Subscription,
    SubscriptionStatus,
    utcnow,
)

TRIAL_MAX_DAYS = 30
MAX_PERIODS = 60  # matches the UI cap; guards against absurd values (e.g. 1e9)
                  # that would overflow relativedelta -> 500 on "confirm paid".


def compute_expiry(
    base: datetime,
    plan: Plan,
    trial_days: int | None = None,
    periods: int = 1,
) -> datetime | None:
    if plan.is_unlimited:
        return None
    if plan.is_trial:
        # trial length: explicit override, else the plan's own configured days
        # (periods doesn't apply to trials — they're not renewable)
        days = min(trial_days or plan.duration_days or 0, TRIAL_MAX_DAYS)
        return base + timedelta(days=days)
    periods = max(1, min(MAX_PERIODS, periods))
    delta = relativedelta()
    if plan.duration_months:
        delta += relativedelta(months=plan.duration_months * periods)
    if plan.duration_days:
        delta += relativedelta(days=plan.duration_days * periods)
    return base + delta


def get_active_subscription(
    session: Session, user_id: int
) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.created_at.desc())
    )
    return session.exec(stmt).first()


def get_current_subscription(
    session: Session, user_id: int
) -> Subscription | None:
    """Latest subscription that is still active OR already expired (overdue).

    run_expiry_scan flips a sub to `expired` the first day past its expiry date,
    but such a user still holds Plex/Overseerr access and is still owed action.
    Enforcement (auto-suspend) and collect surfaces (digest/worklist/reports) must
    keep seeing them — the active-only `get_active_subscription` drops them, so
    reconcile would never suspend and digests would omit the most overdue user."""
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(
            Subscription.status.in_(
                (SubscriptionStatus.active, SubscriptionStatus.expired)
            )
        )
        .order_by(Subscription.created_at.desc())
    )
    return session.exec(stmt).first()


def create_subscription(
    session: Session,
    user: AppUser,
    plan: Plan,
    *,
    trial_days: int | None = None,
    start: datetime | None = None,
    periods: int = 1,
) -> Subscription:
    start = start or utcnow()
    expiry = compute_expiry(start, plan, trial_days, periods=periods)

    existing = get_active_subscription(session, user.id)
    if existing is not None:
        existing.status = SubscriptionStatus.cancelled
        session.add(existing)

    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        start_at=start,
        expiry_at=expiry,
        status=SubscriptionStatus.active,
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return sub


def record_setup_payment(
    session: Session,
    sub: Subscription,
    plan: Plan,
    *,
    actor_id: int | None,
    collected_by: int | None,
    periods: int = 1,
    causale: str = "Initial setup",
    paid_at: datetime | None = None,
) -> Renewal | None:
    """Log the up-front payment of a first paid-plan setup as a *paid* renewal so
    the money shows in earnings and the renewals history. Does NOT touch expiry
    (create_subscription already set it). No-op for trial/unlimited/non-paid."""
    if not plan.is_paid or plan.is_trial or plan.is_unlimited:
        return None
    periods = max(1, min(MAX_PERIODS, periods))
    when = paid_at or sub.start_at or utcnow()
    renewal = Renewal(
        subscription_id=sub.id,
        plan_id=plan.id,
        periods=periods,
        amount_cents=plan.price_cents * periods,
        status=RenewalStatus.paid,
        due_at=sub.start_at or when,
        paid_at=when,
        causale=causale,
        created_by=actor_id,
        collected_by=collected_by,
    )
    session.add(renewal)
    session.commit()
    session.refresh(renewal)
    return renewal


def change_plan(
    session: Session,
    sub: Subscription,
    new_plan: Plan,
    *,
    trial_days: int | None = None,
    start: datetime | None = None,
    periods: int = 1,
    actor_id: int | None = None,
    collected_by: int | None = None,
) -> Subscription:
    old_plan = session.get(Plan, sub.plan_id)
    from_non_paid = old_plan is None or old_plan.is_unlimited or old_plan.is_trial
    sub.plan_id = new_plan.id
    upfront_charge = False
    if new_plan.is_unlimited:
        sub.expiry_at = None
    elif new_plan.is_trial:
        base = start or utcnow()
        sub.expiry_at = compute_expiry(base, new_plan, trial_days)
        if start:
            sub.start_at = start
    elif start is not None and from_non_paid:
        # Manual start when moving Trial/F&F -> paid (import an already-paying
        # user, or fix a wrong start). Grants `periods` periods from that date,
        # like a first assignment. paid -> paid never honors start (keeps expiry).
        sub.start_at = start
        sub.expiry_at = compute_expiry(start, new_plan, periods=periods)
        upfront_charge = True
    elif sub.expiry_at is None:
        # Leaving unlimited for a paid plan, no manual start: term starts now,
        # renew to add time.
        sub.expiry_at = utcnow()
    # paid -> paid (or no eligible manual start): keep current expiry untouched.
    sub.status = SubscriptionStatus.active
    session.add(sub)
    session.commit()
    session.refresh(sub)
    if upfront_charge:
        # Same up-front payment as a first paid-setup (record_setup_payment) —
        # without this, money collected right at the upgrade never appears in
        # earnings/reports (no Renewal row is ever created for this path).
        record_setup_payment(
            session, sub, new_plan, actor_id=actor_id, collected_by=collected_by,
            periods=periods, causale="Plan upgrade", paid_at=start,
        )
    return sub


def create_renewal(
    session: Session,
    sub: Subscription,
    *,
    actor_id: int | None,
    collected_by: int | None,
    due_at: datetime | None = None,
    periods: int = 1,
) -> Renewal:
    plan = session.get(Plan, sub.plan_id)
    if plan is None or not plan.is_paid or plan.is_trial or plan.is_unlimited:
        raise ValueError("Only paid, non-trial plans can be renewed")
    periods = max(1, min(MAX_PERIODS, periods))
    renewal = Renewal(
        subscription_id=sub.id,
        plan_id=plan.id,
        periods=periods,
        amount_cents=plan.price_cents * periods,
        status=RenewalStatus.pending,
        due_at=due_at or sub.expiry_at or utcnow(),
        created_by=actor_id,
        collected_by=collected_by,
    )
    session.add(renewal)
    session.commit()
    session.refresh(renewal)
    return renewal


def mark_renewal_paid(
    session: Session,
    renewal: Renewal,
    *,
    causale: str,
    paid_at: datetime | None = None,
) -> Renewal:
    if not causale or not causale.strip():
        raise ValueError("causale is required to mark a renewal as paid")
    # Only a still-pending renewal may be paid. Without this, a re-POST (double
    # click, browser retry, deliberate replay) would extend expiry by another full
    # period with no new payment and could resurrect a cancelled subscription.
    if renewal.status != RenewalStatus.pending:
        raise ValueError("renewal is not pending (already paid or cancelled)")

    renewal.status = RenewalStatus.paid
    renewal.causale = causale.strip()
    renewal.paid_at = paid_at or utcnow()
    session.add(renewal)

    sub = session.get(Subscription, renewal.subscription_id)
    plan = session.get(Plan, renewal.plan_id)
    now = utcnow()
    base = max(now, sub.expiry_at) if sub.expiry_at else now
    sub.expiry_at = compute_expiry(base, plan, periods=renewal.periods or 1)
    sub.status = SubscriptionStatus.active
    session.add(sub)

    session.commit()
    session.refresh(renewal)
    return renewal


def list_plans(session: Session, only_active: bool = True) -> list[Plan]:
    stmt = select(Plan)
    if only_active:
        stmt = stmt.where(Plan.active.is_(True))
    return list(session.exec(stmt.order_by(Plan.price_cents)).all())


def get_plan_by_slug(session: Session, slug: str) -> Plan | None:
    return session.exec(select(Plan).where(Plan.slug == slug)).first()


def get_pending_renewal(session: Session, sub_id: int) -> Renewal | None:
    return session.exec(
        select(Renewal)
        .where(Renewal.subscription_id == sub_id)
        .where(Renewal.status == RenewalStatus.pending)
    ).first()


def has_pending_renewal(session: Session, sub_id: int) -> bool:
    return get_pending_renewal(session, sub_id) is not None


def list_renewals(session: Session, sub_id: int) -> list[Renewal]:
    stmt = (
        select(Renewal)
        .where(Renewal.subscription_id == sub_id)
        .order_by(Renewal.created_at.desc())
    )
    return list(session.exec(stmt).all())
