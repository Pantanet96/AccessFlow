from sqlmodel import select

import app.services.telegram_service as telegram_service
from app.models import AppUser, Broadcast, NotificationLog, NotificationType, Role
from app.services import broadcast as broadcast_svc
from app.services import mail_service
from app.services.telegram_link import (
    link_telegram,
    make_link_token,
    read_link_token,
)
from app.templating import _active_broadcast


def _mk(session, role, name, **kw):
    u = AppUser(role=role, real_name=name, **kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_make_and_read_link_token(db_session):
    u = _mk(db_session, Role.user, "Linker")
    token = make_link_token(u.id)
    # Telegram ?start= allows only [A-Za-z0-9_-], max 64 chars.
    assert "." not in token and len(token) <= 64
    assert read_link_token(token)["uid"] == u.id


def test_read_link_token_rejects_tampered():
    token = make_link_token(42)
    assert read_link_token(token + "x") is None


def test_read_link_token_rejects_expired():
    token = make_link_token(42)
    assert read_link_token(token, max_age=-1) is None


def test_read_link_token_rejects_wrong_binding():
    token = make_link_token(42, bind="111")
    assert read_link_token(token, bind="999") is None


def test_link_telegram_sets_chat_id(db_session):
    u = _mk(db_session, Role.user, "TG")
    token = make_link_token(u.id)
    linked = link_telegram(db_session, token, 123456)
    assert linked is not None and linked.telegram_id == "123456"


def test_link_telegram_invalid_token(db_session):
    assert link_telegram(db_session, "garbage", 1) is None


def test_broadcast_sends_to_linked_active_users(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_service, "send_message", lambda chat, text: calls.append(chat) or True
    )
    _mk(db_session, Role.user, "A", telegram_id="111")
    _mk(db_session, Role.user, "B", telegram_id="222")
    _mk(db_session, Role.user, "NoTg")  # no telegram_id -> skipped
    _mk(db_session, Role.user, "Inactive", telegram_id="333", is_active=False)

    sent = broadcast_svc.broadcast(db_session, "hello")
    assert sent == 2
    assert set(calls) == {"111", "222"}
    logs = db_session.exec(
        select(NotificationLog).where(
            NotificationLog.type == NotificationType.broadcast
        )
    ).all()
    assert len(logs) == 2


def test_broadcast_falls_back_to_email(db_session, monkeypatch):
    monkeypatch.setattr(telegram_service, "send_message", lambda chat, text: False)
    sent_emails = []
    monkeypatch.setattr(
        mail_service, "send_email",
        lambda to, subject, body, html=None: sent_emails.append(to) or True,
    )
    _mk(db_session, Role.user, "EmailOnly", notify_email="a@x.com")
    _mk(db_session, Role.user, "Neither")  # no telegram_id, no email -> skipped

    sent = broadcast_svc.broadcast(db_session, "hello")
    assert sent == 1
    assert sent_emails == ["a@x.com"]


def test_active_broadcast_role_filter_and_dismiss(db_session):
    admin = _mk(db_session, Role.admin, "Mgr")
    user = _mk(db_session, Role.user, "Usr")
    broadcast_svc.broadcast(db_session, "admins only", only_role=Role.admin)

    assert _active_broadcast(admin) is not None
    assert _active_broadcast(user) is None  # role-filtered out

    admin.dismissed_broadcast_id = _active_broadcast(admin).id
    db_session.add(admin)
    db_session.commit()
    assert _active_broadcast(admin) is None


def test_broadcast_banner_shown_and_dismissed(client, db_session, login_as):
    user = _mk(db_session, Role.user, "BannerUser")
    broadcast_svc.broadcast(db_session, "Site maintenance tonight")
    login_as(client, user.id)

    page = client.get("/")
    assert "Site maintenance tonight" in page.text

    bid = db_session.exec(select(Broadcast)).all()[-1].id
    resp = client.post(
        "/broadcast/dismiss", data={"id": bid, "next": "/"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    page2 = client.get("/")
    assert "Site maintenance tonight" not in page2.text


def test_telegram_link_redirects_to_profile(client, db_session, login_as):
    # Telegram setup moved into a /profile section; old URL just redirects.
    u = _mk(db_session, Role.user, "RouteLink")
    login_as(client, u.id)
    resp = client.get("/telegram/link", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile"
    # The connect section is rendered on the profile page (locale-independent
    # marker: the manual /start link command always appears when no bot deep link).
    page = client.get("/profile")
    assert page.status_code == 200
    assert "/start " in page.text


def test_broadcast_route_permission(client, db_session, login_as, monkeypatch):
    monkeypatch.setattr(telegram_service, "send_message", lambda chat, text: True)
    user = _mk(db_session, Role.user, "Plain")
    login_as(client, user.id)
    assert client.get("/broadcast", follow_redirects=False).status_code == 403

    admin = _mk(db_session, Role.admin, "Caster")
    login_as(client, admin.id)
    resp = client.post(
        "/broadcast", data={"message": "news"}, follow_redirects=False
    )
    assert resp.status_code == 200
