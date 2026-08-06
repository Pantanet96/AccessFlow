"""Overseerr / Jellyseerr / Seerr integration (API-compatible).

All calls no-op (return None/[]) when integration is disabled or unconfigured.
"""
import httpx

from app import runtime_config

_TIMEOUT = 15


def _cfg() -> dict:
    return runtime_config.overseerr_config()


def enabled() -> bool:
    c = _cfg()
    return c["enabled"] and bool(c["url"]) and bool(c["api_key"])


def _headers(c: dict) -> dict:
    return {"X-Api-Key": c["api_key"], "Content-Type": "application/json"}


class OverseerrUrlError(RuntimeError):
    """URL missing / unreachable (DNS, refused, timeout, wrong host)."""


class OverseerrKeyError(RuntimeError):
    """URL reached but the API key was rejected (401/403)."""


def test() -> dict:
    """Validate URL and API key separately so the UI can tell them apart.

    /api/v1/status is PUBLIC (200s even with a wrong key) so it only proves the
    URL is reachable. /api/v1/settings/main is auth-gated (403 on a bad key) so
    it proves the key. Raises OverseerrUrlError / OverseerrKeyError accordingly.
    """
    c = _cfg()
    if not c["url"]:
        raise OverseerrUrlError("Overseerr URL not set")
    if not c["api_key"]:
        raise OverseerrKeyError("Overseerr API key not set")
    try:
        auth = httpx.get(
            f"{c['url']}/api/v1/settings/main", headers=_headers(c), timeout=_TIMEOUT
        )
    except httpx.RequestError as exc:  # DNS / refused / timeout -> URL problem
        raise OverseerrUrlError(str(exc)) from exc
    if auth.status_code in (401, 403):
        raise OverseerrKeyError(f"HTTP {auth.status_code}")
    if auth.status_code >= 500 or auth.status_code == 404:
        raise OverseerrUrlError(f"HTTP {auth.status_code}")  # wrong host / not Overseerr
    auth.raise_for_status()
    resp = httpx.get(f"{c['url']}/api/v1/status", headers=_headers(c), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def find_user(plex_id: str | None = None, email: str | None = None) -> dict | None:
    c = _cfg()
    if not enabled():
        return None
    skip = 0
    take = 100
    email_l = (email or "").lower()
    while True:
        resp = httpx.get(
            f"{c['url']}/api/v1/user",
            headers=_headers(c),
            params={"take": take, "skip": skip},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        for u in results:
            if plex_id and str(u.get("plexId")) == str(plex_id):
                return u
            if email_l and (u.get("email") or "").lower() == email_l:
                return u
        page = data.get("pageInfo", {})
        if skip + take >= page.get("results", 0):
            return None
        skip += take


def import_from_plex(plex_ids: list[str] | None = None) -> None:
    c = _cfg()
    if not enabled():
        return
    body = {}
    if plex_ids:
        body["plexIds"] = [str(p) for p in plex_ids]
    resp = httpx.post(
        f"{c['url']}/api/v1/user/import-from-plex",
        headers=_headers(c),
        json=body,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()


def set_permissions(user_id: int, permissions: int) -> None:
    c = _cfg()
    if not enabled():
        return
    resp = httpx.post(
        f"{c['url']}/api/v1/user/{user_id}/settings/permissions",
        headers=_headers(c),
        json={"permissions": permissions},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()


def delete_user(user_id: int) -> None:
    c = _cfg()
    if not enabled():
        return
    resp = httpx.delete(
        f"{c['url']}/api/v1/user/{user_id}", headers=_headers(c), timeout=_TIMEOUT
    )
    resp.raise_for_status()


# ---- Telegram bot settings (global) ----

def get_telegram_settings() -> dict:
    """Overseerr's global Telegram agent settings (admin API)."""
    c = _cfg()
    resp = httpx.get(
        f"{c['url']}/api/v1/settings/notifications/telegram",
        headers=_headers(c),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def bot_match() -> dict:
    """Compare the Telegram bot used by AccessFlow vs Overseerr.

    Returns {checked, same, pum_username, ov_username, reason}. `same` is
    None when it cannot be determined (missing data / call failed).
    """
    from app import runtime_config

    tg = runtime_config.telegram_config()
    pum_token = (tg["token"] or "").strip()
    pum_user = (tg["username"] or "").strip().lstrip("@").lower()
    out = {
        "checked": True,
        "same": None,
        "pum_username": tg["username"] or "",
        "ov_username": "",
        "reason": "",
    }
    if not enabled():
        out.update(checked=False, reason="overseerr_disabled")
        return out
    try:
        opts = (get_telegram_settings() or {}).get("options", {}) or {}
    except Exception as exc:  # noqa: BLE001
        out.update(reason=f"error: {exc}")
        return out
    ov_token = str(opts.get("botAPI") or "").strip()
    ov_user = str(opts.get("botUsername") or "").strip().lstrip("@").lower()
    out["ov_username"] = opts.get("botUsername") or ""
    # Token match is definitive; fall back to username match.
    if pum_token and ov_token:
        out["same"] = pum_token == ov_token
        out["reason"] = "token"
    elif pum_user and ov_user:
        out["same"] = pum_user == ov_user
        out["reason"] = "username"
    else:
        out["reason"] = "insufficient_data"
    return out


# ---- Per-user Telegram chat id ----

def get_user_notifications(user_id: int) -> dict:
    c = _cfg()
    resp = httpx.get(
        f"{c['url']}/api/v1/user/{user_id}/settings/notifications",
        headers=_headers(c),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_user_chat_id(plex_id: str | None = None, email: str | None = None) -> str | None:
    if not enabled():
        return None
    ou = find_user(plex_id=plex_id, email=email)
    if not ou:
        return None
    settings = get_user_notifications(ou["id"])
    cid = settings.get("telegramChatId")
    return str(cid) if cid else None


def push_user_chat_id(
    chat_id: str, plex_id: str | None = None, email: str | None = None
) -> bool:
    """Write `chat_id` into the Overseerr user's Telegram settings. Returns True on success."""
    if not enabled() or not chat_id:
        return False
    ou = find_user(plex_id=plex_id, email=email)
    if not ou:
        return False
    settings = get_user_notifications(ou["id"])
    settings["telegramChatId"] = str(chat_id)
    if settings.get("telegramSendSilently") is None:
        settings["telegramSendSilently"] = False
    resp = httpx.post(
        f"{_cfg()['url']}/api/v1/user/{ou['id']}/settings/notifications",
        headers=_headers(_cfg()),
        json=settings,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return True
