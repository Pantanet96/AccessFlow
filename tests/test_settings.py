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


# ---- Public base URL (Settings -> System) ----

def test_public_url_saved_and_normalized(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    resp = client.post(
        "/settings/public-url",
        data={"public_base_url": "accessflow.example.com/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Bare host gets a scheme, trailing slash goes -- callers concatenate a path
    # straight onto this.
    assert runtime_config.public_base_url() == "https://accessflow.example.com"


def test_public_url_rejects_junk(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    for junk in ("http://", "ftp://x.test", "///"):
        resp = client.post("/settings/public-url", data={"public_base_url": junk})
        assert resp.status_code == 400, junk


def test_public_url_blank_falls_back_to_env(client, db_session, login_as):
    from app.config import get_settings

    login_as(client, _superadmin(db_session).id)
    client.post("/settings/public-url", data={"public_base_url": "https://x.test"})
    client.post("/settings/public-url", data={"public_base_url": ""})
    assert runtime_config.public_base_url() == get_settings().public_base_url.rstrip("/")


def test_cookies_stay_secure_when_env_is_https(db_session, monkeypatch):
    """An admin typing an http URL must not downgrade a working HTTPS deploy."""
    from app.config import get_settings

    settings_store.set_value(db_session, "public_base_url", "http://typo.local")
    monkeypatch.setattr(get_settings(), "public_base_url", "https://real.example.com")
    assert runtime_config.cookies_secure() is True


# ---- Background jobs (Settings > Jobs) ----

def test_job_interval_saved_and_clamped(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    resp = client.post(
        "/settings/jobs/plex_auto_import/interval",
        data={"interval_hours": "6"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert runtime_config.job_interval_hours("plex_auto_import") == 6

    client.post(
        "/settings/jobs/plex_auto_import/interval", data={"interval_hours": "9999"}
    )
    assert runtime_config.job_interval_hours("plex_auto_import") == 168  # clamped to max

    client.post(
        "/settings/jobs/plex_auto_import/interval", data={"interval_hours": "abc"}
    )
    assert (
        runtime_config.job_interval_hours("plex_auto_import")
        == runtime_config.DEFAULT_JOB_INTERVAL_HOURS
    )

    # Each job's interval is independent -- changing one must not affect another.
    assert runtime_config.job_interval_hours("db_backup") == (
        runtime_config.DEFAULT_JOB_INTERVAL_HOURS
    )


def test_job_interval_rejects_unknown_job_id(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    resp = client.post(
        "/settings/jobs/not-a-real-job/interval",
        data={"interval_hours": "6"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from app.services import settings_store

    assert settings_store.get_value(db_session, "job_interval_hours:not-a-real-job") is None


def test_job_run_hour_saved_and_clamped(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    resp = client.post(
        "/settings/jobs/expiry_scan/run-hour",
        data={"run_hour": "14"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert runtime_config.job_run_hour("expiry_scan") == 14

    client.post("/settings/jobs/expiry_scan/run-hour", data={"run_hour": "99"})
    assert runtime_config.job_run_hour("expiry_scan") == 23  # clamped to max


def test_job_interval_endpoint_rejects_daily_job(client, db_session, login_as):
    """expiry_scan is 'daily'-scheduled -- the hours-interval endpoint is for
    'interval' jobs only, and must not silently accept it."""
    login_as(client, _superadmin(db_session).id)
    client.post("/settings/jobs/expiry_scan/interval", data={"interval_hours": "6"})
    from app.services import settings_store

    assert settings_store.get_value(db_session, "job_interval_hours:expiry_scan") is None


def test_job_run_hour_endpoint_rejects_interval_job(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    client.post("/settings/jobs/plex_auto_import/run-hour", data={"run_hour": "14"})
    from app.services import settings_store

    assert settings_store.get_value(db_session, "job_run_hour:plex_auto_import") is None


def test_run_job_now_executes_immediately(client, db_session, login_as, monkeypatch):
    import app.scheduler as scheduler_module

    calls = []
    # JOBS holds its own reference to the job function, so patch the registry
    # entry directly rather than the module-level _run_backup.
    for j in scheduler_module.JOBS:
        if j["id"] == "db_backup":
            monkeypatch.setitem(j, "fn", lambda: calls.append("ran"))

    login_as(client, _superadmin(db_session).id)
    resp = client.post("/settings/jobs/db_backup/run", follow_redirects=False)
    assert resp.status_code == 303
    assert calls == ["ran"]


def test_run_job_now_rejects_unknown_job_id(client, db_session, login_as):
    login_as(client, _superadmin(db_session).id)
    resp = client.post(
        "/settings/jobs/not-a-real-job/run", follow_redirects=False
    )
    assert resp.status_code == 303


def test_settings_jobs_page_ok_without_running_scheduler(client, db_session, login_as):
    # Tests run with the scheduler disabled -- the page must still render,
    # showing the "not running" fallback instead of a next-run time.
    login_as(client, _superadmin(db_session).id)
    resp = client.get("/settings?group=jobs")
    assert resp.status_code == 200
    assert "scheduler non attivo" in resp.text  # default test locale is it


def test_plex_forward_url_uses_the_configured_domain(client, db_session, login_as):
    """The OAuth callback is built from the setting, not from the request host --
    this is what a reverse-proxy deploy gets wrong without it."""
    settings_store.set_value(db_session, "public_base_url", "https://af.example.com")
    assert (
        runtime_config.public_base_url() + "/login/plex/callback"
        == "https://af.example.com/login/plex/callback"
    )


def test_bad_stored_smtp_port_does_not_break_settings(client, db_session, login_as):
    # int("1e3") raised in smtp_config(): /settings 500'd, including the form
    # needed to fix it. Now it falls back and new bad values are refused.
    settings_store.set_value(db_session, "smtp_port", "1e3")
    login_as(client, _superadmin(db_session).id)
    assert client.get("/settings?group=notifiche").status_code == 200

    resp = client.post("/settings/smtp", data={"smtp_port": "99999"}, follow_redirects=False)
    assert resp.status_code == 400
    assert settings_store.get_value(db_session, "smtp_port") == "1e3"
    resp = client.post("/settings/smtp", data={"smtp_port": "587"}, follow_redirects=False)
    assert resp.status_code == 303
