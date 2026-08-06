"""Telegram message delivery via the Bot API (stateless, no polling).

The polling bot (Step 9) handles incoming commands; this module only sends.
"""
import logging

import httpx

from app import runtime_config

_API = "https://api.telegram.org"
log = logging.getLogger("pum.telegram")


def send_message(chat_id: str | int, text: str, parse_mode: str | None = None) -> bool:
    """Best-effort send. Returns True on delivery, False on any failure (channel
    unconfigured, recipient blocked the bot, transport/HTTP error). It never
    raises: a single unreachable recipient must not abort a batch (daily scan,
    broadcast). Failures are NEVER logged with the exception text — that embeds
    the request URL, which contains the bot token."""
    token = runtime_config.telegram_config()["token"]
    if not token or not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:  # e.g. "MarkdownV2" for templated notifications; None = plain
        payload["parse_mode"] = parse_mode
    try:
        resp = httpx.post(
            f"{_API}/bot{token}/sendMessage",
            json=payload,
            timeout=15,
        )
    except httpx.HTTPError:
        log.warning("Telegram send to %s failed: transport error", chat_id)
        return False
    if not resp.is_success:
        log.warning("Telegram send to %s failed: HTTP %s", chat_id, resp.status_code)
        return False
    return True


def deliver(chat_id: str | int, text: str) -> str:
    """Send `text` without raising. Used to confirm a freshly-set chat_id is
    actually reachable (Telegram forbids a bot messaging a user who never
    pressed Start). Returns one of:
      'ok'        - delivered
      'forbidden' - user never started the bot / blocked it (HTTP 403)
      'no_config' - no bot token or no chat_id -> nothing to verify
      'error'     - any other failure
    """
    token = runtime_config.telegram_config()["token"]
    if not token or not chat_id:
        return "no_config"
    try:
        resp = httpx.post(
            f"{_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except httpx.HTTPError:
        return "error"
    if resp.status_code == 403:
        return "forbidden"
    return "ok" if resp.is_success else "error"


def get_me() -> dict:
    """Validate the configured bot token; returns the bot's profile.

    Never lets an httpx exception escape: its text embeds the request URL, which
    contains the bot token, and callers render the error into the settings page /
    logs. Raises RuntimeError with a token-free message instead."""
    token = runtime_config.telegram_config()["token"]
    if not token:
        raise RuntimeError("No Telegram bot token configured")
    try:
        resp = httpx.get(f"{_API}/bot{token}/getMe", timeout=15)
    except httpx.HTTPError:
        raise RuntimeError("Telegram API unreachable")
    if not resp.is_success:
        raise RuntimeError(f"Telegram API returned HTTP {resp.status_code}")
    return resp.json()["result"]
