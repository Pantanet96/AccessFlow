"""Plex OAuth PIN flow (plex.tv API v2).

Flow: create_pin -> redirect user to build_auth_url -> user authorizes ->
plex.tv redirects back to our callback -> poll_pin returns an auth token ->
fetch_account returns the Plex account identity.
"""
import time
import uuid
from urllib.parse import urlencode

import httpx

from app.config import get_settings

PLEX_API = "https://plex.tv/api/v2"
AUTH_BASE = "https://app.plex.tv/auth#?"
PRODUCT = "AccessFlow"
_TIMEOUT = 15


def client_id() -> str:
    settings = get_settings()
    if settings.plex_client_id:
        return settings.plex_client_id
    # Stable per-install identifier derived from the secret key.
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "pum-plex:" + settings.app_secret_key))


def _headers(token: str | None = None) -> dict[str, str]:
    h = {
        "accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": client_id(),
        "X-Plex-Version": "1.0",
        "X-Plex-Device-Name": PRODUCT,
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def create_pin() -> dict:
    resp = httpx.post(
        f"{PLEX_API}/pins", headers=_headers(), data={"strong": "true"}, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    return {"id": data["id"], "code": data["code"]}


def build_auth_url(code: str, forward_url: str) -> str:
    params = {
        "clientID": client_id(),
        "code": code,
        "forwardUrl": forward_url,
        "context[device][product]": PRODUCT,
    }
    return AUTH_BASE + urlencode(params)


def poll_pin(pin_id) -> str | None:
    resp = httpx.get(
        f"{PLEX_API}/pins/{pin_id}", headers=_headers(), timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json().get("authToken")


def wait_for_pin(pin_id, attempts: int = 6, delay: float = 0.5) -> str | None:
    """Poll `poll_pin` up to `attempts` times, `delay` seconds apart, until it
    returns an auth token (the user hasn't authorized yet until then)."""
    for attempt in range(attempts):
        token = poll_pin(pin_id)
        if token:
            return token
        if attempt < attempts - 1:
            time.sleep(delay)
    return None


def list_servers(token: str) -> tuple[str, list[dict]]:
    """Return (account_email, [{id, name}]) for owned Plex Media Servers."""
    from plexapi.myplex import MyPlexAccount

    account = MyPlexAccount(token=token)
    servers = []
    for res in account.resources():
        provides = (res.provides or "")
        if getattr(res, "owned", False) and "server" in provides:
            servers.append({"id": res.clientIdentifier, "name": res.name})
    return account.email, servers


def fetch_account(token: str) -> dict:
    resp = httpx.get(f"{PLEX_API}/user", headers=_headers(token), timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return {
        "id": data.get("id"),
        "uuid": data.get("uuid"),
        "email": data.get("email"),
        "username": data.get("username"),
    }
