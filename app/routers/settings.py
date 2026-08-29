"""SuperAdmin settings: connect Plex (OAuth + server pick), SMTP, Telegram."""
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

import json
import secrets
from urllib.parse import urlparse

from app import runtime_config
from app.auth.deps import require_role
from app.auth.session import read_value, sign_value
from app.config import get_settings
from app.db import get_session
from app.i18n import gettext as _
from app.models import AppUser, Role
from app.services import (
    audit,
    mail_service,
    notification_templates,
    overseerr_service,
    plex_oauth,
    plex_service,
    settings_store,
    telegram_service,
)
from app.templating import templates

router = APIRouter()

_PIN_COOKIE = "plex_setup_pin"
_PIN_SALT = "plex-setup-pin"

_GROUPS = ("plex", "notifiche", "utenti", "sistema")


def _valid_group(group: str) -> str:
    return group if group in _GROUPS else "plex"


def _context(session: Session, viewer: AppUser, group: str = "plex", **extra) -> dict:
    plex = runtime_config.plex_config()
    smtp = runtime_config.smtp_config()
    tg = runtime_config.telegram_config()
    ov = runtime_config.overseerr_config()
    sched = runtime_config.reminder_schedule()
    sections = []
    if plex["token"] and plex["server_name"]:
        sections = plex_service.list_sections_safe()
    ctx = {
        "current_user": viewer,
        "group": group,
        "plex_connected": bool(plex["token"]),
        "plex_email": plex["account_email"],
        "plex_server": plex["server_name"],
        "sections": sections,
        "default_sections": runtime_config.plex_default_sections(),
        "reminder_days_before": ",".join(str(d) for d in sched["before"]),
        "reminder_days_after": ",".join(str(-d) for d in sched["after"]),
        "digest_lookahead_days": runtime_config.digest_lookahead(),
        "notification_retention_days": runtime_config.notification_retention_days(),
        "currency_setting": runtime_config.currency()["setting"],
        "currencies": list(runtime_config.CURRENCIES),
        "local_login_visible": runtime_config.local_login_visible(),
        "public_base_url": runtime_config.public_base_url(),
        "color_theme_setting": runtime_config.color_theme(),
        "color_themes": list(runtime_config.COLOR_THEMES),
        "smtp": smtp,
        "smtp_pass_set": bool(smtp["password"]),
        "telegram_username": tg["username"],
        "telegram_token_set": bool(tg["token"]),
        "overseerr_url": ov["url"],
        "overseerr_public_url": ov["public_url"],
        "overseerr_enabled": ov["enabled"],
        "overseerr_key_set": bool(ov["api_key"]),
        "overseerr_default_permissions": ov["default_permissions"],
        "telegram_bot_match": (
            overseerr_service.bot_match() if ov["enabled"] else {"checked": False}
        ),
        "message": None,
        "error": None,
    }
    ctx.update(extra)
    return ctx


def _admin(viewer: AppUser = Depends(require_role(Role.superadmin))) -> AppUser:
    return viewer


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    group: str = Query("plex"),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "settings.html", _context(session, viewer, group=_valid_group(group))
    )


# ---- SMTP ----

