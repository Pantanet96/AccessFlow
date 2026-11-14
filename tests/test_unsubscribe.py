# -*- coding: utf-8 -*-
import pytest
from sqlmodel import select

from app.models import AppUser, AuditLog, Role
from app.services import broadcast as broadcast_svc
from app.services import mail_service, notifications, settings_store, telegram_service
from app.services import notification_templates as nt
from app.services import unsubscribe

BASE = "https://af.example.test"


def _user(session, name="Mario", email="mario@example.com", **kw):
    u = AppUser(role=Role.user, real_name=name, plex_email=email, **kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


@pytest.fixture
def public_url(db_session):
    settings_store.set_value(db_session, "public_base_url", BASE)
    yield BASE
    settings_store.delete_value(db_session, "public_base_url")


def _fresh(session, user):
    session.expire_all()
    return session.get(AppUser, user.id)


# ---- token ----

def test_token_round_trip(db_session):
    u = _user(db_session)
    assert unsubscribe.read_token(db_session, unsubscribe.make_token(u)).id == u.id


@pytest.mark.parametrize("bad", ["", "garbage", "a.b.c", "x" * 200])
def test_a_forged_token_names_nobody(db_session, bad):
    assert unsubscribe.read_token(db_session, bad) is None


def test_a_tampered_token_is_refused(db_session):
    token = unsubscribe.make_token(_user(db_session))
    # A character in the MIDDLE of the signature. The last one carries only
    # 4 useful bits of the final byte, so swapping it can decode to the very
    # same signature -- a test that flipped it failed for 1 token in 16.
    payload, sig = token.rsplit(".", 1)
    mid = len(sig) // 2
    swapped = sig[:mid] + ("A" if sig[mid] != "A" else "B") + sig[mid + 1:]
    assert unsubscribe.read_token(db_session, payload + "." + swapped) is None


def test_a_token_dies_when_the_notification_address_changes(db_session):
    # A link from a forwarded or leaked message must stop working once the
    # user's notifications go somewhere else.
    u = _user(db_session)
    token = unsubscribe.make_token(u)
    u.notify_email = "new@example.com"
    db_session.add(u)
    db_session.commit()
    assert unsubscribe.read_token(db_session, token) is None


def test_a_token_for_one_user_does_not_open_another(db_session):
    a = _user(db_session, "A", "a@example.com")
    b = _user(db_session, "B", "b@example.com")
    ta, tb = unsubscribe.make_token(a), unsubscribe.make_token(b)
    # A's payload under B's signature: the signature covers the payload.
    crossed = ta.rsplit(".", 1)[0] + "." + tb.rsplit(".", 1)[1]
    assert unsubscribe.read_token(db_session, crossed) is None
    assert unsubscribe.read_token(db_session, ta).id == a.id


def test_no_url_while_the_public_address_is_local(db_session):
    # A List-Unsubscribe pointing at localhost is worse than none: a provider
    # that cannot reach it holds the failure against the sender.
    u = _user(db_session)
    settings_store.set_value(db_session, "public_base_url", "http://localhost:8000")
    assert unsubscribe.url_for(u) == ""
    settings_store.set_value(db_session, "public_base_url", BASE)
    assert unsubscribe.url_for(u).startswith(BASE + "/unsubscribe/")
    settings_store.delete_value(db_session, "public_base_url")


# ---- the endpoint ----

def test_get_shows_the_confirmation_and_changes_nothing(client, db_session):
    # Mail scanners and link previewers fetch every URL in a message.
    u = _user(db_session)
    resp = client.get("/unsubscribe/" + unsubscribe.make_token(u))
    assert resp.status_code == 200
    assert 'method="post"' in resp.text
    assert _fresh(db_session, u).notify_via_email is True


def test_one_click_post_works_with_no_browser_headers(client, db_session):
    # What Gmail sends: a bare POST from its own servers, with neither Origin
    # nor Sec-Fetch-Site. The CSRF check refuses that everywhere else.
    u = _user(db_session)
    del client.headers["origin"]
    resp = client.post("/unsubscribe/" + unsubscribe.make_token(u),
                       data={"List-Unsubscribe": "One-Click"})
    assert resp.status_code == 200
    assert _fresh(db_session, u).notify_via_email is False


def test_the_csrf_exemption_stops_at_the_unsubscribe_path(client, db_session):
    del client.headers["origin"]
    assert client.post("/settings/smtp", data={"smtp_host": "x"}).status_code == 403
    assert client.post("/logout").status_code == 403
    # A prefix look-alike is not the exempt path.
    assert client.post("/unsubscribed/whatever").status_code == 403


@pytest.mark.parametrize("method", ["get", "post"])
def test_an_invalid_link_is_a_404_that_touches_nobody(client, db_session, method):
    u = _user(db_session)
    resp = getattr(client, method)("/unsubscribe/not-a-token")
    assert resp.status_code == 404
    assert _fresh(db_session, u).notify_via_email is True


def test_a_repeated_post_is_harmless_and_logged_once(client, db_session):
    # A provider retries its POST; a person double-clicks.
    u = _user(db_session)
    url = "/unsubscribe/" + unsubscribe.make_token(u)
    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 200
    rows = db_session.exec(
        select(AuditLog).where(AuditLog.action == "unsubscribe_email")).all()
    assert len(rows) == 1 and rows[0].actor_id == u.id


def test_the_link_acts_on_its_own_user_whoever_is_signed_in(client, db_session, login_as):
    # The token is the only authority: the session cookie is never consulted,
    # which is what makes the CSRF exemption safe.
    admin = db_session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).first()
    other = _user(db_session, "Altro", "altro@example.com")
    login_as(client, admin.id)
    client.post("/unsubscribe/" + unsubscribe.make_token(other))
    assert _fresh(db_session, other).notify_via_email is False
    assert _fresh(db_session, admin).notify_via_email is True


