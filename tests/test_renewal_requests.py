from sqlmodel import select

from app.models import AppUser, Renewal, RenewalStatus, Role
from app.services import subscriptions as svc


def _mk(session, role, name, manager_id=None):
    u = AppUser(role=role, real_name=name, manager_id=manager_id)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _plan(session, slug):
    from app.models import Plan

    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def test_user_dashboard_shows_subscription(client, db_session, login_as):
    user = _mk(db_session, Role.user, "Dash")
    svc.create_subscription(db_session, user, _plan(db_session, "bronze"))
    login_as(client, user.id)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Bronze" in resp.text
    assert "Richiedi rinnovo" in resp.text or "Request renewal" in resp.text


def test_user_can_request_renewal(client, db_session, login_as):
    mgr = _mk(db_session, Role.moderator, "Mgr")
    user = _mk(db_session, Role.user, "Req", manager_id=mgr.id)
    sub = svc.create_subscription(db_session, user, _plan(db_session, "bronze"))
    login_as(client, user.id)
    resp = client.post(
        f"/subscriptions/{sub.id}/request-renewal",
        data={"causale": "PayPal 12/06"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    r = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).one()
    assert r.status == RenewalStatus.pending
    assert r.causale == "PayPal 12/06"
    assert r.created_by == user.id


def test_user_cannot_request_for_others_sub(client, db_session, login_as):
    owner = _mk(db_session, Role.user, "Owner")
    other = _mk(db_session, Role.user, "Other")
    sub = svc.create_subscription(db_session, owner, _plan(db_session, "bronze"))
    login_as(client, other.id)
    resp = client.post(
        f"/subscriptions/{sub.id}/request-renewal", follow_redirects=False
    )
    assert resp.status_code == 403


def test_no_duplicate_pending_request(client, db_session, login_as):
    user = _mk(db_session, Role.user, "Dup")
    sub = svc.create_subscription(db_session, user, _plan(db_session, "bronze"))
    login_as(client, user.id)
    client.post(f"/subscriptions/{sub.id}/request-renewal")
    client.post(f"/subscriptions/{sub.id}/request-renewal")
    db_session.commit()
    rows = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).all()
    assert len(rows) == 1


def test_manager_sees_and_validates_request(client, db_session, login_as):
    mgr = _mk(db_session, Role.moderator, "ModV")
    user = _mk(db_session, Role.user, "Member", manager_id=mgr.id)
    sub = svc.create_subscription(db_session, user, _plan(db_session, "bronze"))
    r = svc.create_renewal(db_session, sub, actor_id=user.id, collected_by=mgr.id)

    login_as(client, mgr.id)
    page = client.get("/requests")
    assert page.status_code == 200
    assert "Member" in page.text

    # validate via existing pay route
    resp = client.post(
        f"/renewals/{r.id}/pay", data={"causale": "confirmed"}, follow_redirects=False
    )
    assert resp.status_code == 303
    db_session.commit()
    db_session.refresh(r)
    assert r.status == RenewalStatus.paid


def test_user_can_cancel_own_request(client, db_session, login_as):
    user = _mk(db_session, Role.user, "Canceller")
    sub = svc.create_subscription(db_session, user, _plan(db_session, "bronze"))
    r = svc.create_renewal(db_session, sub, actor_id=user.id, collected_by=None)
    login_as(client, user.id)
    resp = client.post(f"/renewals/{r.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    db_session.expunge_all()
    assert db_session.get(Renewal, r.id) is None


def test_user_cannot_delete_others_request(client, db_session, login_as):
    owner = _mk(db_session, Role.user, "OwnerR")
    other = _mk(db_session, Role.user, "OtherR")
    sub = svc.create_subscription(db_session, owner, _plan(db_session, "bronze"))
    r = svc.create_renewal(db_session, sub, actor_id=owner.id, collected_by=None)
    login_as(client, other.id)
    assert client.post(f"/renewals/{r.id}/delete", follow_redirects=False).status_code == 403


def test_manager_can_delete_request(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModDel")
    user = _mk(db_session, Role.user, "MemberDel", manager_id=mod.id)
    sub = svc.create_subscription(db_session, user, _plan(db_session, "bronze"))
    r = svc.create_renewal(db_session, sub, actor_id=user.id, collected_by=mod.id)
    login_as(client, mod.id)
    resp = client.post(f"/renewals/{r.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/requests"
    db_session.expunge_all()
    assert db_session.get(Renewal, r.id) is None


def test_cannot_delete_paid_renewal(client, db_session, login_as):
    user = _mk(db_session, Role.user, "PaidDel")
    sub = svc.create_subscription(db_session, user, _plan(db_session, "bronze"))
    r = svc.create_renewal(db_session, sub, actor_id=user.id, collected_by=None)
    svc.mark_renewal_paid(db_session, r, causale="done")
    login_as(client, user.id)
    assert client.post(f"/renewals/{r.id}/delete", follow_redirects=False).status_code == 400


def test_requests_page_forbidden_for_user(client, db_session, login_as):
    user = _mk(db_session, Role.user, "Plain")
    login_as(client, user.id)
    assert client.get("/requests", follow_redirects=False).status_code == 403


def test_moderator_requests_scoped(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ScopeMod")
    mine = _mk(db_session, Role.user, "Mine", manager_id=mod.id)
    notmine = _mk(db_session, Role.user, "NotMine")
    s1 = svc.create_subscription(db_session, mine, _plan(db_session, "bronze"))
    s2 = svc.create_subscription(db_session, notmine, _plan(db_session, "bronze"))
    svc.create_renewal(db_session, s1, actor_id=mine.id, collected_by=mod.id)
    svc.create_renewal(db_session, s2, actor_id=notmine.id, collected_by=None)
    login_as(client, mod.id)
    page = client.get("/requests")
    assert "Mine" in page.text
    assert "NotMine" not in page.text
