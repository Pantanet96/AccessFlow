"""Reporting: plan counters, upcoming expiries, earnings buckets and history.

Earnings (expiry-based projection, relative to `ref`'s calendar month):
- prev               = paid renewals with paid_at in the previous month
- current_collected  = paid renewals with paid_at in that month
- current_collectable= price of subs expiring that month with no paid renewal
- next_projected     = price of subs expiring the month after (paid plans only)

Every read takes an optional `manager_id`: it scopes by who *owns* the user
(AppUser.manager_id), not by who collected the money — the reports page asks
"how is this manager's book doing", not "who cashed the note".
"""
from datetime import datetime

from dateutil.relativedelta import relativedelta
from sqlmodel import Session, select

from app.models import (
    AppUser,
    Plan,
    Renewal,
    RenewalStatus,
    Role,
    Subscription,
    SubscriptionStatus,
    utcnow,
)

# Sentinel for "users with no manager". Real ids start at 1, so 0 is free and
# keeps every scoped read on a single code path instead of a second parameter.
UNASSIGNED = 0


def _month_start(ref: datetime) -> datetime:
    return ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _scope_user_ids(session: Session, manager_id: int | None) -> set[int] | None:
    """None = every user (no filter). An empty set = a manager with no users,
    which must match nothing — hence `None` and `set()` mean different things."""
    if manager_id is None:
        return None
    cond = (
        AppUser.manager_id.is_(None)
        if manager_id == UNASSIGNED
        else AppUser.manager_id == manager_id
    )
    return set(session.exec(select(AppUser.id).where(cond)).all())


def _scope_sub_ids(session: Session, manager_id: int | None) -> set[int] | None:
    uids = _scope_user_ids(session, manager_id)
    if uids is None:
        return None
    if not uids:
        return set()
    return set(
        session.exec(
            select(Subscription.id).where(Subscription.user_id.in_(uids))
        ).all()
    )


def _paid_in(session: Session, start: datetime, end: datetime, sub_ids=None):
    if sub_ids is not None and not sub_ids:
        return []
    stmt = (
        select(Renewal)
        .where(Renewal.status == RenewalStatus.paid)
        .where(Renewal.paid_at >= start)
        .where(Renewal.paid_at < end)
    )
    if sub_ids is not None:
        stmt = stmt.where(Renewal.subscription_id.in_(sub_ids))
    return session.exec(stmt).all()


def _sum_paid(session: Session, start: datetime, end: datetime, sub_ids=None) -> int:
    return sum(r.amount_cents for r in _paid_in(session, start, end, sub_ids))


def _subs_expiring(session: Session, start: datetime, end: datetime, uids=None):
    # active OR expired: a sub that lapsed earlier in the window is flipped to
    # `expired` by the daily scan; it is still collectable, so keep counting it.
    if uids is not None and not uids:
        return []
    stmt = (
        select(Subscription)
        .where(
            Subscription.status.in_(
                (SubscriptionStatus.active, SubscriptionStatus.expired)
            )
        )
        .where(Subscription.expiry_at.is_not(None))
        .where(Subscription.expiry_at >= start)
        .where(Subscription.expiry_at < end)
    )
    if uids is not None:
        stmt = stmt.where(Subscription.user_id.in_(uids))
    return session.exec(stmt).all()


def _has_paid_renewal(session: Session, sub_id: int, start: datetime, end: datetime) -> bool:
    return (
        session.exec(
            select(Renewal)
            .where(Renewal.subscription_id == sub_id)
            .where(Renewal.status == RenewalStatus.paid)
            .where(Renewal.paid_at >= start)
            .where(Renewal.paid_at < end)
        ).first()
        is not None
    )


def _active_paying(session: Session, uids=None) -> int:
    if uids is not None and not uids:
        return 0
    stmt = (
        select(Subscription)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.status == SubscriptionStatus.active)
        .where(Plan.is_paid.is_(True))
    )
    if uids is not None:
        stmt = stmt.where(Subscription.user_id.in_(uids))
    return len(session.exec(stmt).all())


def earnings(
    session: Session, ref: datetime | None = None, manager_id: int | None = None
) -> dict:
    ref = ref or utcnow()
    cur_start = _month_start(ref)
    next_start = cur_start + relativedelta(months=1)
    prev_start = cur_start - relativedelta(months=1)
    nn_start = next_start + relativedelta(months=1)

    uids = _scope_user_ids(session, manager_id)
    sub_ids = _scope_sub_ids(session, manager_id)

    prev = _sum_paid(session, prev_start, cur_start, sub_ids)
    current_collected = _sum_paid(session, cur_start, next_start, sub_ids)

    current_collectable = 0
    due = renewed = 0
    for sub in _subs_expiring(session, cur_start, next_start, uids):
        plan = session.get(Plan, sub.plan_id)
        if plan is None or not plan.is_paid:
            continue
        due += 1
        if _has_paid_renewal(session, sub.id, cur_start, next_start):
            renewed += 1
        else:
            current_collectable += plan.price_cents

    next_projected = 0
    for sub in _subs_expiring(session, next_start, nn_start, uids):
        plan = session.get(Plan, sub.plan_id)
        if plan is not None and plan.is_paid:
            next_projected += plan.price_cents

    paying = _active_paying(session, uids)
    return {
        "prev": prev,
        "current_collected": current_collected,
        "current_collectable": current_collectable,
        "next_projected": next_projected,
        # None (not 0) when nothing was due: "0% renewed" of nothing is a lie.
        "renewal_rate": (renewed / due * 100) if due else None,
        "paying_users": paying,
        "arpu_cents": round(current_collected / paying) if paying else 0,
    }


