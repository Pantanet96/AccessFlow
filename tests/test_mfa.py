import re
import time

import pytest
from sqlmodel import Session, select

from app.auth import mfa
from app.auth.session import COOKIE_NAME
from app.db import engine
from app.models import AppUser

PW = "test-admin-pw"


def _admin_id():
    with Session(engine) as s:
        return s.exec(select(AppUser).where(AppUser.username == "admin")).one().id


def _code(secret, offset=0):
    return mfa._code(secret, int(time.time() // mfa.STEP) + offset)


@pytest.fixture
def admin_mfa_off():
    # Shared seeded admin: never leave MFA on for the tests that follow.
    uid = _admin_id()
    yield uid
    with Session(engine) as s:
        mfa.disable(s, uid)


def test_totp_matches_rfc6238_and_refuses_replay():
    # RFC 6238 appendix B, SHA1, T=59s -> 94287082 (last 6 digits).
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert mfa._code(secret, 1) == "287082"
    assert mfa.match_step(secret, "287082", now=59) == 1
    assert mfa.match_step(secret, "287082", after=1, now=59) is None  # used once
    assert mfa.match_step(secret, "000000", now=59) is None
    assert mfa.match_step(secret, "28708", now=59) is None


def test_enable_then_login_needs_code(client, admin_mfa_off):
    uid = admin_mfa_off
    client.cookies.set("locale", "en")
    client.post("/login", data={"username": "admin", "password": PW})
    page = client.get("/profile/mfa").text
    assert "<svg" in page
    with Session(engine) as s:
        secret = mfa.pending_secret(s, uid)

    # Wrong password: stays off even with a good code.
    bad = client.post("/profile/mfa/enable",
                      data={"current_password": "nope", "code": _code(secret)})
    assert bad.status_code == 400
    with Session(engine) as s:
        assert not mfa.enabled(s, uid)

    done = client.post("/profile/mfa/enable",
                       data={"current_password": PW, "code": _code(secret)})
    assert done.status_code == 200
    codes = re.findall(r"\b[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}\b", done.text)
    assert len(codes) == mfa.RECOVERY_CODES
    client.post("/logout")

    # Password alone: no session, just the code prompt.
    resp = client.post("/login", data={"username": "admin", "password": PW},
                       follow_redirects=False)
    assert resp.headers["location"] == "/login/mfa"
    assert COOKIE_NAME not in resp.cookies
    assert client.get("/", follow_redirects=False).status_code == 303

    assert client.post("/login/mfa", data={"code": "000000"}).status_code == 401
    ok = client.post("/login/mfa", data={"code": _code(secret, 1)}, follow_redirects=False)
    assert ok.status_code == 303 and COOKIE_NAME in ok.cookies
    client.post("/logout")

    # A recovery code works once.
    for expected in (303, 401):
        client.post("/login", data={"username": "admin", "password": PW})
        r = client.post("/login/mfa", data={"code": codes[0]}, follow_redirects=False)
        assert r.status_code == expected
    client.post("/logout")


def test_code_step_without_password_step_is_refused(client, admin_mfa_off):
    # /login/mfa is only reachable with the signed cookie the password step sets.
    resp = client.post("/login/mfa", data={"code": "123456"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login/local"


def test_env_reset_turns_mfa_off(admin_mfa_off, monkeypatch):
    from app.config import get_settings
    from app.seed import seed_superadmin

    uid = admin_mfa_off
    with Session(engine) as s:
        secret = mfa.start_setup(s, uid)
        assert mfa.enable(s, uid, _code(secret)) is not None
        monkeypatch.setattr(get_settings(), "superadmin_mfa_reset", True)
        seed_superadmin(s)
        assert not mfa.enabled(s, uid)


def test_disable_needs_password_and_code(client, admin_mfa_off):
    uid = admin_mfa_off
    with Session(engine) as s:
        secret = mfa.start_setup(s, uid)
        codes = mfa.enable(s, uid, _code(secret))
    client.post("/login", data={"username": "admin", "password": PW})
    client.post("/login/mfa", data={"code": _code(secret, 1)})

    client.post("/profile/mfa/disable", data={"current_password": PW, "code": "000000"})
    with Session(engine) as s:
        assert mfa.enabled(s, uid)
    client.post("/profile/mfa/disable", data={"current_password": "nope", "code": _code(secret, -1)})
    with Session(engine) as s:
        assert mfa.enabled(s, uid)
    # The TOTP code just used at login is spent; a recovery code does it.
    client.post("/profile/mfa/disable", data={"current_password": PW, "code": codes[0]})
    with Session(engine) as s:
        assert not mfa.enabled(s, uid)
    client.post("/logout")
