from datetime import timedelta

import pytest
from dateutil.relativedelta import relativedelta

from app.models import AppUser, Plan, RenewalStatus, Role, SubscriptionStatus, utcnow
from app.services import subscriptions as svc


def _plan(session, slug):
    from sqlmodel import select

    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def _user(session):
    u = AppUser(role=Role.user, real_name="Tester")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


# ---- compute_expiry ----

def test_compute_expiry_months(db_session):
    base = utcnow()
    gold = _plan(db_session, "gold")
    assert svc.compute_expiry(base, gold) == base + relativedelta(months=12)


def test_compute_expiry_unlimited_none(db_session):
    base = utcnow()
    ff = _plan(db_session, "family_friends")
    assert svc.compute_expiry(base, ff) is None


def test_compute_expiry_trial_capped(db_session):
    base = utcnow()
    trial = _plan(db_session, "trial")
    assert svc.compute_expiry(base, trial, trial_days=10) == base + timedelta(days=10)
    # capped at 30
    assert svc.compute_expiry(base, trial, trial_days=99) == base + timedelta(days=30)


# ---- create_subscription ----

def test_create_subscription_sets_expiry_and_cancels_prior(db_session):
    u = _user(db_session)
    bronze = _plan(db_session, "bronze")
    sub1 = svc.create_subscription(db_session, u, bronze)
    assert sub1.status == SubscriptionStatus.active
    assert sub1.expiry_at is not None

    silver = _plan(db_session, "silver")
    sub2 = svc.create_subscription(db_session, u, silver)
    db_session.refresh(sub1)
    assert sub1.status == SubscriptionStatus.cancelled
    assert svc.get_active_subscription(db_session, u.id).id == sub2.id


def test_create_subscription_multiple_periods(db_session):
    # First setup paying N periods up-front: expiry = start + N × duration.
    u = _user(db_session)
    bronze = _plan(db_session, "bronze")  # 1 month
    sub = svc.create_subscription(db_session, u, bronze, periods=5)
    assert sub.expiry_at.date() == (sub.start_at + relativedelta(months=5)).date()


# ---- renewal (the risky math) ----

def test_mark_paid_early_extends_from_current_expiry(db_session):
    u = _user(db_session)
    bronze = _plan(db_session, "bronze")
    sub = svc.create_subscription(db_session, u, bronze)
    # expiry 10 days out (early renewal)
    future = utcnow() + timedelta(days=10)
    sub.expiry_at = future
    db_session.add(sub)
    db_session.commit()

    r = svc.create_renewal(db_session, sub, actor_id=None, collected_by=None)
    assert r.status == RenewalStatus.pending and r.amount_cents == 500
    svc.mark_renewal_paid(db_session, r, causale="paypal x")
    db_session.refresh(sub)
    expected = (future + relativedelta(months=1)).date()
    assert sub.expiry_at.date() == expected


def test_mark_paid_expired_extends_from_today(db_session):
    u = _user(db_session)
    bronze = _plan(db_session, "bronze")
    sub = svc.create_subscription(db_session, u, bronze)
    sub.expiry_at = utcnow() - timedelta(days=5)  # already expired
    db_session.add(sub)
    db_session.commit()

    r = svc.create_renewal(db_session, sub, actor_id=None, collected_by=None)
    svc.mark_renewal_paid(db_session, r, causale="paypal y")
    db_session.refresh(sub)
    expected = (utcnow() + relativedelta(months=1)).date()
    assert sub.expiry_at.date() == expected


def test_renew_multiple_periods(db_session):
    u = _user(db_session)
    bronze = _plan(db_session, "bronze")  # 1 month, 500
    sub = svc.create_subscription(db_session, u, bronze)
    future = utcnow() + timedelta(days=10)
    sub.expiry_at = future
    db_session.add(sub)
    db_session.commit()

    r = svc.create_renewal(
        db_session, sub, actor_id=None, collected_by=None, periods=3
    )
    assert r.periods == 3 and r.amount_cents == 1500  # 3 × price
    svc.mark_renewal_paid(db_session, r, causale="paypal x")
    db_session.refresh(sub)
    assert sub.expiry_at.date() == (future + relativedelta(months=3)).date()


def test_mark_paid_requires_causale(db_session):
    u = _user(db_session)
    bronze = _plan(db_session, "bronze")
    sub = svc.create_subscription(db_session, u, bronze)
    r = svc.create_renewal(db_session, sub, actor_id=None, collected_by=None)
    with pytest.raises(ValueError):
        svc.mark_renewal_paid(db_session, r, causale="  ")


def test_cannot_renew_unlimited_or_trial(db_session):
    u = _user(db_session)
    ff = _plan(db_session, "family_friends")
    sub = svc.create_subscription(db_session, u, ff)
    with pytest.raises(ValueError):
        svc.create_renewal(db_session, sub, actor_id=None, collected_by=None)


# ---- change_plan ----

def test_change_plan_paid_to_paid_keeps_expiry(db_session):
    u = _user(db_session)
    bronze = _plan(db_session, "bronze")
    gold = _plan(db_session, "gold")
    sub = svc.create_subscription(db_session, u, bronze)
    original = sub.expiry_at
    svc.change_plan(db_session, sub, gold)
    db_session.refresh(sub)
    assert sub.plan_id == gold.id
    assert sub.expiry_at == original  # no free time granted


def test_change_plan_to_ff_clears_expiry(db_session):
    u = _user(db_session)
    bronze = _plan(db_session, "bronze")
    ff = _plan(db_session, "family_friends")
    sub = svc.create_subscription(db_session, u, bronze)
    svc.change_plan(db_session, sub, ff)
    db_session.refresh(sub)
    assert sub.expiry_at is None


def test_change_plan_from_ff_to_paid_with_manual_start(db_session):
    from datetime import datetime

    u = _user(db_session)
    ff = _plan(db_session, "family_friends")
    bronze = _plan(db_session, "bronze")  # 1 month
    sub = svc.create_subscription(db_session, u, ff)
    assert sub.expiry_at is None
    start = datetime(2026, 1, 15)
    svc.change_plan(db_session, sub, bronze, start=start)
    db_session.refresh(sub)
    assert sub.start_at == start
    assert sub.expiry_at == start + relativedelta(months=1)


def test_change_plan_from_ff_to_paid_sets_expiry_now(db_session):
    u = _user(db_session)
    ff = _plan(db_session, "family_friends")
    bronze = _plan(db_session, "bronze")
    sub = svc.create_subscription(db_session, u, ff)
    assert sub.expiry_at is None
    svc.change_plan(db_session, sub, bronze)
    db_session.refresh(sub)
    assert sub.expiry_at is not None
    assert sub.expiry_at.date() == utcnow().date()
