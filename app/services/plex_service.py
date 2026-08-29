"""Plex admin operations using the connected admin token (share/unshare)."""
import time

from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

from app import runtime_config

# Fail fast: a slow/unreachable Plex shouldn't hang a web request for 30s+.
_TIMEOUT = 8


class PlexNotConnected(RuntimeError):
    pass


class PlexShareNotFound(RuntimeError):
    """No pending invite / share on plex.tv for the given address."""


def _account() -> MyPlexAccount:
    token = runtime_config.plex_config()["token"]
    if not token:
        raise PlexNotConnected("Plex is not connected")
    return MyPlexAccount(token=token, timeout=_TIMEOUT)


# Reuse the connected server across requests. Discovery + connect() costs
# ~8-22s here because the container can't reach the LAN connection URIs plex.tv
# advertises, so plexapi burns the timeout on them before the remote one wins.
# ponytail: TTL cache, no health-retry — a stale entry self-heals on the next
#           connect() error (cache cleared) or after TTL; add retry-on-use if a
#           dead-but-cached connection ever causes a visible failure.
_CONN_TTL = 600  # seconds
_conn_cache: dict | None = None


def _account_and_server():
    global _conn_cache
    cfg = runtime_config.plex_config()
    if not cfg["token"] or not cfg["server_name"]:
        raise PlexNotConnected("Plex is not connected / no server selected")
    direct = cfg.get("direct_url", "")
    key = f"{cfg['token']}:{cfg['server_name']}:{direct}"
    cached = _conn_cache
    if cached is not None and cached["key"] == key and (time.time() - cached["at"]) < _CONN_TTL:
        return cached["account"], cached["server"]
    try:
        account = MyPlexAccount(token=cfg["token"], timeout=_TIMEOUT)
        if direct:
            # Skip plex.tv discovery (no probing the dead LAN URIs): one GET to
            # the baseurl. Friend/share ops still use `account` (plex.tv) below.
            server = PlexServer(direct, token=cfg["token"], timeout=_TIMEOUT)
        else:
            server = account.resource(cfg["server_name"]).connect(timeout=_TIMEOUT)
    except Exception:
        _conn_cache = None
        raise
    _conn_cache = {"key": key, "at": time.time(), "account": account, "server": server}
    return account, server


def _is_friend(account, email: str) -> bool:
    return any(
        (u.email or "").lower() == email.lower() for u in account.users()
    )


# Libraries change rarely; cache so the connected server isn't hit on every
# page render. Keyed by token+server so a reconnect/server-switch invalidates.
# An hour, not 5 minutes: the only cost of a stale entry is that a library added
# on Plex shows up late in the drift warning, and an admin who just added one has
# the "Resync libraries" button (users.py -> list_sections(force=True)) to refresh
# on demand. The 5-minute TTL mostly bought repeated ~8-22s cold reconnects.
_SECTIONS_TTL = 3600  # seconds
_sections_cache: dict | None = None


def list_sections(force: bool = False) -> list[dict]:
    """Available libraries on the connected server (cached up to 5 min)."""
    global _sections_cache
    cfg = runtime_config.plex_config()
    key = f"{cfg.get('token')}:{cfg.get('server_name')}:{cfg.get('direct_url', '')}"
    cached = _sections_cache
    if (
        not force
        and cached is not None
        and cached["key"] == key
        and (time.time() - cached["at"]) < _SECTIONS_TTL
    ):
        return cached["data"]
    _account, server = _account_and_server()
    data = [{"key": str(s.key), "title": s.title} for s in server.library.sections()]
    _sections_cache = {"key": key, "at": time.time(), "data": data}
    return data


def list_sections_safe() -> list[dict]:
    """`list_sections()`, swallowing errors (Plex off/unreachable) into []."""
    try:
        return list_sections()
    except Exception:  # noqa: BLE001
        return []


def _machine_id(server=None) -> str:
    """machineIdentifier of the connected server (config first: no connect())."""
    mid = runtime_config.plex_config().get("server_id") or ""
    if mid:
        return mid
    if server is None:
        _acct, server = _account_and_server()
    return server.machineIdentifier


def _find_share_id(account, machine_id: str, email: str) -> str | None:
    """Id of the plex.tv `shared_servers` row for `email`, if one exists.

    Reads the share list directly instead of `account.users()`: a withdrawn
    invite can leave the share row behind after the user record is gone, and
    that orphan is exactly what makes the next invite 400."""
    try:
        data = account.query(MyPlexAccount.FRIENDINVITE.format(machineId=machine_id))
    except Exception:  # noqa: BLE001 - no share list, nothing to reconcile
        return None
    for elem in data if data is not None else []:
        attrib = elem.attrib
        candidates = {
            (attrib.get("email") or "").lower(),
            (attrib.get("username") or "").lower(),
            (attrib.get("invitedEmail") or "").lower(),
        }
        if email.lower() in candidates - {""}:
            return attrib.get("id")
    return None


def _delete_share(account, machine_id: str, share_id: str) -> None:
    """DELETE the `shared_servers` row (plexapi has no wrapper for it)."""
    url = MyPlexAccount.FRIENDSERVERS.format(machineId=machine_id, serverId=share_id)
    account.query(url, account._session.delete)