@router.post("/settings/smtp", response_class=HTMLResponse)
def save_smtp(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_user: str = Form(""),
    smtp_pass: str = Form(""),
    smtp_from: str = Form(""),
    smtp_from_name: str = Form(""),
    smtp_tls: str = Form("false"),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    settings_store.set_value(session, "smtp_host", smtp_host)
    settings_store.set_value(session, "smtp_port", smtp_port)
    settings_store.set_value(session, "smtp_user", smtp_user)
    settings_store.set_value(session, "smtp_from", smtp_from)
    settings_store.set_value(session, "smtp_from_name", smtp_from_name)
    settings_store.set_value(session, "smtp_tls", "true" if smtp_tls == "on" else "false")
    if smtp_pass:  # blank keeps the existing password
        settings_store.set_value(session, "smtp_pass", smtp_pass)
    audit.record(session, viewer.id, "settings_smtp")
    return RedirectResponse("/settings?group=notifiche", status_code=303)


@router.post("/settings/smtp/test", response_class=HTMLResponse)
def test_smtp(
    request: Request,
    to: str = Form(...),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    try:
        ok = mail_service.send_email(
            to, _("AccessFlow — test email"), _("SMTP is configured correctly.")
        )
        extra = (
            {"message": _("Test email sent to %s") % to}
            if ok
            else {"error": _("SMTP not configured (no host).")}
        )
    except Exception as exc:  # noqa: BLE001
        extra = {"error": _("Send failed: %s") % exc}
    return templates.TemplateResponse(
        request, "settings.html", _context(session, viewer, group="notifiche", **extra)
    )


# ---- Telegram ----

@router.post("/settings/telegram", response_class=HTMLResponse)
def save_telegram(
    request: Request,
    telegram_bot_token: str = Form(""),
    telegram_bot_username: str = Form(""),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    settings_store.set_value(session, "telegram_bot_username", telegram_bot_username)
    if telegram_bot_token:  # blank keeps the existing token
        settings_store.set_value(session, "telegram_bot_token", telegram_bot_token)
    audit.record(session, viewer.id, "settings_telegram")
    return RedirectResponse("/settings?group=notifiche", status_code=303)


@router.post("/settings/telegram/test", response_class=HTMLResponse)
def test_telegram(
    request: Request,
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    try:
        me = telegram_service.get_me()
        msg = _("Bot OK: @%s") % me.get("username", "?")
        # If the SuperAdmin linked their Telegram, send a test message too.
        if viewer.telegram_id:
            telegram_service.send_message(
                viewer.telegram_id, _("AccessFlow — test message.")
            )
            msg += " " + _("(test message sent to your Telegram)")
        result = {"ok": True, "text": msg}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "text": _("Bot test failed: %s") % exc}
    return templates.TemplateResponse(
        request, "settings.html",
        _context(session, viewer, group="notifiche", telegram_test=result),
    )


# ---- Plex connect (OAuth PIN) ----

@router.get("/settings/plex/connect")
def plex_connect(viewer: AppUser = Depends(_admin)):
    pin = plex_oauth.create_pin()
    forward = runtime_config.public_base_url() + "/settings/plex/callback"
    url = plex_oauth.build_auth_url(pin["code"], forward)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        _PIN_COOKIE,
        sign_value({"id": pin["id"]}, salt=_PIN_SALT),
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=runtime_config.cookies_secure(),
    )
    return response


@router.get("/settings/plex/callback", response_class=HTMLResponse)
def plex_callback(
    request: Request,
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    raw = request.cookies.get(_PIN_COOKIE)
    state = read_value(raw, salt=_PIN_SALT) if raw else None
    if not state:
        return RedirectResponse("/settings", status_code=303)

    token = plex_oauth.wait_for_pin(state["id"])
    if not token:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _context(session, viewer, error=_("Plex sign-in was not completed.")),
        )

    email, servers = plex_oauth.list_servers(token)
    settings_store.set_value(session, "plex_token", token)
    settings_store.set_value(session, "plex_account_email", email or "")
    audit.record(session, viewer.id, "plex_connect", detail={"email": email})

    response = templates.TemplateResponse(
        request,
        "settings_plex_select.html",
        {"current_user": viewer, "servers": servers, "email": email},
    )
    response.delete_cookie(_PIN_COOKIE)
    return response


@router.post("/settings/plex/server")
def plex_select_server(
    server: str = Form(...),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    # server form value = "id|name"
    server_id, _sep, server_name = server.partition("|")
    settings_store.set_value(session, "plex_server_id", server_id)
    settings_store.set_value(session, "plex_server_name", server_name)
    audit.record(session, viewer.id, "plex_select_server", detail={"server": server_name})
    return RedirectResponse("/settings?group=plex", status_code=303)


@router.post("/settings/plex/disconnect")
def plex_disconnect(
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    for key in ("plex_token", "plex_server_id", "plex_server_name", "plex_account_email"):
        settings_store.delete_value(session, key)
    audit.record(session, viewer.id, "plex_disconnect")
    return RedirectResponse("/settings?group=plex", status_code=303)


@router.post("/settings/plex/libraries")
def save_default_libraries(
    libraries: list[str] = Form(default=[]),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    settings_store.set_value(session, "plex_default_sections", json.dumps(libraries))
    audit.record(session, viewer.id, "set_default_libraries", detail={"count": len(libraries)})
    return RedirectResponse("/settings?group=plex", status_code=303)


# ---- Display currency ----

@router.post("/settings/currency")
def save_currency(
    currency: str = Form("EUR"),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    if currency not in runtime_config.CURRENCIES:
        currency = "EUR"
    settings_store.set_value(session, "currency", currency)
    audit.record(session, viewer.id, "settings_currency", detail={"currency": currency})
    return RedirectResponse("/settings?group=utenti", status_code=303)


# ---- Local login link visibility ----

@router.post("/settings/local-login")
def save_local_login_visible(
    local_login_visible: str = Form("false"),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    settings_store.set_value(
        session, "local_login_visible", "true" if local_login_visible == "on" else "false"
    )
    audit.record(session, viewer.id, "settings_local_login_visible")
    return RedirectResponse("/settings?group=sistema", status_code=303)


# ---- Color theme ----

@router.post("/settings/color-theme")
def save_color_theme(
    color_theme: str = Form(runtime_config.DEFAULT_COLOR_THEME),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    if color_theme not in runtime_config.COLOR_THEMES:
        color_theme = runtime_config.DEFAULT_COLOR_THEME
    settings_store.set_value(session, "color_theme", color_theme)
    audit.record(session, viewer.id, "settings_color_theme", detail={"theme": color_theme})
    return RedirectResponse("/settings?group=sistema", status_code=303)


# ---- Reminder / dunning schedule ----

@router.post("/settings/reminders")
def save_reminders(
    reminder_days_before: str = Form(""),
    reminder_days_after: str = Form(""),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    before = runtime_config._parse_days(reminder_days_before, lo=1, hi=90)
    after = runtime_config._parse_days(reminder_days_after, lo=0, hi=90)
    # Empty parsed list -> store sentinel "none": an empty string would DELETE the
    # key in settings_store and silently revert to the ENV default.
    settings_store.set_value(
        session, "reminder_days_before",
        ",".join(map(str, before)) if before else "none",
    )
    settings_store.set_value(
        session, "reminder_days_after",
        ",".join(map(str, after)) if after else "none",
    )
    audit.record(
        session, viewer.id, "settings_reminders",
        detail={"before": before, "after": after},
    )
    return RedirectResponse("/settings?group=utenti", status_code=303)


@router.post("/settings/digest")
def save_digest(
    digest_lookahead_days: str = Form(""),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    try:
        n = max(1, min(90, int(digest_lookahead_days)))
    except (ValueError, TypeError):
        n = get_settings().digest_lookahead_days
    settings_store.set_value(session, "digest_lookahead_days", str(n))
    audit.record(session, viewer.id, "settings_digest", detail={"lookahead_days": n})
    return RedirectResponse("/settings?group=utenti", status_code=303)


@router.post("/settings/notification-retention")
def save_notification_retention(
    notification_retention_days: str = Form(""),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    # The clamp lives in runtime_config so the value we store and the value the
    # pruner reads can never drift apart. A blank/garbled field falls back to the
    # default rather than to 0 — "keep forever" has to be typed on purpose.
    try:
        n = int(notification_retention_days)
    except (ValueError, TypeError):
        n = runtime_config.DEFAULT_NOTIFICATION_RETENTION_DAYS
    n = runtime_config.clamp_retention_days(n)
    settings_store.set_value(session, "notification_retention_days", str(n))
    audit.record(
        session, viewer.id, "settings_notification_retention", detail={"days": n}
    )
    return RedirectResponse("/settings?group=notifiche", status_code=303)


# ---- Notification templates (per-channel, per-locale; admin-overridable) ----

def _templates_ctx(session: Session, viewer: AppUser, **extra) -> dict:
    ctx = {
        "current_user": viewer,
        "entries": notification_templates.editor_entries(session),
        "message": None,
        "error": None,
    }
    ctx.update(extra)
    return ctx


@router.get("/settings/templates", response_class=HTMLResponse)
def templates_page(
    request: Request,
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "settings_templates.html", _templates_ctx(session, viewer)
    )


@router.post("/settings/templates/save", response_class=HTMLResponse)
def save_template(
    request: Request,
    tpl_type: str = Form(...),
    part: str = Form(...),
    locale: str = Form(...),
    text: str = Form(""),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    if (
        tpl_type not in notification_templates.TYPES
        or part not in notification_templates.PARTS
        or locale not in notification_templates.LOCALES
    ):
        return RedirectResponse("/settings/templates", status_code=303)
    err = notification_templates.validate(text)
    if err:
        return templates.TemplateResponse(
            request, "settings_templates.html",
            _templates_ctx(session, viewer, error=_("Template error: %s") % err),
        )
    # Empty text deletes the override -> reverts to the built-in default.
    settings_store.set_value(session, f"ntpl:{tpl_type}:{part}:{locale}", text.strip())
    audit.record(
        session, viewer.id, "settings_template",
        detail={"type": tpl_type, "part": part, "locale": locale},
    )
    return templates.TemplateResponse(
        request, "settings_templates.html",
        _templates_ctx(session, viewer, message=_("Template saved.")),
    )


@router.post("/settings/templates/reset")
def reset_template(
    tpl_type: str = Form(...),
    part: str = Form(...),
    locale: str = Form(...),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    settings_store.delete_value(session, f"ntpl:{tpl_type}:{part}:{locale}")
    audit.record(
        session, viewer.id, "settings_template",
        detail={"type": tpl_type, "part": part, "locale": locale, "reset": True},
    )
    return RedirectResponse("/settings/templates", status_code=303)


# ---- Public base URL ----

def _clean_base_url(raw: str) -> str:
    """Normalize an admin-typed base URL, or "" if it isn't usable.
    A bare host gets https:// (the common paste); trailing slashes go, because
    every caller concatenates a path straight onto this."""
    url = raw.strip()
    if not url:
        return ""
    if "://" in url:
        scheme, _sep, rest = url.partition("://")
        if scheme.lower() not in ("http", "https") or not rest.strip("/"):
            return ""
    else:
        url = "https://" + url
    url = url.rstrip("/")
    # hostname, not netloc: "https://http:" has a netloc but nothing to reach.
    if not urlparse(url).hostname:
        return ""
    return url


@router.post("/settings/public-url")
def save_public_url(
    request: Request,
    public_base_url: str = Form(""),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    raw = public_base_url.strip()
    url = _clean_base_url(raw)
    if raw and not url:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _context(
                session, viewer, group="sistema",
                error=_("That doesn't look like a valid URL: %s") % raw,
            ),
            status_code=400,
        )
    # Blank clears the override and falls back to the PUBLIC_BASE_URL env var.
    settings_store.set_value(session, "public_base_url", url)
    audit.record(session, viewer.id, "settings_public_url", detail={"url": url})
    return RedirectResponse("/settings?group=sistema", status_code=303)


# ---- Overseerr ----

@router.post("/settings/overseerr")
def save_overseerr(
    overseerr_url: str = Form(""),
    overseerr_public_url: str = Form(""),
    overseerr_api_key: str = Form(""),
    overseerr_default_permissions: str = Form("32"),
    overseerr_enabled: str = Form("false"),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    settings_store.set_value(session, "overseerr_url", overseerr_url.strip())
    settings_store.set_value(
        session, "overseerr_public_url", overseerr_public_url.strip()
    )
    settings_store.set_value(
        session, "overseerr_enabled", "true" if overseerr_enabled == "on" else "false"
    )
    if overseerr_default_permissions.isdigit():
        settings_store.set_value(
            session, "overseerr_default_permissions", overseerr_default_permissions
        )
    if overseerr_api_key:  # blank keeps existing key
        settings_store.set_value(session, "overseerr_api_key", overseerr_api_key)
    audit.record(session, viewer.id, "settings_overseerr")
    return RedirectResponse("/settings?group=plex", status_code=303)


# ---- Encryption key rotation ----

@router.post("/settings/rotate-key", response_class=HTMLResponse)
def rotate_key(
    request: Request,
    new_secret: str = Form(""),
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    new_secret = new_secret.strip()
    if not new_secret:
        new_secret = secrets.token_urlsafe(48)  # auto-generate strong secret
    elif len(new_secret) < 16:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _context(
                session, viewer, group="sistema",
                error=_("New secret must be at least 16 characters."),
            ),
        )
    if new_secret == get_settings().app_secret_key:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _context(
                session, viewer, group="sistema",
                error=_("New secret is identical to the current one."),
            ),
        )
    try:
        count = settings_store.rotate_secret(session, new_secret)
    except Exception as exc:  # noqa: BLE001 — abort cleanly, store untouched on raise
        return templates.TemplateResponse(
            request,
            "settings.html",
            _context(
                session, viewer, group="sistema",
                error=_("Key rotation failed: %s") % exc,
            ),
        )
    audit.record(session, viewer.id, "settings_rotate_key", detail={"rows": count})
    return templates.TemplateResponse(
        request,
        "settings_key_rotated.html",
        {"current_user": viewer, "new_secret": new_secret, "rows": count},
    )


@router.post("/settings/overseerr/test", response_class=HTMLResponse)
def test_overseerr(
    request: Request,
    viewer: AppUser = Depends(_admin),
    session: Session = Depends(get_session),
):
    try:
        status_info = overseerr_service.test()
        result = {
            "ok": True,
            "text": _("Connection OK and API key valid — Overseerr version %s")
            % status_info.get("version", "?"),
        }
    except overseerr_service.OverseerrUrlError as exc:
        result = {
            "ok": False,
            "text": _("URL unreachable — check the Base URL. (%s)") % exc,
        }
    except overseerr_service.OverseerrKeyError as exc:
        result = {
            "ok": False,
            "text": _("URL OK but the API key was rejected — check the API key. (%s)")
            % exc,
        }
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "text": _("Overseerr test failed: %s") % exc}
    return templates.TemplateResponse(
        request, "settings.html",
        _context(session, viewer, group="plex", overseerr_test=result),
    )
