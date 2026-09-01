"""Share/invite reconciliation against plex.tv (no network: fake account/server)."""
import types

import pytest
from plexapi.myplex import MyPlexInvite

import app.services.plex_service as plex_service


class _Elem:
    def __init__(self, **attrib):
        self.attrib = attrib


class FakeAccount:
    """Stands in for MyPlexAccount: records calls, replays canned failures."""

    def __init__(self, users=(), shares=(), invite_error=None, invites=()):
        self._users = list(users)
        self._shares = list(shares)
        self._invites = list(invites)
        self._invite_error = invite_error
        self.calls = []
        self._session = types.SimpleNamespace(delete="DELETE")

    def users(self):
        return self._users

    def inviteFriend(self, email, server, sections=None):
        self.calls.append(("invite", email, sections))
        if self._invite_error and len(
            [c for c in self.calls if c[0] == "invite"]
        ) == 1:
            raise self._invite_error

    def updateFriend(self, email, server, sections=None):
        self.calls.append(("update", email, sections))

    def removeFriend(self, email):
        self.calls.append(("removeFriend", email))
        raise RuntimeError("not a friend")

    def query(self, url, method=None, **kwargs):
        self.calls.append(("query", url, method))
        if method is not None:
            return None
        return self._invites if url.startswith(MyPlexInvite.REQUESTED) else self._shares


def _user(email):
    return types.SimpleNamespace(email=email, servers=[])


@pytest.fixture
def wired(monkeypatch):
    """Point plex_service at a fake account/server + a known machine id."""

    def _wire(account):
        server = types.SimpleNamespace(machineIdentifier="MID")
        monkeypatch.setattr(
            plex_service, "_account_and_server", lambda: (account, server)
        )
        monkeypatch.setattr(plex_service, "_account", lambda: account)
        monkeypatch.setattr(
            plex_service.runtime_config,
            "plex_config",
            lambda: {
                "token": "t",
                "server_name": "S",
                "server_id": "MID",
                "account_email": "",
                "direct_url": "",
            },
        )
        return account, server

    return _wire


_ALREADY = RuntimeError(
    "(400) bad_request; https://plex.tv/api/servers/MID/shared_servers "
    '<Response code="400" status="You\'re already sharing this server with '
    'a@b.it. Please edit your existing share."/>'
)


def test_invite_edits_existing_share_for_known_user(wired):
    account, _ = wired(
        FakeAccount(users=[_user("a@b.it")], invite_error=_ALREADY)
    )
    plex_service.invite_friend("a@b.it", sections=["Film"])
    assert ("update", "a@b.it", ["Film"]) in account.calls


def test_invite_drops_orphan_share_and_retries(wired):
    # Share row left behind by a withdrawn invite: no user record any more.
    account, _ = wired(
        FakeAccount(
            users=[],
            shares=[_Elem(id="77", email="a@b.it", username="")],
            invite_error=_ALREADY,
        )
    )
    plex_service.invite_friend("a@b.it", sections=["Film"])
    assert (
        "query",
        "https://plex.tv/api/servers/MID/shared_servers/77",
        "DELETE",
    ) in account.calls
    assert [c for c in account.calls if c[0] == "invite"] == [
        ("invite", "a@b.it", ["Film"]),
        ("invite", "a@b.it", ["Film"]),
    ]


def test_invite_reraises_when_no_share_found(wired):
    account, _ = wired(FakeAccount(users=[], shares=[], invite_error=_ALREADY))
    with pytest.raises(RuntimeError, match="already sharing"):
        plex_service.invite_friend("a@b.it", sections=["Film"])


def test_invite_reraises_unrelated_error(wired):
    account, _ = wired(FakeAccount(invite_error=RuntimeError("(401) unauthorized")))
    with pytest.raises(RuntimeError, match="401"):
        plex_service.invite_friend("a@b.it", sections=["Film"])
    assert not [c for c in account.calls if c[0] == "update"]


def test_cancel_invite_sweeps_leftover_share(wired):
    # Neither pending nor a friend, but the share row is still there.
    account, _ = wired(
        FakeAccount(shares=[_Elem(id="77", email="a@b.it", username="")])
    )
    plex_service.cancel_invite("a@b.it")
    assert (
        "query",
        "https://plex.tv/api/servers/MID/shared_servers/77",
        "DELETE",
    ) in account.calls


def test_cancel_invite_raises_when_nothing_matches(wired):
    wired(FakeAccount(shares=[]))
    with pytest.raises(plex_service.PlexShareNotFound):
        plex_service.cancel_invite("a@b.it")


def _invite(email, invite_id=None):
    """A pending invite as plex.tv really returns it. Until the address has a
    Plex account there is no username and no numeric id: the id *is* the
    address, which is what plexapi's `cast(int, ...)` turns into `nan`."""
    return _Elem(
        id=invite_id or email,
        email=email,
        username="" if invite_id is None else "someone",
        friend="0",
        home="0",
        server="1",
    )


_WITHDRAW = "https://plex.tv/api/invites/requested/a@b.it?friend=0&home=0&server=1"


def test_cancel_invite_withdraws_invite_without_a_plex_account(wired):
    # The invitee hasn't signed up yet, so the invite id is their address.
    # Handing that to plexapi 404s on `.../requested/nan`; we DELETE it by id.
    account, _ = wired(
        FakeAccount(
            invites=[_invite("a@b.it")],
            shares=[_Elem(id="77", email="a@b.it")],
        )
    )
    plex_service.cancel_invite("a@b.it")
    assert ("query", _WITHDRAW, "DELETE") in account.calls
    # The share row goes too -- leaving it is what 400s the next invite.
    assert (
        "query",
        "https://plex.tv/api/servers/MID/shared_servers/77",
        "DELETE",
    ) in account.calls


def test_cancel_invite_ignores_a_pending_invite_for_someone_else(wired):
    account, _ = wired(FakeAccount(invites=[_invite("other@b.it")], shares=[]))
    with pytest.raises(plex_service.PlexShareNotFound):
        plex_service.cancel_invite("a@b.it")
    assert not [c for c in account.calls if c[0] == "query" and c[2] == "DELETE"]


def test_invite_withdraws_a_still_open_invite_and_retries(wired):
    # plex.tv says "already sharing" for an invite it never withdrew. It is in
    # neither `users()` nor `shared_servers` -- only in the pending list.
    account, _ = wired(
        FakeAccount(
            users=[], shares=[], invites=[_invite("a@b.it")], invite_error=_ALREADY
        )
    )
    plex_service.invite_friend("a@b.it", sections=["Film"])
    assert ("query", _WITHDRAW, "DELETE") in account.calls
    assert [c for c in account.calls if c[0] == "invite"] == [
        ("invite", "a@b.it", ["Film"]),
        ("invite", "a@b.it", ["Film"]),
    ]
