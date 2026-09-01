"""Resolved runtime configuration: DB setting overrides ENV fallback."""
import json
import logging

from sqlmodel import Session

from app.config import get_settings
from app.db import engine
from app.services import settings_store

# Runtime config is read on nearly every request (Plex/SMTP/Telegram/Overseerr
# calls, reminder scans); each read otherwise opens a session and Fernet-decrypts
# up to ~14 rows. Settings change only via the admin Settings page, so cache each
# group keyed by settings_store's generation counter and let a write invalidate it.
_cache: dict[str, tuple[int, object]] = {}


def _cached(key: str, builder):
    gen = settings_store.generation()
    hit = _cache.get(key)
    if hit is not None and hit[0] == gen:
        return hit[1]
    value = builder()
    _cache[key] = (gen, value)
    return value


def _read_all() -> dict:
    return _cached("read_all", _load_all)


def _load_all() -> dict:
    with Session(engine) as session:
        keys = [
            "plex_token",
            "plex_server_name",
            "plex_server_id",
            "plex_account_email",
            "plex_direct_url",
            "smtp_host",
            "smtp_port",
            "smtp_user",
            "smtp_pass",
            "smtp_from",
            "smtp_from_name",
            "smtp_tls",
            "telegram_bot_token",
            "telegram_bot_username",
        ]
        return {k: settings_store.get_value(session, k) for k in keys}


# Selectable display currencies: code -> (symbol, position). "none" = show the
# bare number. Amounts are never converted — this only changes the label.
CURRENCIES: dict[str, tuple[str, str]] = {
    "EUR": ("€", "suffix"),
    "USD": ("$", "prefix"),
    "GBP": ("£", "prefix"),
    "CHF": ("CHF", "suffix"),
    "none": ("", "suffix"),
}


def currency() -> dict:
    return _cached("currency", _load_currency)


def _load_currency() -> dict:
    with Session(engine) as session:
        code = settings_store.get_value(session, "currency") or "EUR"
    if code not in CURRENCIES:
        code = "EUR"
    sym, pos = CURRENCIES[code]
    return {
        "setting": code,                       # raw stored code (for the <select>)
        "code": "" if code == "none" else code,  # label text; empty when none
        "symbol": sym,
        "position": pos,
    }


def plex_config() -> dict:
    db = _read_all()
    s = get_settings()
    return {
        "token": db["plex_token"] or s.plex_token,
        "server_name": db["plex_server_name"] or s.plex_server_name,
        "server_id": db["plex_server_id"] or "",
        "account_email": db["plex_account_email"] or "",
        "direct_url": (db["plex_direct_url"] or s.plex_direct_url).strip(),
    }


def smtp_config() -> dict:
    db = _read_all()
    s = get_settings()
    port = db["smtp_port"]
    tls = db["smtp_tls"]
    return {
        "host": db["smtp_host"] or s.smtp_host,
        "port": int(port) if port else s.smtp_port,
        "user": db["smtp_user"] or s.smtp_user,
        "password": db["smtp_pass"] or s.smtp_pass,
        "from_addr": db["smtp_from"] or s.smtp_from,
        "from_name": db["smtp_from_name"] or s.smtp_from_name,
        "tls": (tls.lower() == "true") if tls is not None else s.smtp_tls,
    }


def telegram_config() -> dict:
    db = _read_all()
    s = get_settings()
    return {
        "token": db["telegram_bot_token"] or s.telegram_bot_token,
        "username": db["telegram_bot_username"] or s.telegram_bot_username,
    }


def local_login_visible() -> bool:
    return _cached("local_login_visible", _load_local_login_visible)


def _load_local_login_visible() -> bool:
    with Session(engine) as session:
        v = settings_store.get_value(session, "local_login_visible")
    return v != "false"  # default visible


def public_base_url() -> str:
    """External URL the app is reached at, without a trailing slash.

    DB setting first, ENV `PUBLIC_BASE_URL` as fallback. Every absolute link the
    app hands out is built from this: the Plex OAuth forward URLs, the CSRF
    origin allowlist, and the sign-in link in the invite email. A deploy behind a
    reverse proxy can't infer it, hence the setting."""
    return _cached("public_base_url", _load_public_base_url)


def _load_public_base_url() -> str:
    with Session(engine) as session:
        url = settings_store.get_value(session, "public_base_url")
    return (url or get_settings().public_base_url or "").strip().rstrip("/")


def cookies_secure() -> bool:
    """Secure flag for session cookies.

    True when either the configured base URL or the ENV fallback is https: a
    typo in Settings must not silently downgrade a working HTTPS deploy to
    cookies an on-path attacker can read."""
    return public_base_url().lower().startswith("https://") or (
        get_settings().public_base_url.lower().startswith("https://")
    )


def overseerr_config() -> dict:
    return _cached("overseerr", _load_overseerr)


