"""build_worklist bucket classification (uses the seeded paid plans)."""
from sqlmodel import select

from app.models import AppUser, Role
from app.services import subscriptions as sub_svc
from app.services.worklist import build_worklist


def test_worklist_pending_and_no_sub(db_session):
    s = db_session
    admin = s.exec(select(AppUser).where(AppUser.role == Role.superadmin)).first()
    plan = sub_svc.get_plan_by_slug(s, "bronze")

    # user with active sub + pending renewal -> pending bucket
    u1 = AppUser(role=Role.user, real_name="U1")
    s.add(u1); s.commit(); s.refresh(u1)
    sub1 = sub_svc.create_subscription(s, u1, plan)
    sub_svc.create_renewal(s, sub1, actor_id=None, collected_by=None)

    # user with no subscription -> no_sub bucket
    u2 = AppUser(role=Role.user, real_name="U2")
    s.add(u2); s.commit(); s.refresh(u2)

    wl = build_worklist(s, admin)
    assert any(it["user"].id == u1.id for it in wl["pending"])
    assert u1.id not in {it["user"].id for it in wl["to_collect"]}  # has pending -> not to_collect
    assert any(u.id == u2.id for u in wl["no_sub"])


def test_worklist_paid_but_suspended(db_session):
    s = db_session
    admin = s.exec(select(AppUser).where(AppUser.role == Role.superadmin)).first()
    plan = sub_svc.get_plan_by_slug(s, "gold")  # 12 months -> expiry in future

    u = AppUser(role=Role.user, real_name="Susp", access_suspended=True)
    s.add(u); s.commit(); s.refresh(u)
    sub_svc.create_subscription(s, u, plan)  # active, future expiry, suspended flag set

    wl = build_worklist(s, admin)
    assert any(it["user"].id == u.id for it in wl["paid_suspended"])