def test_an_unsubscribed_user_gets_no_more_email(client, db_session, monkeypatch, public_url):
    sent = []
    monkeypatch.setattr(mail_service, "send_email",
                        lambda to, s, b, html=None, **kw: sent.append(to) or True)
    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: True)
    u = _user(db_session)
    client.post("/unsubscribe/" + unsubscribe.make_token(u))
    u = _fresh(db_session, u)
    assert notifications._send_email(
        db_session, recipient=u, sub_id=None, ntype=None, dedup_key="k",
        type_="welcome", ctx=dict(nt.SAMPLE_CTX)) is False
    assert sent == []


# ---- the headers ----

_CFG = dict(host="mx.example.com", port=587, user="", password="",
            from_addr="noreply@accessflow.test", from_name="", tls=True)


def test_https_link_gets_both_headers():
    msg = mail_service._build(_CFG, "u@x.test", "s", "b", None, BASE + "/unsubscribe/t")
    assert msg["List-Unsubscribe"] == "<%s/unsubscribe/t>" % BASE
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_http_link_gets_the_plain_header_only():
    # RFC 8058 requires https for one-click; a provider that sees the marker on
    # a plain-http link ignores the pair.
    msg = mail_service._build(_CFG, "u@x.test", "s", "b", None,
                              "http://af.lan/unsubscribe/t")
    assert msg["List-Unsubscribe"] == "<http://af.lan/unsubscribe/t>"
    assert msg["List-Unsubscribe-Post"] is None


@pytest.mark.parametrize("url", [
    None, "", "ftp://x/y", "/unsubscribe/t",
    "https://x.test/a b", "https://x.test/a>\r\nBcc: v@x.test",
    'https://x.test/"quoted"',
])
def test_an_unusable_link_adds_no_header_and_does_not_raise(url):
    msg = mail_service._build(_CFG, "u@x.test", "s", "b", None, url)
    assert msg["List-Unsubscribe"] is None and msg["List-Unsubscribe-Post"] is None


# ---- who gets it ----

def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mail_service, "send_email",
        lambda to, subject, body, html=None, **kw: calls.append(
            dict(to=to, body=body, html=html, **kw)) or True)
    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: True)
    return calls


def test_a_notification_carries_the_link_in_header_and_footer(db_session, monkeypatch, public_url):
    calls = _capture(monkeypatch)
    u = _user(db_session)
    notifications._send_email(
        db_session, recipient=u, sub_id=None,
        ntype=notifications.NotificationType.welcome, dedup_key="w:1",
        type_="welcome", ctx=dict(nt.SAMPLE_CTX))
    (call,) = calls
    url = call["unsubscribe_url"]
    assert url.startswith(BASE + "/unsubscribe/")
    assert unsubscribe.read_token(db_session, url.rsplit("/", 1)[1]).id == u.id
    # Visible too: a text-only client shows no header button.
    assert url in call["html"] and "Non ricevere più queste email" in call["html"]
    assert url in call["body"]


def test_no_link_anywhere_while_the_public_address_is_local(db_session, monkeypatch):
    calls = _capture(monkeypatch)
    settings_store.set_value(db_session, "public_base_url", "http://localhost:8000")
    notifications._send_email(
        db_session, recipient=_user(db_session), sub_id=None,
        ntype=notifications.NotificationType.welcome, dedup_key="w:2",
        type_="welcome", ctx=dict(nt.SAMPLE_CTX))
    (call,) = calls
    assert call["unsubscribe_url"] is None
    assert "/unsubscribe/" not in call["html"] and "/unsubscribe/" not in call["body"]
    settings_store.delete_value(db_session, "public_base_url")


def test_a_broadcast_carries_the_header(db_session, monkeypatch, public_url):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: False)
    u = _user(db_session, notify_email="b@example.com")
    broadcast_svc.broadcast(db_session, "ciao")
    mine = [c for c in calls if c["to"] == "b@example.com"]
    assert mine and mine[0]["unsubscribe_url"].startswith(BASE + "/unsubscribe/")
    assert unsubscribe.read_token(
        db_session, mine[0]["unsubscribe_url"].rsplit("/", 1)[1]).id == u.id


