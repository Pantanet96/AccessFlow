from sqlmodel import select

import app.services.mail_service as mail_service
import app.services.plex_oauth as po
import app.services.telegram_service as telegram_service
from app import runtime_config
from app.models import AppUser, Role
from app.services import settings_store


def _superadmin(session):
    return session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).one()


def _mk(session, role, name):
    u = AppUser(role=role, real_name=name)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


# ---- settings_store ----

def test_settings_store_roundtrip_encrypted(db_session):
    settings_store.set_value(db_session, "plex_token", "secret-tok")
    # stored value is encrypted, not the plaintext
    from app.models import AppSetting

    row = db_session.get(AppSetting, "plex_token")
    assert row.value != "secret-tok"
    assert settings_store.get_value(db_session, "plex_token") == "secret-tok"


def test_settings_store_delete_on_empty(db_session):
    settings_store.set_value(db_session, "smtp_host", "mail.x")
    settings_store.set_value(db_session, "smtp_host", "")
    assert settings_store.get_value(db_session, "smtp_host") is None


def test_runtime_config_db_overrides_env(db_session):
    settings_store.set_value(db_session, "smtp_host", "db-host")
    assert runtime_config.smtp_config()["host"] == "db-host"


# ---- generation counter / runtime_config caching ----

def test_set_value_bumps_generation(db_session):
    g0 = settings_store.generation()
    settings_store.set_value(db_session, "smtp_host", "x")
    assert settings_store.generation() > g0


def test_delete_value_bumps_generation(db_session):
    settings_store.set_value(db_session, "smtp_host", "x")
    g0 = settings_store.generation()
    settings_store.delete_value(db_session, "smtp_host")
    assert settings_store.generation() > g0


def test_smtp_config_is_cached_until_next_write(db_session, monkeypatch):
    calls = {"n": 0}
    real_get_value = settings_store.get_value

    def counting_get_value(session, key):
        calls["n"] += 1
        return real_get_value(session, key)

    monkeypatch.setattr(settings_store, "get_value", counting_get_value)

    runtime_config.smtp_config()
    after_first = calls["n"]
    runtime_config.smtp_config()
    assert calls["n"] == after_first  # served from cache, no new DB reads

    settings_store.set_value(db_session, "smtp_host", "new-host")
    assert runtime_config.smtp_config()["host"] == "new-host"
    assert calls["n"] > after_first  # cache invalidated by the write


def test_overseerr_config_is_cached_until_next_write(db_session, monkeypatch):
    calls = {"n": 0}
    real_get_value = settings_store.get_value

    def counting_get_value(session, key):
        calls["n"] += 1
        return real_get_value(session, key)

    monkeypatch.setattr(settings_store, "get_value", counting_get_value)

    runtime_config.overseerr_config()
    after_first = calls["n"]
    runtime_config.overseerr_config()
    assert calls["n"] == after_first

    settings_store.set_value(db_session, "overseerr_url", "http://ovs")
    assert runtime_config.overseerr_config()["url"] == "http://ovs"
    assert calls["n"] > after_first


# ---- access control ----

def test_settings_requires_superadmin(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "JustAdmin")
    login_as(client, admin.id)
    assert client.get("/settings", follow_redirects=False).status_code == 403


def test_settings_page_superadmin_ok(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    assert client.get("/settings").status_code == 200


# ---- SMTP ----

def test_save_smtp_keeps_password_when_blank(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    client.post("/settings/smtp", data={"smtp_host": "h", "smtp_pass": "pw", "smtp_tls": "on"})
    client.post("/settings/smtp", data={"smtp_host": "h2", "smtp_pass": ""})
    assert settings_store.get_value(db_session, "smtp_pass") == "pw"
    assert settings_store.get_value(db_session, "smtp_host") == "h2"


def test_smtp_test_route(client, db_session, login_as, monkeypatch):
    monkeypatch.setattr(mail_service, "send_email", lambda to, s, b: True)
    login_as(client, _superadmin(db_session).id)
    resp = client.post("/settings/smtp/test", data={"to": "me@example.com"})
    assert resp.status_code == 200
    assert "me@example.com" in resp.text


# ---- Telegram ----

def test_telegram_test_route(client, db_session, login_as, monkeypatch):
    monkeypatch.setattr(telegram_service, "get_me", lambda: {"username": "mybot"})
    login_as(client, _superadmin(db_session).id)
    resp = client.post("/settings/telegram/test")
    assert resp.status_code == 200
    assert "mybot" in resp.text


# ---- Plex connect flow ----

def test_plex_connect_redirects(client, db_session, login_as, monkeypatch):
    monkeypatch.setattr(po, "create_pin", lambda: {"id": 1, "code": "C"})
    login_as(client, _superadmin(db_session).id)
    resp = client.get("/settings/plex/connect", follow_redirects=False)
    assert resp.status_code == 303
    assert "app.plex.tv" in resp.headers["location"]
    assert "plex_setup_pin" in resp.cookies


def test_plex_callback_saves_token_and_lists_servers(client, db_session, login_as, monkeypatch):
    monkeypatch.setattr(po, "create_pin", lambda: {"id": 1, "code": "C"})
    login_as(client, _superadmin(db_session).id)
    client.get("/settings/plex/connect", follow_redirects=False)
    monkeypatch.setattr(po, "poll_pin", lambda pid: "admin-token")
    monkeypatch.setattr(
        po, "list_servers", lambda tok: ("owner@example.com", [{"id": "m1", "name": "HomeServer"}])
    )
    resp = client.get("/settings/plex/callback")
    assert resp.status_code == 200
    assert "HomeServer" in resp.text
    assert runtime_config.plex_config()["token"] == "admin-token"
    assert runtime_config.plex_config()["account_email"] == "owner@example.com"


def test_plex_select_and_disconnect(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    settings_store.set_value(db_session, "plex_token", "tok")
    client.post("/settings/plex/server", data={"server": "m1|HomeServer"})
    assert runtime_config.plex_config()["server_name"] == "HomeServer"
    assert runtime_config.plex_config()["server_id"] == "m1"

    client.post("/settings/plex/disconnect")
    assert runtime_config.plex_config()["token"] == ""
    assert runtime_config.plex_config()["server_name"] == ""
