from sqlmodel import select

import app.services.plex_service as plex_service
from app.models import AppUser, Role
from app.services import plex_import


def _mk(session, role, name, **kw):
    u = AppUser(role=role, real_name=name, **kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_import_creates_and_skips(db_session, monkeypatch):
    # one already exists (by account id)
    _mk(db_session, Role.user, "Existing", plex_account_id="100")
    monkeypatch.setattr(
        plex_service,
        "list_shared_users",
        lambda: [
            {"id": "100", "email": "existing@example.com", "username": "existing"},
            {"id": "200", "email": "new1@example.com", "username": "new1"},
            {"id": "300", "email": "new2@example.com", "username": "new2"},
        ],
    )
    result = plex_import.import_plex_users(db_session)
    assert result["created"] == 2 and result["skipped"] == 1
    assert result["stale"] == []  # everyone still shared
    new = db_session.exec(
        select(AppUser).where(AppUser.plex_account_id == "200")
    ).one()
    assert new.role == Role.user and new.manager_id is None


def test_import_route_admin(client, db_session, login_as, monkeypatch):
    monkeypatch.setattr(
        plex_service,
        "list_shared_users",
        lambda: [{"id": "900", "email": "imp@example.com", "username": "imp"}],
    )
    admin = _mk(db_session, Role.admin, "ImpAdmin")
    login_as(client, admin.id)
    resp = client.post("/users/import-plex")
    assert resp.status_code == 200
    assert db_session.exec(
        select(AppUser).where(AppUser.plex_account_id == "900")
    ).first() is not None


def test_import_route_forbidden_for_moderator(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModNoImport")
    login_as(client, mod.id)
    assert client.post("/users/import-plex", follow_redirects=False).status_code == 403


def test_import_not_connected(client, db_session, login_as, monkeypatch):
    def _boom():
        raise plex_service.PlexNotConnected()

    monkeypatch.setattr(plex_service, "list_shared_users", _boom)
    admin = _mk(db_session, Role.admin, "ImpAdmin2")
    login_as(client, admin.id)
    resp = client.post("/users/import-plex", follow_redirects=False)
    assert resp.status_code == 400


def test_import_detects_stale_users(db_session, monkeypatch):
    # Active app user who is NOT in the current Plex shared list -> stale.
    _mk(db_session, Role.user, "GoneFromPlex",
        plex_account_id="500", plex_email="gone@example.com")
    # Suspended-on-purpose user should NOT be flagged stale.
    _mk(db_session, Role.user, "Suspended",
        plex_account_id="600", plex_email="susp@example.com",
        access_suspended=True)
    monkeypatch.setattr(
        plex_service, "list_shared_users",
        lambda: [{"id": "700", "email": "still@example.com", "username": "still"}],
    )
    result = plex_import.import_plex_users(db_session)
    assert "GoneFromPlex" in result["stale"]
    assert "Suspended" not in result["stale"]


def test_users_without_active_subscription(db_session, monkeypatch):
    from app.models import Plan
    from app.services import subscriptions as sub_svc

    u_no = _mk(db_session, Role.user, "NoPlan", plex_email="np@example.com")
    u_yes = _mk(db_session, Role.user, "HasPlan", plex_email="hp@example.com")
    plan = db_session.exec(select(Plan)).first()
    sub_svc.create_subscription(db_session, u_yes, plan)

    names = [u.real_name for u in plex_import.users_without_active_subscription(db_session)]
    assert "NoPlan" in names
    assert "HasPlan" not in names
