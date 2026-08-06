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
    client.post("/login", data={"username": "admin", "password": "test-admin-pw"})
    resp = client.post(
        "/profile/password",
        data={"current_password": "test-admin-pw", "new_password": "newsecret1",
              "confirm_password": "newsecret1"},
    )
    assert resp.status_code == 200
    client.post("/logout")
    # New password works, old does not.
    new_ok = client.post("/login", data={"username": "admin", "password": "newsecret1"},
                         follow_redirects=False)
    assert new_ok.status_code == 303
    client.post("/logout")
    old = client.post("/login", data={"username": "admin", "password": "test-admin-pw"},
                      follow_redirects=False)
    assert old.status_code == 401
    # Restore the shared-DB password so later tests in this module still log in.
    client.post("/login", data={"username": "admin", "password": "newsecret1"})
    client.post("/profile/password",
                data={"current_password": "newsecret1", "new_password": "test-admin-pw",
                      "confirm_password": "test-admin-pw"})
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
