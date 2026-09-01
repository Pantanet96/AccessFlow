"""Share/invite reconciliation against plex.tv (no network: fake account/server)."""
import types

import pytest

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

    def pendingInvites(self, includeSent=True, includeReceived=True):
        self.calls.append(("pendingInvites", includeReceived))
        return self._invites

    def cancelInvite(self, user):
        """Mirrors plexapi: given a string it goes through `pendingInvite()`,
        which skips every invite with an empty `username` -- so it raises for an
        address with no Plex account yet. Given the object it just deletes."""
        if isinstance(user, str):
            self.calls.append(("cancelInvite:notfound", user))
            raise RuntimeError(f"Unable to find invite {user}")
        self.calls.append(("cancelInvite", user.email))

    def removeFriend(self, email):
        self.calls.append(("removeFriend", email))
        raise RuntimeError("not a friend")

    def query(self, url, method=None, **kwargs):
        self.calls.append(("query", url, method))
        if method is None:
            return self._shares
        return None


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


def _invite(email, username=""):
    """plex.tv leaves `username` empty until the address has a Plex account."""
    return types.SimpleNamespace(email=email, username=username, id=42)


def test_cancel_invite_withdraws_invite_without_a_plex_account(wired):
    # The invitee hasn't signed up yet, so plex.tv reports no username for the
    # invite. plexapi's own lookup drops it on the floor; ours must not.
    account, _ = wired(
        FakeAccount(
            invites=[_invite("a@b.it")],
            shares=[_Elem(id="77", email="a@b.it")],
        )
    )
    plex_service.cancel_invite("a@b.it")
    assert ("cancelInvite", "a@b.it") in account.calls
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
    assert not [c for c in account.calls if c[0].startswith("cancelInvite")]
