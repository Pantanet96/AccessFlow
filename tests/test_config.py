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
