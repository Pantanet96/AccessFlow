from app.auth.session import COOKIE_NAME


def test_login_success_sets_cookie(client):
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "test-admin-pw"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert COOKIE_NAME in resp.cookies


def test_login_bad_password(client):
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert COOKIE_NAME not in resp.cookies


def test_protected_route_redirects_anonymous(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_change_password_wrong_current_rejected(client):
    client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
    resp = client.post(
        "/profile/password",
        data={"current_password": "nope", "new_password": "newsecret1",
              "confirm_password": "newsecret1"},
    )
    assert resp.status_code == 200
    # Old password still works.
    client.post("/logout")
    ok = client.post("/login", data={"username": "admin", "password": "test-admin-pw"},
                     follow_redirects=False)
    assert ok.status_code == 303


def test_change_password_success(client):
    new = "a-longer-new-secret"
    client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
    resp = client.post(
        "/profile/password",
        data={"current_password": "test-admin-pw", "new_password": new,
              "confirm_password": new},
    )
    assert resp.status_code == 200
    client.post("/logout")
    # New password works, old does not.
    new_ok = client.post("/login", data={"username": "admin", "password": new},
                         follow_redirects=False)
    assert new_ok.status_code == 303
    client.post("/logout")
    old = client.post("/login", data={"username": "admin", "password": "test-admin-pw"},
                      follow_redirects=False)
    assert old.status_code == 401
    # Restore the shared-DB password so later tests in this module still log in.
    # Straight to the DB: the fixture password no longer passes the policy.
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import AppUser
    from app.security import hash_password

    with Session(engine) as s:
        admin = s.exec(select(AppUser).where(AppUser.username == "admin")).one()
        admin.password_hash = hash_password("test-admin-pw")
        s.add(admin)
        s.commit()


def test_password_policy_on_change(client):
    client.cookies.set("locale", "en")
    client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
    for bad, why in (("short-pw", "at least 12"), ("myadminpassword", "username"),
                     ("password1234", "too common")):
        resp = client.post("/profile/password", data={
            "current_password": "test-admin-pw", "new_password": bad,
            "confirm_password": bad})
        assert why in resp.text, bad
    client.post("/logout")


def test_weak_password_banner_for_superadmin(client):
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import AppUser

    client.cookies.set("locale", "en")
    # "test-admin-pw" contains the username: flagged at login, banner shown.
    client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
    assert "does not meet the requirements" in client.get("/").text
    with Session(engine) as s:
        assert s.exec(select(AppUser).where(AppUser.username == "admin")).one().password_weak
    client.post("/logout")


def test_protected_route_ok_when_logged_in(client):
    client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Admin" in resp.text


def test_logout_clears_session(client):
    client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
    client.post("/logout")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


def test_username_lock_does_not_block_correct_password(client):
    # A username locked from OTHER IPs must not deny the real user: a correct
    # password from an unlocked IP still logs in (M9 lockout-DoS mitigation).
    import app.auth.throttle as throttle
    throttle._BUCKETS.clear()
    for i in range(throttle.MAX_FAILS):
        throttle.register_failure("admin", f"9.9.9.{i}")
    assert throttle.check_locked("admin", "0.0.0.0") is not None  # username locked
    resp = client.post(
        "/login", data={"username": "admin", "password": "test-admin-pw"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    throttle._BUCKETS.clear()


def test_password_problem():
    from app.security import password_problem

    assert password_problem("a-perfectly-fine-one", "admin") is None
    assert password_problem("short", "admin")
    assert password_problem("x" * 12 + "ü" * 31, "")  # 74 bytes: past bcrypt's limit
    assert password_problem("MyAdmin-password", "admin")
    assert password_problem("Password1234", "")
    assert password_problem("abababababab", "")


def test_leftover_initial_password_file_banner(client):
    from app.seed import initial_password_file

    client.cookies.set("locale", "en")
    client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
    assert "INITIAL_SUPERADMIN_PASSWORD.txt" not in client.get("/").text
    f = initial_password_file()
    f.write_text("x")
    try:
        assert "INITIAL_SUPERADMIN_PASSWORD.txt is still" in client.get("/").text
    finally:
        f.unlink()
    client.post("/logout")


# ---- __Host- cookies on https deploys ----

CREDS = {"username": "admin", "password": "test-admin-pw"}


def _https(db_session):
    from app.services import settings_store

    settings_store.set_value(db_session, "public_base_url", "https://af.example.com")


def _admin_token(db_session):
    from sqlmodel import select

    from app.auth.session import make_token
    from app.models import AppUser

    admin = db_session.exec(select(AppUser).where(AppUser.username == "admin")).one()
    return make_token(admin.id, admin.session_gen or 0)


def _set_cookie(resp, name):
    return next(c for c in resp.headers.get_list("set-cookie") if c.startswith(name + "="))


def test_https_login_sets_host_prefixed_session_cookie(client, db_session):
    _https(db_session)
    c = _set_cookie(client.post("/login", data=CREDS, follow_redirects=False),
                    "__Host-pum_session")
    assert "Secure" in c and "Path=/" in c and "Domain" not in c


def test_https_ignores_unprefixed_session_cookie(client, db_session):
    # A sibling subdomain can set "pum_session" on the parent domain, never "__Host-pum_session".
    _https(db_session)
    token = _admin_token(db_session)
    client.cookies.set("pum_session", token)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    client.cookies.clear()
    client.cookies.set("__Host-pum_session", token)
    assert client.get("/", follow_redirects=False).status_code == 200


def test_https_logout_deletes_cookie_with_secure(client, db_session):
    _https(db_session)
    client.cookies.set("__Host-pum_session", _admin_token(db_session))
    c = _set_cookie(client.post("/logout", follow_redirects=False), "__Host-pum_session")
    assert "Max-Age=0" in c and "Secure" in c


def test_https_plex_pin_cookie_is_host_prefixed(client, db_session, monkeypatch):
    import app.services.plex_oauth as po

    _https(db_session)
    monkeypatch.setattr(po, "create_pin", lambda: {"id": 42, "code": "ABCD"})
    resp = client.get("/login/plex", follow_redirects=False)
    assert "Secure" in _set_cookie(resp, "__Host-plex_pin")
