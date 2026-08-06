def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # DB always probed; must pass for 200.
    assert body["checks"]["database"]["status"] == "pass"
    # Subsystems disabled in the test env (conftest) -> skipped, never fail.
    assert body["checks"]["scheduler"]["status"] == "skipped"
    assert body["checks"]["telegram_bot"]["status"] == "skipped"
    assert body["checks"]["plex"]["status"] == "skipped"


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "AccessFlow" in resp.text
    # Without this the browser probes /favicon.ico and gets a 404.
    assert 'rel="icon"' in resp.text


def test_locale_query_sets_cookie(client):
    resp = client.get("/login?locale=en")
    assert resp.status_code == 200
    assert resp.cookies.get("locale") == "en"


def test_version_hidden_from_anonymous_visitors(client, db_session, login_as):
    """The login page is public; leaking the exact release there tells an
    attacker which CVEs to try. Signed-in users still see it in the footer."""
    from app import __version__
    from app.models import AppUser, Role

    anon = client.get("/login")
    assert anon.status_code == 200
    assert "AccessFlow" in anon.text  # name is fine, it's in the title
    assert __version__ not in anon.text

    user = AppUser(role=Role.user, real_name="Someone")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    login_as(client, user.id)
    assert __version__ in client.get("/profile").text


def test_warm_plex_cache_calls_list_sections(monkeypatch):
    """The startup warm-up exists to pay Plex's cold connect off-request."""
    import app.services.plex_service as plex_service
    from app.main import warm_plex_cache

    calls = []
    monkeypatch.setattr(plex_service, "list_sections", lambda: calls.append(1))
    warm_plex_cache()
    assert calls == [1]


def test_warm_plex_cache_never_raises(monkeypatch):
    """It runs on a thread nobody awaits: an unreachable or unconfigured Plex
    must not take the process down."""
    import app.services.plex_service as plex_service
    from app.main import warm_plex_cache
    from app.services.plex_service import PlexNotConnected

    def _not_connected():
        raise PlexNotConnected("Plex is not connected")

    monkeypatch.setattr(plex_service, "list_sections", _not_connected)
    warm_plex_cache()  # must not raise

    def _boom():
        raise TimeoutError("plex.tv unreachable")

    monkeypatch.setattr(plex_service, "list_sections", _boom)
    warm_plex_cache()  # must not raise


def test_security_headers_present(client):
    resp = client.get("/login")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    # No external script/style sources allowed -> CDN inclusions are gone.
    assert "cdn.jsdelivr.net" not in resp.text
    assert "unpkg.com" not in resp.text
