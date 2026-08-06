from sqlmodel import select

from app.models import AppUser, Renewal, RenewalStatus, Role
from app.services import subscriptions as svc


def _mk(session, role, name, manager_id=None):
    u = AppUser(role=role, real_name=name, manager_id=manager_id)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _plan(session, slug):
    from app.models import Plan

    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def test_moderator_renew_assigned_user_flow(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModR")
    user = _mk(db_session, Role.user, "Assigned", manager_id=mod.id)
    bronze = _plan(db_session, "bronze")
    sub = svc.create_subscription(db_session, user, bronze)
    login_as(client, mod.id)

    # create pending renewal
    resp = client.post(f"/subscriptions/{sub.id}/renew", follow_redirects=False)
    assert resp.status_code == 303
    db_session.commit()
    r = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).one()
    assert r.status == RenewalStatus.pending

    # mark paid with causale
    resp = client.post(
        f"/renewals/{r.id}/pay",
        data={"causale": "paypal amici"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    db_session.refresh(r)
    assert r.status == RenewalStatus.paid and r.causale == "paypal amici"


def test_first_paid_setup_records_payment(client, db_session, login_as):
    # First paid setup must log a paid renewal so it shows in the reports page.
    from app.models import RenewalStatus
    from app.services import reports as reports_svc

    admin = _mk(db_session, Role.admin, "AdmPay")
    user = _mk(db_session, Role.user, "PayU")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{user.id}/subscription/plan",
        data={"plan_slug": "gold", "trial_days": "", "periods": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    sub = svc.get_active_subscription(db_session, user.id)
    r = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).one()
    gold = _plan(db_session, "gold")
    assert r.status == RenewalStatus.paid and r.amount_cents == gold.price_cents
    # money is now visible in earnings for the current month
    assert reports_svc.earnings(db_session)["current_collected"] >= gold.price_cents


def test_admin_set_expiry_override(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdmExp")
    user = _mk(db_session, Role.user, "U", manager_id=admin.id)
    bronze = _plan(db_session, "bronze")
    sub = svc.create_subscription(db_session, user, bronze)
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{user.id}/subscription/expiry",
        data={"expiry_date": "2027-03-15"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(sub)
    assert sub.expiry_at.date().isoformat() == "2027-03-15"


def test_moderator_cannot_set_expiry(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModExp")
    user = _mk(db_session, Role.user, "U2", manager_id=mod.id)
    bronze = _plan(db_session, "bronze")
    svc.create_subscription(db_session, user, bronze)
    login_as(client, mod.id)
    resp = client.post(
        f"/users/{user.id}/subscription/expiry",
        data={"expiry_date": "2027-03-15"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_moderator_cannot_renew_unassigned(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModU")
    other = _mk(db_session, Role.user, "NotMine")
    bronze = _plan(db_session, "bronze")
    sub = svc.create_subscription(db_session, other, bronze)
    login_as(client, mod.id)
    resp = client.post(f"/subscriptions/{sub.id}/renew", follow_redirects=False)
    assert resp.status_code == 403


def test_moderator_cannot_change_to_free_plan(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModF")
    user = _mk(db_session, Role.user, "U", manager_id=mod.id)
    login_as(client, mod.id)
    resp = client.post(
        f"/users/{user.id}/subscription/plan",
        data={"plan_slug": "family_friends", "trial_days": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_admin_can_assign_any_plan(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminP")
    user = _mk(db_session, Role.user, "U2")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{user.id}/subscription/plan",
        data={"plan_slug": "gold", "trial_days": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    sub = svc.get_active_subscription(db_session, user.id)
    assert sub is not None and sub.plan_id == _plan(db_session, "gold").id


def test_create_with_backdated_start(client, db_session, login_as):
    from datetime import datetime

    from dateutil.relativedelta import relativedelta

    admin = _mk(db_session, Role.admin, "AdminBack")
    user = _mk(db_session, Role.user, "Paying")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{user.id}/subscription/plan",
        data={"plan_slug": "gold", "trial_days": "", "start_date": "2026-01-15"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    sub = svc.get_active_subscription(db_session, user.id)
    assert sub.start_at.date() == datetime(2026, 1, 15).date()
    # Gold = 12 months -> expiry back-dated accordingly
    assert sub.expiry_at.date() == (datetime(2026, 1, 15) + relativedelta(months=12)).date()


def test_change_plan_ignores_start_date(client, db_session, login_as):
    from datetime import datetime

    admin = _mk(db_session, Role.admin, "AdminChg")
    user = _mk(db_session, Role.user, "HasSub")
    bronze = _plan(db_session, "bronze")
    existing = svc.create_subscription(db_session, user, bronze)
    original_expiry = existing.expiry_at
    login_as(client, admin.id)
    # change to gold with a start_date -> start_date ignored, paid->paid keeps expiry
    client.post(
        f"/users/{user.id}/subscription/plan",
        data={"plan_slug": "gold", "start_date": "2020-01-01"},
        follow_redirects=False,
    )
    db_session.commit()
    sub = svc.get_active_subscription(db_session, user.id)
    assert sub.expiry_at == original_expiry


def test_change_plan_from_family_friends_with_start_records_payment(
    client, db_session, login_as
):
    # Upgrading F&F -> paid with a manual start date is an up-front payment
    # (mirrors first-paid-setup); it must be logged as a paid renewal.
    from app.services import reports as reports_svc

    admin = _mk(db_session, Role.admin, "AdminFFUpg")
    user = _mk(db_session, Role.user, "FFtoPaid")
    ff = _plan(db_session, "family_friends")
    svc.create_subscription(db_session, user, ff)
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{user.id}/subscription/plan",
        data={"plan_slug": "gold", "start_date": "2026-01-01", "periods": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    sub = svc.get_active_subscription(db_session, user.id)
    gold = _plan(db_session, "gold")
    r = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).one()
    assert r.status == RenewalStatus.paid and r.amount_cents == gold.price_cents
    rows = reports_svc.paid_renewals(db_session)
    assert any(row["amount_cents"] == gold.price_cents for row in rows)


def test_change_plan_from_trial_with_start_records_payment(
    client, db_session, login_as
):
    from app.services import reports as reports_svc

    admin = _mk(db_session, Role.admin, "AdminTrialUpg")
    user = _mk(db_session, Role.user, "TrialToPaid")
    trial = _plan(db_session, "trial")
    svc.create_subscription(db_session, user, trial, trial_days=7)
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{user.id}/subscription/plan",
        data={"plan_slug": "gold", "start_date": "2026-01-01", "periods": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    sub = svc.get_active_subscription(db_session, user.id)
    gold = _plan(db_session, "gold")
    r = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).one()
    assert r.status == RenewalStatus.paid and r.amount_cents == gold.price_cents
    rows = reports_svc.paid_renewals(db_session)
    assert any(row["amount_cents"] == gold.price_cents for row in rows)


def test_user_can_view_own_but_not_renew(client, db_session, login_as):
    user = _mk(db_session, Role.user, "Owner")
    bronze = _plan(db_session, "bronze")
    sub = svc.create_subscription(db_session, user, bronze)
    login_as(client, user.id)
    # can view
    assert client.get(f"/users/{user.id}/subscription").status_code == 200
    # cannot renew
    resp = client.post(f"/subscriptions/{sub.id}/renew", follow_redirects=False)
    assert resp.status_code == 403


def test_user_cannot_view_others_subscription(client, db_session, login_as):
    user = _mk(db_session, Role.user, "A")
    other = _mk(db_session, Role.user, "B")
    login_as(client, user.id)
    resp = client.get(f"/users/{other.id}/subscription", follow_redirects=False)
    assert resp.status_code == 403


def test_subscription_detail_shows_account_panel_for_manager(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminAcct")
    target = _mk(db_session, Role.user, "AcctTarget", manager_id=admin.id)
    login_as(client, admin.id)
    resp = client.get(f"/users/{target.id}/subscription")
    assert resp.status_code == 200
    assert f'action="/users/{target.id}/name"' in resp.text
    assert f'action="/users/{target.id}/role"' not in resp.text  # admin, non superadmin: niente select ruolo
    assert f'action="/users/{target.id}/manager"' in resp.text
    assert f'action="/users/{target.id}/delete"' in resp.text


def test_subscription_detail_hides_account_panel_for_self_view(client, db_session, login_as):
    user = _mk(db_session, Role.user, "SelfAcct")
    login_as(client, user.id)
    resp = client.get(f"/users/{user.id}/subscription")
    assert resp.status_code == 200
    assert f'action="/users/{user.id}/name"' not in resp.text
    assert f'action="/users/{user.id}/manager"' not in resp.text
    assert f'action="/users/{user.id}/delete"' not in resp.text


def test_subscription_detail_superadmin_sees_role_select(client, db_session, login_as):
    superadmin = db_session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).first()
    target = _mk(db_session, Role.user, "RoleTarget")
    login_as(client, superadmin.id)
    resp = client.get(f"/users/{target.id}/subscription")
    assert resp.status_code == 200
    assert f'action="/users/{target.id}/role"' in resp.text


def test_subscription_detail_no_delete_for_inactive_target(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminInact")
    target = _mk(db_session, Role.user, "InactiveTarget", manager_id=admin.id)
    target.is_active = False
    db_session.add(target)
    db_session.commit()
    login_as(client, admin.id)
    resp = client.get(f"/users/{target.id}/subscription")
    assert resp.status_code == 200
    assert f'action="/users/{target.id}/delete"' not in resp.text