def monthly_series(
    session: Session,
    months: int = 12,
    ref: datetime | None = None,
    manager_id: int | None = None,
) -> list[dict]:
    """[{"month": "YYYY-MM", "collected_cents": n}], oldest first, ending on
    `ref`'s month. One query bucketed in Python — 12 round-trips buy nothing."""
    ref = ref or utcnow()
    end = _month_start(ref) + relativedelta(months=1)
    start = end - relativedelta(months=months)

    buckets: dict[str, int] = {}
    cur = start
    while cur < end:
        buckets[cur.strftime("%Y-%m")] = 0
        cur += relativedelta(months=1)

    sub_ids = _scope_sub_ids(session, manager_id)
    for r in _paid_in(session, start, end, sub_ids):
        key = r.paid_at.strftime("%Y-%m")
        if key in buckets:
            buckets[key] += r.amount_cents
    return [{"month": k, "collected_cents": v} for k, v in buckets.items()]


def manager_totals(session: Session, ref: datetime | None = None) -> list[dict]:
    """The selected month sliced per manager, plus a row for the users with no
    manager, so the rows reconcile with the headline cards.

    ponytail: one earnings() pass per manager. Fine at a handful of managers;
    if the list ever grows, fold it into a single grouped query.
    """
    mgrs = session.exec(
        select(AppUser)
        .where(AppUser.role.in_((Role.admin, Role.moderator)))
        .where(AppUser.is_active.is_(True))
        .order_by(AppUser.real_name)
    ).all()
    rows = [
        {"id": m.id, "name": m.real_name, **earnings(session, ref, m.id)}
        for m in mgrs
    ]
    # name=None -> the view prints "Superadmin": only admins and moderators can
    # ever be a manager, so an unset manager_id is the superadmin's own book.
    rows.append({"id": UNASSIGNED, "name": None,
                 **earnings(session, ref, UNASSIGNED)})
    return [
        r for r in rows
        if r["current_collected"] or r["current_collectable"] or r["paying_users"]
    ]


def plan_counters(session: Session, manager_id: int | None = None) -> list[dict]:
    uids = _scope_user_ids(session, manager_id)
    plans = session.exec(select(Plan).order_by(Plan.price_cents)).all()
    out = []
    for plan in plans:
        stmt = (
            select(Subscription)
            .where(Subscription.plan_id == plan.id)
            .where(Subscription.status == SubscriptionStatus.active)
        )
        if uids is not None:
            if not uids:
                out.append({"name": plan.name, "count": 0})
                continue
            stmt = stmt.where(Subscription.user_id.in_(uids))
        out.append({"name": plan.name, "count": len(session.exec(stmt).all())})
    return out


def paid_renewals(
    session: Session,
    start: datetime | None = None,
    end: datetime | None = None,
    manager_id: int | None = None,
) -> list[dict]:
    """Paid renewals (setup + renewals), newest first — the movement history
    behind the earnings buckets, and the CSV export. No window = everything."""
    sub_ids = _scope_sub_ids(session, manager_id)
    if sub_ids is not None and not sub_ids:
        return []
    stmt = (
        select(Renewal)
        .where(Renewal.status == RenewalStatus.paid)
        .order_by(Renewal.paid_at.desc())
    )
    if start is not None:
        stmt = stmt.where(Renewal.paid_at >= start)
    if end is not None:
        stmt = stmt.where(Renewal.paid_at < end)
    if sub_ids is not None:
        stmt = stmt.where(Renewal.subscription_id.in_(sub_ids))

    out = []
    for r in session.exec(stmt).all():
        sub = session.get(Subscription, r.subscription_id)
        user = session.get(AppUser, sub.user_id) if sub else None
        plan = session.get(Plan, r.plan_id)
        collector = session.get(AppUser, r.collected_by) if r.collected_by else None
        manager = (
            session.get(AppUser, user.manager_id)
            if user and user.manager_id
            else None
        )
        out.append({
            "paid_at": r.paid_at,
            "user": user.real_name if user else "?",
            "user_id": user.id if user else None,
            "plan": plan.name if plan else "?",
            "amount_cents": r.amount_cents,
            "causale": r.causale or "",
            "collected_by": collector.real_name if collector else "",
            "manager": manager.real_name if manager else "",
        })
    return out


def upcoming_expiries(
    session: Session,
    days: int = 30,
    ref: datetime | None = None,
    manager_id: int | None = None,
) -> list[dict]:
    ref = ref or utcnow()
    horizon = ref + relativedelta(days=days)
    uids = _scope_user_ids(session, manager_id)
    if uids is not None and not uids:
        return []
    stmt = (
        select(Subscription)
        .where(Subscription.status == SubscriptionStatus.active)
        .where(Subscription.expiry_at.is_not(None))
        .where(Subscription.expiry_at >= ref)
        .where(Subscription.expiry_at < horizon)
        .order_by(Subscription.expiry_at)
    )
    if uids is not None:
        stmt = stmt.where(Subscription.user_id.in_(uids))

    rows = []
    for sub in session.exec(stmt).all():
        user = session.get(AppUser, sub.user_id)
        plan = session.get(Plan, sub.plan_id)
        pending = session.exec(
            select(Renewal)
            .where(Renewal.subscription_id == sub.id)
            .where(Renewal.status == RenewalStatus.pending)
        ).first() is not None
        rows.append(
            {
                "user": user.real_name if user else "?",
                "user_id": user.id if user else None,
                "sub_id": sub.id,
                "plan": plan.name if plan else "?",
                "amount_cents": plan.price_cents if plan else 0,
                "expiry": sub.expiry_at,
                "days_left": (sub.expiry_at.date() - ref.date()).days,
                "pending_renewal": pending,
            }
        )
    return rows
