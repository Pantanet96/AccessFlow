"""Role hierarchy: superadmin > admin > moderator > user.

You act only on people strictly below you (managers scoped to their own users);
nobody sets their own plan except the superadmin owner; you cannot delete or
re-role a peer or a superior.
"""
from sqlmodel import select

from app.models import AppUser, Role
from app.services import users as users_svc


def _mk(session, role, name, manager_id=None):
    u = AppUser(role=role, real_name=name, manager_id=manager_id)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _plan(session, slug):
    from app.models import Plan

    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def _superadmin(session):
    return session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).first()


# ---- can_manage_user unit matrix ----

def test_can_manage_user_matrix(db_session):
    sa = _superadmin(db_session)
    admin = _mk(db_session, Role.admin, "A")
    admin2 = _mk(db_session, Role.admin, "A2")
    mod = _mk(db_session, Role.moderator, "M")
    mine = _mk(db_session, Role.user, "Mine", manager_id=mod.id)
    other = _mk(db_session, Role.user, "Other")

    # strictly below -> True
    assert users_svc.can_manage_user(admin, mod) is True
    assert users_svc.can_manage_user(admin, other) is True
    assert users_svc.can_manage_user(mod, mine) is True
    assert users_svc.can_manage_user(sa, admin) is True

    # peer / superior / unassigned -> False
    assert users_svc.can_manage_user(admin, admin2) is False
    assert users_svc.can_manage_user(mod, other) is False
    assert users_svc.can_manage_user(mod, admin) is False

    # self: only the superadmin owner
    assert users_svc.can_manage_user(admin, admin) is False
    assert users_svc.can_manage_user(mod, mod) is False
    assert users_svc.can_manage_user(sa, sa) is True


# ---- self plan-change is blocked for manager/admin ----

def test_manager_cannot_set_own_plan(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModSelf")
    login_as(client, mod.id)
    resp = client.post(
        f"/users/{mod.id}/subscription/plan",
        data={"plan_slug": "gold", "trial_days": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_admin_cannot_set_own_plan(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminSelf")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{admin.id}/subscription/plan",
        data={"plan_slug": "gold", "trial_days": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_superadmin_can_set_own_plan(client, db_session, login_as):
    sa = _superadmin(db_session)
    login_as(client, sa.id)
    resp = client.post(
        f"/users/{sa.id}/subscription/plan",
        data={"plan_slug": "gold", "trial_days": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303


# ---- manager cannot touch users that aren't theirs ----

def test_manager_cannot_change_unassigned_user_plan(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModX2")
    other = _mk(db_session, Role.user, "NotMine2")
    login_as(client, mod.id)
    resp = client.post(
        f"/users/{other.id}/subscription/plan",
        data={"plan_slug": "gold", "trial_days": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---- cannot disable a peer or a superior ----

def test_admin_cannot_delete_peer_admin(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminA")
    peer = _mk(db_session, Role.admin, "AdminB")
    login_as(client, admin.id)
    resp = client.post(f"/users/{peer.id}/delete", follow_redirects=False)
    assert resp.status_code == 303  # silently ignored
    db_session.commit()
    assert db_session.get(AppUser, peer.id).is_active is True


def test_admin_cannot_delete_superadmin(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminC")
    sa = _superadmin(db_session)
    login_as(client, admin.id)
    resp = client.post(f"/users/{sa.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    db_session.commit()
    assert db_session.get(AppUser, sa.id).is_active is True


def test_admin_cannot_change_roles(client, db_session, login_as):
    # set_role needs manage_roles (superadmin only) -> admin is forbidden outright.
    admin = _mk(db_session, Role.admin, "AdminRole")
    target = _mk(db_session, Role.user, "Pawn")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/role", data={"role": "moderator"}, follow_redirects=False
    )
    assert resp.status_code == 403
    db_session.commit()
    assert db_session.get(AppUser, target.id).role == Role.user