def _load_overseerr() -> dict:
    with Session(engine) as session:
        url = settings_store.get_value(session, "overseerr_url")
        public_url = settings_store.get_value(session, "overseerr_public_url")
        key = settings_store.get_value(session, "overseerr_api_key")
        enabled = settings_store.get_value(session, "overseerr_enabled")
        dperm = settings_store.get_value(session, "overseerr_default_permissions")
    return {
        "url": (url or "").rstrip("/"),
        "public_url": (public_url or url or "").rstrip("/"),
        "api_key": key or "",
        "enabled": enabled == "true",
        "default_permissions": int(dperm) if (dperm and dperm.isdigit()) else 32,
    }


def plex_default_sections() -> list[str]:
    return _cached("plex_default_sections", _load_plex_default_sections)


def _load_plex_default_sections() -> list[str]:
    with Session(engine) as session:
        raw = settings_store.get_value(session, "plex_default_sections")
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def _parse_days(raw: str | None, *, lo: int, hi: int) -> list[int]:
    """Comma-separated ints -> sorted, de-duped, clamped to [lo, hi]. Junk ignored.
    Literal 'none' (any case) = an explicit empty list (phase disabled)."""
    if raw is None or raw.strip().lower() == "none":
        return []
    out: set[int] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            n = int(tok)
        except ValueError:
            continue
        if lo <= n <= hi:
            out.add(n)
    return sorted(out)


def reminder_schedule() -> dict:
    """Resolved reminder ladders. DB override -> ENV fallback.
    before: positive days-remaining buckets, e.g. [7, 3, 1].
    after:  overdue buckets as signed days_left <= 0, e.g. [0, -3]."""
    return _cached("reminder_schedule", _load_reminder_schedule)


def _load_reminder_schedule() -> dict:
    with Session(engine) as session:
        db_before = settings_store.get_value(session, "reminder_days_before")
        db_after = settings_store.get_value(session, "reminder_days_after")
    s = get_settings()
    # `is not None` (not truthiness): a stored "none" sentinel must survive as []
    # instead of falling through to the ENV default.
    before_raw = db_before if db_before is not None else s.reminder_days_before
    after_raw = db_after if db_after is not None else s.reminder_days_after
    before = _parse_days(before_raw, lo=1, hi=90)
    after = [-d for d in _parse_days(after_raw, lo=0, hi=90)]
    return {"before": before, "after": after}


def digest_lookahead() -> int:
    """Manager weekly-digest lookahead window in days. DB override -> ENV, clamp [1,90]."""
    return _cached("digest_lookahead", _load_digest_lookahead)


def _load_digest_lookahead() -> int:
    with Session(engine) as session:
        raw = settings_store.get_value(session, "digest_lookahead_days")
    s = get_settings()
    try:
        n = int(raw) if raw not in (None, "") else s.digest_lookahead_days
    except (ValueError, TypeError):
        n = s.digest_lookahead_days
    return max(1, min(90, n))


# Applied when the admin has never saved the setting. 0 (keep forever) is still
# reachable, but only by typing it explicitly.
DEFAULT_NOTIFICATION_RETENTION_DAYS = 30


def clamp_retention_days(n: int) -> int:
    """0 = keep forever; anything else is clamped to 1..3650 days.

    There is no 30-day floor any more. Every dedup_key that could resurrect a
    duplicate send already carries a date: reminders embed the day offset and the
    expiry date, and the manager digest embeds the ISO week *and* only fires on
    the manager's own digest_weekday. A pruned row therefore cannot regenerate the
    same key on a later day. The one time-invariant key is `welcome:{user_id}`,
    which prune_old_notifications() keeps regardless of age.
    """
    return 0 if n <= 0 else max(1, min(3650, n))


# Selectable UI color themes: stored/attribute value -> nothing else needed,
# the hex palettes live in src/input.css under [data-theme="..."]. "rame" is
# the default so a never-configured install (and any invalid/stale stored
# value) renders exactly like a page with no data-theme attribute at all.
COLOR_THEMES: tuple[str, ...] = ("rame", "inchiostro", "muschio")
DEFAULT_COLOR_THEME = "rame"


def color_theme() -> str:
    """Selected UI color theme (rame/inchiostro/muschio). DB-only setting;
    defaults to DEFAULT_COLOR_THEME when never configured or invalid."""
    return _cached("color_theme", _load_color_theme)


def _load_color_theme() -> str:
    with Session(engine) as session:
        raw = settings_store.get_value(session, "color_theme")
    return raw if raw in COLOR_THEMES else DEFAULT_COLOR_THEME


def notification_retention_days() -> int:
    """Days to keep notification_log rows (0 = keep forever). DB-only setting;
    defaults to DEFAULT_NOTIFICATION_RETENTION_DAYS when never configured."""
    return _cached("notification_retention_days", _load_notification_retention_days)


def _load_notification_retention_days() -> int:
    with Session(engine) as session:
        raw = settings_store.get_value(session, "notification_retention_days")
    if raw in (None, ""):
        return DEFAULT_NOTIFICATION_RETENTION_DAYS
    try:
        return clamp_retention_days(int(raw))
    except (ValueError, TypeError):
        # Only reachable if the row was hand-edited: the save handler always
        # stores a clamped int. Fail towards keeping data on a delete path.
        logging.getLogger("pum.config").warning(
            "notification_retention_days is not a number (%r) — keeping history "
            "forever until it is fixed in Settings.", raw,
        )
        return 0
