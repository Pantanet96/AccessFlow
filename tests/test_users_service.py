import pytest
from sqlmodel import select

from app.models import AppUser, Role
from app.services import users as svc


def _mk(session, role, name, manager_id=None, active=True):
    u = AppUser(role=role, real_name=name, manager_id=manager_id, is_active=active)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_list_scoping(db_session):
    admin = _mk(db_session, Role.admin, "Admin A")
    mod = _mk(db_session, Role.moderator, "Mod M")
    u1 = _mk(db_session, Role.user, "U1", manager_id=mod.id)
    _mk(db_session, Role.user, "U2", manager_id=admin.id)

    # admin sees all (incl. seeded superadmin)
    assert len(svc.list_users_for(db_session, admin)) >= 4
    # moderator sees only its assigned users
    mod_view = svc.list_users_for(db_session, mod)
    assert [u.id for u in mod_view] == [u1.id]
    # plain user sees nothing via list
    assert svc.list_users_for(db_session, u1) == []


def test_manager_candidates(db_session):
    _mk(db_session, Role.admin, "Cand Admin")
    _mk(db_session, Role.moderator, "Cand Mod")
    _mk(db_session, Role.user, "Plain")
    names = {c.real_name for c in svc.manager_candidates(db_session)}
    assert "Cand Admin" in names and "Cand Mod" in names
    assert "Plain" not in names


def test_orphan_protection(db_session):
    mod = _mk(db_session, Role.moderator, "Mgr")
    u = _mk(db_session, Role.user, "Sub", manager_id=mod.id)
    with pytest.raises(svc.OrphanError):
        svc.soft_delete(db_session, mod)
    # reassign the user away, then deletion is allowed
    svc.assign_manager(db_session, u, None)
    svc.soft_delete(db_session, mod)
    db_session.refresh(mod)
    assert mod.is_active is False


def test_update_profile_notify_fallback(db_session):
    u = _mk(db_session, Role.user, "P")
    u.plex_email = "p@example.com"
    db_session.add(u)
    db_session.commit()
    svc.update_profile(db_session, u, real_name="P2", notify_email="", telegram_id="123", locale="en")
    assert u.real_name == "P2"
    assert u.notify_email is None
    assert u.effective_notify_email == "p@example.com"
    assert u.telegram_id == "123"
    assert u.locale == "en"


def test_change_role(db_session):
    u = _mk(db_session, Role.user, "R")
    svc.change_role(db_session, u, Role.moderator)
    assert u.role == Role.moderator
