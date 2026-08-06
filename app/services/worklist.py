"""Manager triage board: what needs action, scoped to the viewer's users.

Buckets (all scoped via users_svc.list_users_for):
- pending:        active sub with a pending renewal awaiting confirmation
- to_collect:     paid sub expiring in <=7d or overdue, no pending renewal
- paid_suspended: access suspended yet sub still valid (reactivate likely failed)
- no_sub:         a 'user' account with no active subscription

ponytail: per-user O(n) scan with a couple of queries each. Fine for the small
user base this app targets; revisit with aggregate SQL only if it gets slow.
"""
from app.models import Plan, Role, utcnow
from app.services import subscriptions as sub_svc
from app.services import users as users_svc


def build_worklist(session, viewer) -> dict:
    users = users_svc.list_users_for(session, viewer)
    today = utcnow().date()
    pending, to_collect, paid_suspended, no_sub = [], [], [], []

    for u in users:
        if not u.is_active:
            continue
        # active OR expired: an overdue sub (flipped to `expired` by the daily
        # scan) still needs collecting — it must not fall into the "no_sub" bucket.
        sub = sub_svc.get_current_subscription(session, u.id)
        if sub is None:
            if u.role == Role.user:
                no_sub.append(u)
            continue
        plan = session.get(Plan, sub.plan_id)
        pend = sub_svc.get_pending_renewal(session, sub.id)
        days_left = (sub.expiry_at.date() - today).days if sub.expiry_at else None

        if pend is not None:
            pending.append({"user": u, "sub": sub, "plan": plan,
                            "renewal": pend, "days_left": days_left})
        elif (
            days_left is not None and days_left <= 7
            and plan and plan.is_paid and not plan.is_unlimited and not plan.is_trial
        ):
            to_collect.append({"user": u, "sub": sub, "plan": plan,
                               "days_left": days_left})

        # Anomaly: money/time is valid but access is still off. Includes
        # unlimited/F&F subs (days_left is None = never expires) — a suspended
        # one is always an anomaly worth surfacing.
        if u.access_suspended and (days_left is None or days_left >= 0):
            paid_suspended.append({"user": u, "sub": sub, "plan": plan})

    to_collect.sort(key=lambda x: x["days_left"])          # most overdue first
    pending.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 9999))
    return {"pending": pending, "to_collect": to_collect,
            "paid_suspended": paid_suspended, "no_sub": no_sub}
