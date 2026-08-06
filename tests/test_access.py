import json
from datetime import timedelta

from sqlmodel import select

import app.services.overseerr_service as ov
import app.services.plex_service as plex_service
from app.models import AppUser, Role, Subscription, SubscriptionStatus, utcnow
from app.services import access_service, settings_store


def _mk(session, role=Role.user, name="U", **kw):
    u = AppUser(role=role, real_name=name, plex_email=f"{name}@ex.com",
                plex_account_id=name, **kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _enable_overseerr(session):
    settings_store.set_value(session, "overseerr_url", "http://os:5055")
    settings_store.set_value(session, "overseerr_api_key", "key")
    settings_store.set_value(session, "overseerr_enabled", "true")


def _mute_plex(monkeypatch):
    monkeypatch.setattr(plex_service, "share", lambda *a, **k: None)
    monkeypatch.setattr(plex_service, "unshare", lambda *a, **k: None)
    monkeypatch.setattr(plex_service, "remove_friend", lambda *a, **k: None)


# ---- libraries_for ----

def test_libraries_for_override_and_default(db_session):
    settings_store.set_value(db_session, "plex_default_sections", json.dumps(["Movies"]))
    u = _mk(db_session, name="LibUser")
    assert access_service.libraries_for(db_session, u) == ["Movies"]
    u.shared_libraries = json.dumps(["TV", "Music"])
    assert access_service.libraries_for(db_session, u) == ["TV", "Music"]


# ---- suspend / reactivate (with Overseerr) ----

def test_suspend_sets_flag_and_disables_overseerr(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    _enable_overseerr(db_session)
    calls = {}
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 7, "permissions": 32})
    monkeypatch.setattr(ov, "set_permissions", lambda uid, perms: calls.setdefault("perms", (uid, perms)))
    u = _mk(db_session, name="Susp")

    access_service.suspend(db_session, u)
    assert u.access_suspended is True
    assert u.overseerr_prev_permissions == 32  # saved before zeroing
    assert calls["perms"] == (7, 0)


def test_reactivate_restores_prev_permissions(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    _enable_overseerr(db_session)
    calls = {}
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 7, "permissions": 0})
    monkeypatch.setattr(ov, "set_permissions", lambda uid, perms: calls.setdefault("perms", (uid, perms)))
    u = _mk(db_session, name="React", access_suspended=True, overseerr_prev_permissions=32)

    access_service.reactivate(db_session, u)
    assert u.access_suspended is False
    assert calls["perms"] == (7, 32)


def test_remove_from_plex_deletes_overseerr(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    _enable_overseerr(db_session)
    deleted = {}
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 9})
    monkeypatch.setattr(ov, "delete_user", lambda uid: deleted.setdefault("id", uid))
    u = _mk(db_session, name="Gone")
    access_service.remove_from_plex(db_session, u)
    assert deleted["id"] == 9


def test_grant_overseerr_imports_and_enables(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    _enable_overseerr(db_session)
    calls = {}
    monkeypatch.setattr(ov, "import_from_plex", lambda ids: calls.setdefault("import", ids))
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 11, "permissions": 0})
    monkeypatch.setattr(ov, "set_permissions", lambda uid, perms: calls.setdefault("perms", (uid, perms)))
    u = _mk(db_session, name="NewU", overseerr_prev_permissions=32)
    access_service.grant_overseerr(db_session, u)
    assert calls["import"] == [u.plex_account_id]
    assert calls["perms"] == (11, 32)


def test_grant_overseerr_skipped_when_disabled(db_session, monkeypatch):
    monkeypatch.setattr(ov, "import_from_plex",
                        lambda ids: (_ for _ in ()).throw(AssertionError("called")))
    u = _mk(db_session, name="NewU2")
    access_service.grant_overseerr(db_session, u)  # no-op, must not raise


def test_remove_from_overseerr_deletes(db_session, monkeypatch):
    _enable_overseerr(db_session)
    deleted = {}
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 13})
    monkeypatch.setattr(ov, "delete_user", lambda uid: deleted.setdefault("id", uid))
    u = _mk(db_session, name="Del")
    access_service.remove_from_overseerr(u)
    assert deleted["id"] == 13


def test_overseerr_skipped_when_disabled(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    # overseerr not enabled -> find_user should never be called
    monkeypatch.setattr(ov, "find_user", lambda **k: (_ for _ in ()).throw(AssertionError("called")))
    u = _mk(db_session, name="NoOv")
    access_service.suspend(db_session, u)  # must not raise
    assert u.access_suspended is True


def _trial_plan(session):
    from app.models import Plan

    p = Plan(name="Trial", slug="trial_ov", is_trial=True, is_paid=False,
             duration_days=14)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _activate_sub(session, user, plan):
    session.add(Subscription(user_id=user.id, plan_id=plan.id,
                             expiry_at=utcnow() + timedelta(days=7),
                             status=SubscriptionStatus.active))
    session.commit()


def test_reactivate_trial_is_view_only(db_session, monkeypatch):
    # A trial user gets permission 0 (view only) regardless of saved/default.
    _mute_plex(monkeypatch)
    _enable_overseerr(db_session)
    calls = {}
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 7, "permissions": 0})
    monkeypatch.setattr(ov, "set_permissions", lambda uid, perms: calls.setdefault("perms", (uid, perms)))
    u = _mk(db_session, name="TrialReact", access_suspended=True, overseerr_prev_permissions=32)
    _activate_sub(db_session, u, _trial_plan(db_session))

    access_service.reactivate(db_session, u)
    assert calls["perms"] == (7, 0)  # not 32 -> no requests


