"""Login / logout routes: local credentials + Plex OAuth (PIN flow)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app.auth import throttle
from app.auth.deps import get_current_user
from app.auth.plex_login import resolve_or_activate_user
from app.auth.service import authenticate_local
from app.auth.session import (
    clear_session_cookie,
    read_value,
    set_session_cookie,
    sign_value,
)
from app.config import get_settings
from app.db import get_session
from app.i18n import gettext as _
from app.models import AppUser
from app import runtime_config
from app.services import audit, plex_oauth
from app.templating import templates

router = APIRouter()

_PIN_COOKIE = "plex_pin"
_PIN_SALT = "plex-pin"


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, user: AppUser | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"error": None, "local_login_visible": runtime_config.local_login_visible()},
    )


@router.get("/login/local", response_class=HTMLResponse)
def login_local_form(request: Request, user: AppUser | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login_local.html", {"error": None})


def _client_ip(request: Request) -> str:
    # uvicorn (--proxy-headers + --forwarded-allow-ips <proxy>) has already resolved
    # request.client from the TRUSTED forwarders only. Don't re-read XFF by hand —
    # an untrusted client could forge it to dodge the per-IP lockout (Fix #3).
    return request.client.host if request.client else "unknown"


def _locked_response(request: Request, remaining: int):
    minutes = (remaining + 59) // 60  # round up to whole minutes
    msg = _(
        "Too many failed attempts. Try again in about %(minutes)d minute(s)."
    ) % {"minutes": minutes}
    response = templates.TemplateResponse(
        request, "login_local.html", {"error": msg}, status_code=429
    )
    response.headers["Retry-After"] = str(remaining)
    return response


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    ip = _client_ip(request)

    # 1. Hard block only if THIS IP is locked (an attacker hammering from one
    #    host). The per-username lock is intentionally NOT a hard block: anyone
    #    could otherwise lock a known user (the superadmin) out from any IP.
    ip_remaining = throttle.ip_locked(ip)
    if ip_remaining is not None:
        return _locked_response(request, ip_remaining)

    # 2. Verify credentials. Even when the username is locked we reach here, so a
    #    correct password lets the legitimate user in; wrong guesses still fail.
    user = authenticate_local(session, username, password)
    if user is None:
        # 3. Failure: count it (by username AND by IP).
        locked_for = throttle.register_failure(username, ip)
        if locked_for is not None:
            audit.record(
                session, None, "login_lockout",
                detail={"username": username, "ip": ip, "lock_seconds": locked_for},
            )
            return _locked_response(request, locked_for)
        # Generic error — never reveal whether the username exists.
        return templates.TemplateResponse(
            request,
            "login_local.html",
            {"error": _("Invalid username or password")},
            status_code=401,
        )

    # 4. Success: forgive counters for this username + IP.
    throttle.reset(username, ip)
    audit.record(session, user.id, "login", detail={"method": "local"})
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, user)
    return response


@router.post("/logout")
def logout(
    user: AppUser | None = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    # Bump session_gen so the just-cleared cookie can't be replayed server-side
    # (deleting the cookie only affects a cooperating client; a stolen copy lives on).
    if user is not None:
        user.session_gen = (user.session_gen or 0) + 1
        session.add(user)
        session.commit()
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/login/plex")
def plex_start():
    pin = plex_oauth.create_pin()
    forward = get_settings().public_base_url.rstrip("/") + "/login/plex/callback"
    url = plex_oauth.build_auth_url(pin["code"], forward)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        _PIN_COOKIE,
        sign_value({"id": pin["id"]}, salt=_PIN_SALT),
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookies_secure(),
    )
    return response


@router.get("/login/plex/callback")
def plex_callback(request: Request, session: Session = Depends(get_session)):
    raw = request.cookies.get(_PIN_COOKIE)
    state = read_value(raw, salt=_PIN_SALT) if raw else None
    if not state:
        return RedirectResponse("/login", status_code=303)

    auth_token = plex_oauth.wait_for_pin(state["id"])

    if not auth_token:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": _("Plex sign-in was not completed. Please try again."),
                "local_login_visible": runtime_config.local_login_visible(),
            },
            status_code=401,
        )

    account = plex_oauth.fetch_account(auth_token)
    user = resolve_or_activate_user(session, account)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": _("This Plex account is not invited."),
                "local_login_visible": runtime_config.local_login_visible(),
            },
            status_code=403,
        )

    audit.record(session, user.id, "login", detail={"method": "plex"})
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(_PIN_COOKIE)
    set_session_cookie(response, user)
    return response
