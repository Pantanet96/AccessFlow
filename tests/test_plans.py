from sqlmodel import select

from app.models import AppUser, Plan, Role
from app.services import plans as plans_svc
from app.services import subscriptions as sub_svc


def _superadmin(session):
    return session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).one()


def _mk(session, role, name):
    u = AppUser(role=role, real_name=name)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_seed_has_only_two_builtin_plans(db_session):
    # db_session fixture adds test paid plans, so check via fresh seed semantics:
    builtin = db_session.exec(
        select(Plan).where(Plan.is_unlimited | Plan.is_trial)
    ).all()
    slugs = {p.slug for p in builtin}
    assert slugs == {"family_friends", "trial"}


def test_create_plan_slug_and_paid(db_session):
    plan = plans_svc.create_plan(
        db_session, name="Gold Yearly", plan_type="paid", price_cents=5000,
        duration_months=12, duration_days=None,
    )
    assert plan.slug == "gold_yearly"
    assert plan.is_paid and not plan.is_unlimited and not plan.is_trial
    assert plan.price_cents == 5000 and plan.duration_months == 12


def test_create_trial_plan_with_days(db_session):
    plan = plans_svc.create_plan(
        db_session, name="Trial 15", plan_type="trial", duration_days=15,
    )
    assert plan.is_trial and not plan.is_paid
    assert plan.duration_days == 15 and plan.price_cents == 0
    # capped at 30
    capped = plans_svc.create_plan(
        db_session, name="Trial 99", plan_type="trial", duration_days=99,
    )
    assert capped.duration_days == 30


def test_create_ff_plan(db_session):
    plan = plans_svc.create_plan(
        db_session, name="VIP Friends", plan_type="family_friends",
    )
    assert plan.is_unlimited and not plan.is_paid
    assert plan.duration_days is None and plan.duration_months is None


def test_create_plan_with_libraries(db_session):
    plan = plans_svc.create_plan(
        db_session, name="Movies Only", plan_type="paid", price_cents=300,
        duration_months=1, libraries=["Movies"],
    )
    import json
    assert json.loads(plan.libraries) == ["Movies"]


def test_unique_slug(db_session):
    p1 = plans_svc.create_plan(db_session, name="Same", plan_type="paid",
                               price_cents=100, duration_months=1)
    p2 = plans_svc.create_plan(db_session, name="Same", plan_type="paid",
                               price_cents=200, duration_months=1)
    assert p1.slug != p2.slug


def test_delete_unused_plan(db_session):
    plan = plans_svc.create_plan(db_session, name="Temp", plan_type="paid",
                                 price_cents=100, duration_months=1)
    plans_svc.delete_plan(db_session, plan)
    assert db_session.get(Plan, plan.id) is None


def test_cannot_delete_plan_in_use(db_session):
    import pytest

    plan = plans_svc.create_plan(db_session, name="Used", plan_type="paid",
                                 price_cents=100, duration_months=1)
    user = _mk(db_session, Role.user, "Sub")
    sub_svc.create_subscription(db_session, user, plan)
    with pytest.raises(plans_svc.PlanInUse):
        plans_svc.delete_plan(db_session, plan)


def test_plans_route_superadmin_create(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    resp = client.post(
        "/plans",
        data={"name": "Platinum", "plan_type": "paid", "price": "9.99",
              "duration_months": "3", "duration_days": "", "libraries": ["Movies"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    plan = db_session.exec(select(Plan).where(Plan.slug == "platinum")).one()
    assert plan.price_cents == 999 and plan.duration_months == 3
    import json
    assert json.loads(plan.libraries) == ["Movies"]


def test_plans_route_forbidden_for_admin(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "PlanAdmin")
    login_as(client, admin.id)
    assert client.get("/plans", follow_redirects=False).status_code == 403


def test_plans_row_has_mobile_details_toggle(client, db_session, login_as):
    plans_svc.create_plan(db_session, name="ToggleMe", plan_type="paid",
                          price_cents=100, duration_months=1)
    login_as(client, _superadmin(db_session).id)
    resp = client.get("/plans")
    assert resp.status_code == 200
    assert "plan-extra" in resp.text
    assert 'class="row-toggle-cell"' in resp.text


def test_plans_route_superadmin_edit_after_mobile_markup_change(client, db_session, login_as):
    plan = plans_svc.create_plan(db_session, name="EditMe", plan_type="paid",
                                 price_cents=100, duration_months=1)
    login_as(client, _superadmin(db_session).id)
    resp = client.post(
        f"/plans/{plan.id}/edit",
        data={"name": "EditMe", "price": "12.00", "duration_months": "2",
              "duration_days": "", "active": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    updated = db_session.get(Plan, plan.id)
    assert updated.price_cents == 1200 and updated.duration_months == 2