def test_the_footer_link_is_escaped(db_session, public_url):
    # Rendered with autoescape off, so the escaping is ours to do.
    _, html, _ = nt.render_email(db_session, "welcome", "it", dict(nt.SAMPLE_CTX),
                                 unsubscribe_url=BASE + '/unsubscribe/a"><b>')
    assert '"><b>' not in html and "&quot;&gt;&lt;b&gt;" in html


# ---- on the wire ----

def _wire(msg) -> str:
    """The bytes smtplib actually sends, not the value held in memory."""
    import io as _io
    from email.generator import BytesGenerator

    buf = _io.BytesIO()
    BytesGenerator(buf, policy=msg.policy.clone(linesep="\r\n")).flatten(msg)
    return buf.getvalue().decode()


def test_the_header_reaches_the_wire_unencoded(db_session):
    """The stdlib cannot fold a URL (no spaces) and rewrites it as RFC 2047
    encoded words instead: `=?utf-8?q?=3Chttps...`. No provider parses that,
    so the button never appears -- while msg["List-Unsubscribe"] still reads
    fine in memory, which is why only a test on the serialised bytes sees it."""
    u = _user(db_session)
    url = BASE + "/unsubscribe/" + unsubscribe.make_token(u)
    assert len(url) > 78  # long enough to trigger the folding
    wire = _wire(mail_service._build(_CFG, "u@x.test", "s", "b", None, url))
    assert "List-Unsubscribe: <%s>\r\n" % url in wire
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n" in wire
    assert "=?utf-8?" not in wire.split("\r\n\r\n", 1)[0]


def test_other_headers_still_fold_as_before(db_session):
    # Only List-Unsubscribe is exempt from folding.
    wire = _wire(mail_service._build(_CFG, "u@x.test", "parola " * 30, "b", None))
    subject_lines = [ln for ln in wire.split("\r\n")[:12] if "parola" in ln]
    assert len(subject_lines) > 1 and all(len(ln) <= 78 for ln in subject_lines)


# ---- the exemption stays narrow ----

@pytest.mark.parametrize("method", ["put", "delete", "patch"])
def test_only_post_is_exempt(client, db_session, method):
    del client.headers["origin"]
    url = "/unsubscribe/" + unsubscribe.make_token(_user(db_session))
    assert getattr(client, method)(url).status_code == 403


def test_nothing_else_lives_under_the_exempt_path(client):
    """A route added under /unsubscribe/ would be reachable without the CSRF
    check if the exemption ever went back to a prefix match. Fail loudly the
    day one appears, so that it is a decision and not an accident."""
    # From the OpenAPI schema: included routers are wrapped objects in
    # app.routes, and the schema lists every path however it was mounted.
    paths = client.app.openapi()["paths"]
    found = {p: sorted(ops) for p, ops in paths.items() if p.startswith("/unsubscribe")}
    assert found == {"/unsubscribe/{token}": ["get", "post"]}


def test_a_deeper_path_is_not_exempt(client, db_session):
    del client.headers["origin"]
    token = unsubscribe.make_token(_user(db_session))
    assert client.post("/unsubscribe/%s/extra" % token).status_code == 403


def test_the_post_can_only_switch_off(client, db_session):
    u = _user(db_session, notify_via_email=False)
    assert client.post("/unsubscribe/" + unsubscribe.make_token(u)).status_code == 200
    assert _fresh(db_session, u).notify_via_email is False  # never back on


def test_the_page_is_never_cached(client, db_session):
    resp = client.get("/unsubscribe/" + unsubscribe.make_token(_user(db_session)))
    assert resp.headers["cache-control"] == "no-store"


def test_one_click_accepts_a_multipart_body(client, db_session):
    # The form RFC 8058 recommends.
    u = _user(db_session)
    del client.headers["origin"]
    resp = client.post("/unsubscribe/" + unsubscribe.make_token(u),
                       files={"List-Unsubscribe": (None, "One-Click")})
    assert resp.status_code == 200
    assert _fresh(db_session, u).notify_via_email is False


# ---- who does not get it ----

def test_an_invite_carries_no_unsubscribe_link(db_session, monkeypatch, public_url):
    # The invitee has no account, so there is no preference to switch off.
    from datetime import timedelta

    from app.models import Invite, utcnow

    calls = _capture(monkeypatch)
    inv = Invite(email="new@example.com", real_name="Nuovo", intended_role=Role.user,
                 token="t-unsub", expires_at=utcnow() + timedelta(days=7))
    db_session.add(inv)
    db_session.commit()
    notifications.notify_invite(db_session, inv)
    (call,) = calls
    assert "unsubscribe_url" not in call
    assert "/unsubscribe/" not in call["html"] and "/unsubscribe/" not in call["body"]


def test_a_broadcast_shows_the_link_in_its_plain_body(db_session, monkeypatch, public_url):
    # A broadcast is plain text with no footer of its own.
    calls = _capture(monkeypatch)
    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: False)
    _user(db_session, notify_email="b2@example.com")
    broadcast_svc.broadcast(db_session, "ciao a tutti")
    mine = [c for c in calls if c["to"] == "b2@example.com"][0]
    assert mine["body"].startswith("ciao a tutti")
    assert mine["unsubscribe_url"] in mine["body"]
