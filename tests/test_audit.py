from sqlmodel import select

from app.models import AppUser, Role
from app.services import audit


def _mk(session, role, name):
    u = AppUser(role=role, real_name=name)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _superadmin(session):
    return session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).one()


def test_list_recent_enriches_actor_and_target(db_session):
    actor = _mk(db_session, Role.moderator, "ModActor")
    target = _mk(db_session, Role.user, "TargetUser")
    audit.record(db_session, actor.id, "change_plan", "app_user", target.id, {"plan": "gold"})
    audit.record(db_session, target.id, "login", detail={"method": "plex"})

    entries = audit.list_recent(db_session, limit=10)
    assert entries[0]["actor"] == "TargetUser"   # newest first = login
    assert entries[1]["actor"] == "ModActor"
    assert entries[1]["target"] == "TargetUser"
    assert "gold" in entries[1]["detail"]


def test_audit_page_visible_to_admin(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AuditAdmin")
    login_as(client, admin.id)
    assert client.get("/audit", follow_redirects=False).status_code == 200


def test_audit_page_moderator_notifications_only_user_forbidden(client, db_session, login_as):
    # Moderators now reach /audit, but only the notifications tab — the audit
    # activity log stays admin-only, so its tab must not be offered to them.
    mod = _mk(db_session, Role.moderator, "AuditMod")
    login_as(client, mod.id)
    resp = client.get("/audit", follow_redirects=False)
    assert resp.status_code == 200
    assert "tab=activity" not in resp.text
    # Regular users have no access at all.
    user = _mk(db_session, Role.user, "AuditUser")
    login_as(client, user.id)
    assert client.get("/audit", follow_redirects=False).status_code == 403
