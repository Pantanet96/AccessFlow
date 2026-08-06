"""Plex admin operations using the connected admin token (share/unshare)."""
import time

from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

from app import runtime_config

# Fail fast: a slow/unreachable Plex shouldn't hang a web request for 30s+.
_TIMEOUT = 8


class PlexNotConnected(RuntimeError):
    pass


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


def invite_friend(email: str, sections: list[str] | None = None) -> None:
    """Share the connected server with `email` (given library titles, or all)."""
    account, server = _account_and_server()
    account.inviteFriend(email, server, sections=sections or None)


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
        account.inviteFriend(email, server, sections=sections or None)


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
    Best-effort: raises only if `email` is neither pending nor a friend."""
    account = _account()
    try:
        account.cancelInvite(email)
    except Exception:  # noqa: BLE001 - not pending; maybe already a friend
        account.removeFriend(email)


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
