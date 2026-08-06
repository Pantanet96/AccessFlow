from sqlmodel import select

import app.services.plex_oauth as po
from app.auth.session import COOKIE_NAME
from app.models import AppUser, Invite, InviteStatus, Role


def _start_pin(client, monkeypatch, pin_id=42):
    monkeypatch.setattr(po, "create_pin", lambda: {"id": pin_id, "code": "ABCD"})
    return client.get("/login/plex", follow_redirects=False)


def test_plex_start_redirects_and_sets_pin(client, monkeypatch):
    resp = _start_pin(client, monkeypatch)
    assert resp.status_code == 303
    assert "app.plex.tv/auth" in resp.headers["location"]
    assert "plex_pin" in resp.cookies


def test_callback_without_pin_redirects_to_login(client):
    resp = client.get("/login/plex/callback", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_plex_login_existing_user(client, db_session, monkeypatch):
    db_session.add(
        AppUser(role=Role.user, real_name="Mario", plex_email="mario@example.com")
    )
    db_session.commit()

    _start_pin(client, monkeypatch)
    monkeypatch.setattr(po, "poll_pin", lambda pid: "tok")
    monkeypatch.setattr(
        po,
        "fetch_account",
        lambda t: {"id": "9001", "email": "mario@example.com", "username": "mario"},
    )
    resp = client.get("/login/plex/callback", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert COOKIE_NAME in resp.cookies

    db_session.commit()
    user = db_session.exec(
        select(AppUser).where(AppUser.plex_email == "mario@example.com")
    ).one()
    assert user.plex_account_id == "9001"


def test_plex_login_pending_invite_activates_user(client, db_session, monkeypatch):
    db_session.add(
        Invite(
            email="lucia@example.com",
            real_name="Lucia",
            intended_role=Role.user,
            token="inv-token-1",
        )
    )
    db_session.commit()

    _start_pin(client, monkeypatch)
    monkeypatch.setattr(po, "poll_pin", lambda pid: "tok")
    monkeypatch.setattr(
        po,
        "fetch_account",
        lambda t: {"id": "9002", "email": "lucia@example.com", "username": "lucia"},
    )
    resp = client.get("/login/plex/callback", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    db_session.commit()
    user = db_session.exec(
        select(AppUser).where(AppUser.plex_email == "lucia@example.com")
    ).one()
    assert user.role == Role.user and user.real_name == "Lucia"
    invite = db_session.exec(
        select(Invite).where(Invite.token == "inv-token-1")
    ).one()
    assert invite.status == InviteStatus.accepted


def test_plex_login_unknown_account_denied(client, db_session, monkeypatch):
    _start_pin(client, monkeypatch)
    monkeypatch.setattr(po, "poll_pin", lambda pid: "tok")
    monkeypatch.setattr(
        po,
        "fetch_account",
        lambda t: {"id": "9999", "email": "stranger@example.com", "username": "x"},
    )
    resp = client.get("/login/plex/callback", follow_redirects=False)
    assert resp.status_code == 403
    assert COOKIE_NAME not in resp.cookies


def test_callback_no_token_shows_error(client, monkeypatch):
    _start_pin(client, monkeypatch)
    monkeypatch.setattr(po, "poll_pin", lambda pid: None)
    resp = client.get("/login/plex/callback", follow_redirects=False)
    assert resp.status_code == 401


def test_plex_login_server_owner_becomes_superadmin(client, db_session, monkeypatch):
    from app.services import settings_store

    settings_store.set_value(db_session, "plex_account_email", "owner@example.com")

    _start_pin(client, monkeypatch)
    monkeypatch.setattr(po, "poll_pin", lambda pid: "tok")
    monkeypatch.setattr(
        po,
        "fetch_account",
        lambda t: {"id": "5000", "email": "owner@example.com", "username": "owner"},
    )
    resp = client.get("/login/plex/callback", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert COOKIE_NAME in resp.cookies

    db_session.commit()
    sa = db_session.exec(
        select(AppUser).where(AppUser.role == Role.superadmin)
    ).one()
    assert sa.plex_account_id == "5000"
    assert sa.plex_email == "owner@example.com"
