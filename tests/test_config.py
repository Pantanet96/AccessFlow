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


def test_insecure_secret_flag_ignored_on_https():
    def weak(url):
        return Settings(_env_file=None, app_secret_key="short",
                        allow_insecure_secret=True, public_base_url=url)

    assert weak("http://localhost:8000").secret_refused() is False
    assert weak("https://af.example.com").secret_refused() is True
    strong = Settings(_env_file=None, app_secret_key="x" * 32,
                      public_base_url="https://af.example.com")
    assert strong.secret_refused() is False