def test_sync_trial_strips_requests(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    _enable_overseerr(db_session)
    calls = {}
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 5, "permissions": 32})
    monkeypatch.setattr(ov, "set_permissions", lambda uid, perms: calls.setdefault("perms", (uid, perms)))
    u = _mk(db_session, name="TrialSync")
    _activate_sub(db_session, u, _trial_plan(db_session))

    access_service.sync_overseerr_permissions(db_session, u)
    assert calls["perms"] == (5, 0)


def test_reconcile_reasserts_trial_view_only(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    _enable_overseerr(db_session)
    calls = []
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 8, "permissions": 32})
    monkeypatch.setattr(ov, "set_permissions", lambda uid, perms: calls.append((uid, perms)))
    u = _mk(db_session, name="TrialRecon")
    _activate_sub(db_session, u, _trial_plan(db_session))

    access_service.reconcile_all(db_session)
    assert (8, 0) in calls  # trial held at view-only, not auto-suspended (within window)
    db_session.refresh(u)
    assert u.access_suspended is False


# ---- reconcile_all ----

def test_reconcile_auto_suspends_expired_beyond_grace(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    # no overseerr
    expired = _mk(db_session, name="Expired", grace_days=2)
    db_session.add(Subscription(user_id=expired.id, plan_id=1,
                                expiry_at=utcnow() - timedelta(days=5),
                                status=SubscriptionStatus.active))
    within = _mk(db_session, name="WithinGrace", grace_days=10)
    db_session.add(Subscription(user_id=within.id, plan_id=1,
                                expiry_at=utcnow() - timedelta(days=3),
                                status=SubscriptionStatus.active))
    nosub = _mk(db_session, name="NoSub")
    db_session.commit()

    n = access_service.reconcile_all(db_session)
    db_session.refresh(expired); db_session.refresh(within); db_session.refresh(nosub)
    assert expired.access_suspended is True   # 5 days > 2 grace
    assert within.access_suspended is False   # 3 days < 10 grace
    assert nosub.access_suspended is False    # no subscription -> untouched
    assert n == 1


# ---- overseerr_service.find_user pagination ----

def test_overseerr_find_user_paginates(db_session, monkeypatch):
    _enable_overseerr(db_session)
    pages = {
        0: {"pageInfo": {"results": 3}, "results": [{"id": 1, "plexId": "a"}, {"id": 2, "plexId": "b"}]},
        2: {"pageInfo": {"results": 3}, "results": [{"id": 3, "plexId": "c"}]},
    }

    class _Resp:
        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    def _get(url, headers=None, params=None, timeout=None):
        return _Resp(pages[params["skip"]])

    monkeypatch.setattr(ov.httpx, "get", _get)
    # take=100 so after first page skip(0)+100 >= 3 -> stops; 'c' not found in page 0
    found = ov.find_user(plex_id="a")
    assert found["id"] == 1
    assert ov.find_user(plex_id="zzz") is None


# ---- Overseerr Telegram bot match + chat-id sync ----

def test_bot_match_same_and_different(db_session, monkeypatch):
    _enable_overseerr(db_session)
    settings_store.set_value(db_session, "telegram_bot_username", "MyBot")
    monkeypatch.setattr(ov, "get_telegram_settings",
                        lambda: {"options": {"botUsername": "MyBot"}})
    m = ov.bot_match()
    assert m["checked"] is True and m["same"] is True and m["reason"] == "username"

    monkeypatch.setattr(ov, "get_telegram_settings",
                        lambda: {"options": {"botUsername": "OtherBot"}})
    assert ov.bot_match()["same"] is False


def test_bot_match_unknown_when_no_data(db_session, monkeypatch):
    _enable_overseerr(db_session)
    settings_store.set_value(db_session, "telegram_bot_username", "")
    monkeypatch.setattr(ov, "get_telegram_settings", lambda: {"options": {}})
    assert ov.bot_match()["same"] is None


def test_get_and_push_user_chat_id(db_session, monkeypatch):
    _enable_overseerr(db_session)
    monkeypatch.setattr(ov, "find_user", lambda plex_id=None, email=None: {"id": 5})
    monkeypatch.setattr(ov, "get_user_notifications", lambda uid: {"telegramChatId": 999})
    assert ov.get_user_chat_id(plex_id="x") == "999"

    posted = {}

    class _Resp:
        def raise_for_status(self): pass

    def _post(url, headers=None, json=None, timeout=None):
        posted["json"] = json
        return _Resp()

    monkeypatch.setattr(ov, "get_user_notifications", lambda uid: {})
    monkeypatch.setattr(ov.httpx, "post", _post)
    assert ov.push_user_chat_id("12345", plex_id="x") is True
    assert posted["json"]["telegramChatId"] == "12345"


# ---- library drift (warn-only) ----

def test_library_drift_reports_new_and_stale(db_session, monkeypatch):
    from app.models import Plan
    from app.services import settings_store as ss

    monkeypatch.setattr(plex_service, "list_sections",
                        lambda force=False: [{"title": "Movies"}, {"title": "TV"}])
    ss.set_value(db_session, "plex_default_sections", json.dumps(["Movies"]))
    db_session.add(Plan(name="P", slug="px", libraries=json.dumps(["Gone"])))
    db_session.commit()

    drift = access_service.library_drift(db_session)
    assert drift["new_on_plex"] == ["TV"]       # exists on Plex, shared nowhere
    assert drift["stale_refs"] == ["Gone"]      # configured but missing on Plex


def test_library_drift_silent_when_unconfigured(db_session, monkeypatch):
    # No explicit config anywhere -> implicit "all" -> nothing flagged as new.
    monkeypatch.setattr(plex_service, "list_sections",
                        lambda force=False: [{"title": "Movies"}, {"title": "TV"}])
    assert access_service.library_drift(db_session) == {}


# ---- resync_libraries ----

def test_resync_reapplies_explicit_and_skips_all(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    monkeypatch.setattr(plex_service, "list_sections",
                        lambda force=False: [{"title": "Movies"}, {"title": "TV"}])
    monkeypatch.setattr(plex_service, "get_user_sections", lambda email: [])
    calls = []
    monkeypatch.setattr(plex_service, "share", lambda email, secs: calls.append((email, secs)))

    explicit = _mk(db_session, name="Explicit", shared_libraries=json.dumps(["Movies"]))
    _mk(db_session, name="AllDefault")  # no override, no default -> implicit all

    result = access_service.resync_libraries(db_session)
    assert (explicit.plex_email, ["Movies"]) in calls  # diff -> re-applied
    assert result["updated"] == 1                       # the "all" user is skipped


def test_resync_skips_when_already_matching(db_session, monkeypatch):
    _mute_plex(monkeypatch)
    monkeypatch.setattr(plex_service, "list_sections",
                        lambda force=False: [{"title": "Movies"}])
    monkeypatch.setattr(plex_service, "get_user_sections", lambda email: ["Movies"])
    calls = []
    monkeypatch.setattr(plex_service, "share", lambda email, secs: calls.append((email, secs)))
    _mk(db_session, name="Same", shared_libraries=json.dumps(["Movies"]))

    result = access_service.resync_libraries(db_session)
    assert calls == []                 # already matches -> no write
    assert result["updated"] == 0


def test_resync_prunes_stale_config_refs(db_session, monkeypatch):
    from app.models import Plan
    from app import runtime_config
    from app.services import settings_store as ss

    _mute_plex(monkeypatch)
    monkeypatch.setattr(plex_service, "list_sections",
                        lambda force=False: [{"title": "Movies"}])
    monkeypatch.setattr(plex_service, "get_user_sections", lambda email: [])

    ss.set_value(db_session, "plex_default_sections",
                 json.dumps(["Movies", "Default", "Preroll"]))
    plan = Plan(name="P", slug="px", libraries=json.dumps(["Movies", "Gone"]))
    db_session.add(plan)
    user = _mk(db_session, name="Ovr", shared_libraries=json.dumps(["Dead"]))
    db_session.commit()

    result = access_service.resync_libraries(db_session)
    assert result["pruned"] == 4  # Default, Preroll, Gone, Dead
    db_session.refresh(plan)
    db_session.refresh(user)
    assert json.loads(plan.libraries) == ["Movies"]
    assert json.loads(user.shared_libraries) == []
    assert runtime_config.plex_default_sections() == ["Movies"]
    # config now references only live titles -> the drift banner clears
    assert access_service.library_drift(db_session) == {}


# ---- share() prunes deleted libraries ----

def test_share_prunes_titles_missing_on_server(db_session, monkeypatch):
    class _Sec:
        def __init__(self, t): self.title = t

    class _Lib:
        def __init__(self, titles): self._t = titles
        def sections(self): return [_Sec(t) for t in self._t]

    class _Server:
        def __init__(self, titles):
            self.library = _Lib(titles)
            self.friendlyName = "S"

    class _Acct:
        def __init__(self): self.captured = None
        def users(self): return []  # not a friend -> invite path
        def inviteFriend(self, email, server, sections=None):
            self.captured = ("invite", sections)
        def updateFriend(self, email, server, sections=None):
            self.captured = ("update", sections)

    acct, srv = _Acct(), _Server(["Movies", "TV"])
    monkeypatch.setattr(plex_service, "_account_and_server", lambda: (acct, srv))
    plex_service.share("e@x.com", ["Movies", "Deleted"])
    assert acct.captured == ("invite", ["Movies"])  # dead "Deleted" dropped
