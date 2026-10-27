from app.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.smtp_port == 587
    assert s.smtp_tls is True
    assert s.default_locale == "it"
    assert s.notify_hour == 9


def test_env_override(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("DEFAULT_LOCALE", "en")
    s = Settings(_env_file=None)
    assert s.smtp_port == 2525
    assert s.default_locale == "en"


def test_insecure_secret_on_https_starts_with_warning(monkeypatch, caplog):
    # 1.5.4 refused to start here: a breaking change for deploys already running
    # like this. Changing the key by hand loses every encrypted setting, so the
    # app starts and says so; the fix is a key rotation from Settings.
    import logging

    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "public_base_url", "https://af.example.com")
    with caplog.at_level(logging.WARNING, logger="pum.startup"):
        with TestClient(create_app()) as c:
            assert c.get("/healthz").status_code == 200
    assert "ALLOW_INSECURE_SECRET" in caplog.text