def invite_friend(email: str, sections: list[str] | None = None) -> None:
    """Share the connected server with `email` (given library titles, or all).

    Self-healing on re-invite: plex.tv can keep the `shared_servers` row when an
    invite is withdrawn, and a fresh POST then fails with 400 "You're already
    sharing this server with <email>. Please edit your existing share." Rather
    than dead-ending the admin, edit that share (or drop the stale row and
    invite again) so the address is left shared with exactly `sections`."""
    account, server = _account_and_server()
    _invite(account, server, email, sections)


def _invite(account, server, email: str, sections) -> None:
    """`inviteFriend`, recovering from plex.tv's "already sharing" 400."""
    try:
        account.inviteFriend(email, server, sections=sections or None)
    except Exception as exc:  # noqa: BLE001
        if "already sharing" not in str(exc).lower():
            raise
        if not _resolve_existing_share(account, server, email, sections):
            raise


def _resolve_existing_share(account, server, email: str, sections) -> bool:
    """Point the share plex.tv already holds for `email` at `sections`.
    Returns False if no existing share could be found/repaired."""
    if sections and _is_friend(account, email):
        # Still a known user (invite accepted, or pending but still listed):
        # editing the share is what the plex.tv error asks for.
        account.updateFriend(email, server, sections=sections)
        return True
    # Orphan share, or "all libraries" (which updateFriend can't express):
    # drop the row and invite again from scratch.
    machine_id = _machine_id(server)
    share_id = _find_share_id(account, machine_id, email)
    if share_id is None:
        return False
    _delete_share(account, machine_id, share_id)
    account.inviteFriend(email, server, sections=sections or None)
    return True


def share(email: str, sections: list[str]) -> None:
    """Ensure `email` is shared with exactly `sections` (invite or update).
    Titles no longer present on the server are dropped (plexapi would 404 on a
    renamed/deleted library)."""
    account, server = _account_and_server()
    if sections:
        live = {s.title for s in server.library.sections()}
        sections = [t for t in sections if t in live]
    if _is_friend(account, email):
        account.updateFriend(email, server, sections=sections)
    else:
        _invite(account, server, email, sections)


def unshare(email: str) -> None:
    """Remove all shared libraries for `email` (keeps the friend)."""
    account, server = _account_and_server()
    if _is_friend(account, email):
        account.updateFriend(email, server, sections=[])


def get_user_sections(email: str) -> list[str]:
    """Library titles currently shared with `email` on the connected server."""
    account, server = _account_and_server()
    target = server.friendlyName
    for user in account.users():
        if (user.email or "").lower() != email.lower():
            continue
        for share_ in (user.servers or []):
            if getattr(share_, "name", None) == target:
                try:
                    return [
                        s.title for s in share_.sections() if getattr(s, "shared", True)
                    ]
                except Exception:  # noqa: BLE001
                    return []
    return []


def cancel_invite(email: str) -> None:
    """Withdraw a pending Plex invite for `email`. If it was already accepted on
    Plex (now a friend) but not yet recorded here, revoke that access instead.

    Cancelling the friend request is not enough on its own: plex.tv can keep the
    server's `shared_servers` row, and the next invite for that address then
    fails with 400 "You're already sharing this server with ...". So sweep the
    share list too, and only raise if nothing at all matched `email`."""
    account = _account()
    done = False
    try:
        account.cancelInvite(email)
        done = True
    except Exception:  # noqa: BLE001 - not pending; maybe already a friend
        pass
    try:
        account.removeFriend(email)
        done = True
    except Exception:  # noqa: BLE001 - not a friend either
        pass
    try:
        machine_id = _machine_id()
        share_id = _find_share_id(account, machine_id, email)
        if share_id is not None:
            _delete_share(account, machine_id, share_id)
            done = True
    except Exception:  # noqa: BLE001 - share sweep is best-effort
        pass
    if not done:
        raise PlexShareNotFound(f"No pending invite or share found for {email}")


def remove_friend(email: str) -> None:
    """Revoke a user's access to the Plex server."""
    _account().removeFriend(email)


def list_shared_users() -> list[dict]:
    """Users (friends/home) who have access to the connected server."""
    cfg = runtime_config.plex_config()
    if not cfg["token"] or not cfg["server_name"]:
        raise PlexNotConnected("Plex is not connected / no server selected")
    account = MyPlexAccount(token=cfg["token"])
    server_id = cfg["server_id"]
    server_name = cfg["server_name"]
    out = []
    for user in account.users():
        shares = getattr(user, "servers", None) or []
        has_access = any(
            (server_id and getattr(sh, "machineIdentifier", None) == server_id)
            or getattr(sh, "name", None) == server_name
            for sh in shares
        )
        if not has_access:
            continue
        out.append(
            {
                "id": str(user.id) if getattr(user, "id", None) else None,
                "email": getattr(user, "email", None),
                "username": getattr(user, "username", None) or getattr(user, "title", None),
            }
        )
    return out
