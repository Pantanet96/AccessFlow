"""Login / logout routes: local credentials + Plex OAuth (PIN flow)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app.auth import mfa, throttle
from app.auth.deps import get_current_user
from app.auth.plex_login import resolve_or_activate_user
from app.auth.service import authenticate_local
from app.auth.session import (
    clear_session_cookie,
    read_value,
    set_session_cookie,
    sign_value,
)
from app.db import get_session
from app.i18n import gettext as _
from app.models import AppUser
from app.security import password_problem
from app import runtime_config
from app.services import audit, plex_oauth
from app.templating import templates

router = APIRouter()

_PIN_COOKIE = "plex_pin"
_PIN_SALT = "plex-pin"
_MFA_COOKIE = "mfa_pending"
_MFA_SALT = "mfa-pending"
_MFA_MAX_AGE = 300  # password checked, code still owed: 5 minutes to type it


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
    weak = password_problem(password, user.username) is not None
    if user.password_weak != weak:
        user.password_weak = weak
        session.add(user)
        session.commit()
    if mfa.enabled(session, user.id):
        return _mfa_challenge(user, "local")
    audit.record(session, user.id, "login", detail={"method": "local"})
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, user)
    return response


def _mfa_challenge(user: AppUser, method: str) -> RedirectResponse:
    # No session yet: the first factor only earns the code prompt. The cookie
    # carries session_gen so a password change in between voids it.
    response = RedirectResponse("/login/mfa", status_code=303)
    response.set_cookie(
        _MFA_COOKIE,
        sign_value({"uid": user.id, "gen": user.session_gen or 0, "method": method},
                   salt=_MFA_SALT),
        max_age=_MFA_MAX_AGE, httponly=True, samesite="lax",
        secure=runtime_config.cookies_secure(),
    )
    return response


def _mfa_state(request: Request, session: Session) -> tuple[AppUser, str] | None:
    raw = request.cookies.get(_MFA_COOKIE)
    state = read_value(raw, salt=_MFA_SALT, max_age=_MFA_MAX_AGE) if raw else None
    if not state:
        return None
    user = session.get(AppUser, state["uid"])
    if user is None or not user.is_active or (user.session_gen or 0) != state["gen"]:
        return None
    return user, state.get("method", "local")


@router.get("/login/mfa", response_class=HTMLResponse)
def login_mfa_form(request: Request, session: Session = Depends(get_session)):
    if _mfa_state(request, session) is None:
        return RedirectResponse("/login/local", status_code=303)
    return templates.TemplateResponse(request, "login_mfa.html", {"error": None})


@router.post("/login/mfa")
def login_mfa_submit(
    request: Request,
    code: str = Form(...),
    session: Session = Depends(get_session),
):
    pending = _mfa_state(request, session)
    if pending is None:
        return RedirectResponse("/login/local", status_code=303)
    user, method = pending
    ip = _client_ip(request)
    # Per-account key, hard: only someone who already passed the first factor
    # can reach this, so locking it can't be used to keep the owner out.
    tkey = f"mfa:{user.id}"
    locked = throttle.check_locked(tkey, ip)
    if locked is None:
        kind = mfa.verify(session, user.id, code)
        if kind is not None:
            throttle.reset(tkey, ip)
            audit.record(session, user.id, "login",
                         detail={"method": method, "mfa": kind})
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie(_MFA_COOKIE)
            set_session_cookie(response, user)
            return response
        locked = throttle.register_failure(tkey, ip)
    if locked is not None:
        msg = _("Too many attempts. Try again in about %(minutes)d minute(s).") % {
            "minutes": (locked + 59) // 60
        }
        return templates.TemplateResponse(
            request, "login_mfa.html", {"error": msg}, status_code=429)
    return templates.TemplateResponse(
        request, "login_mfa.html", {"error": _("Invalid code.")}, status_code=401)


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


def _plex_limited(request: Request) -> HTMLResponse | None:
    # Public and anonymous, yet each hit blocks a worker thread on plex.tv (the
    # callback up to ~3s): a flood would starve the whole app.
    remaining = throttle.plex_rate_limited(_client_ip(request))
    if remaining is None:
        return None
    msg = _("Too many attempts. Try again in about %(minutes)d minute(s).") % {
        "minutes": (remaining + 59) // 60
    }
    return templates.TemplateResponse(
        request, "login.html",
        {"error": msg, "local_login_visible": runtime_config.local_login_visible()},
        status_code=429,
    )


@router.get("/login/plex")
def plex_start(request: Request):
    if (limited := _plex_limited(request)) is not None:
        return limited
    pin = plex_oauth.create_pin()
    forward = runtime_config.public_base_url() + "/login/plex/callback"
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


@router.get("/login/plex/callback")
def plex_callback(request: Request, session: Session = Depends(get_session)):
    raw = request.cookies.get(_PIN_COOKIE)
    state = read_value(raw, salt=_PIN_SALT) if raw else None
    if not state:
        return RedirectResponse("/login", status_code=303)
    if (limited := _plex_limited(request)) is not None:
        return limited

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

    # Plex sign-in is phishable: whoever started the PIN gets the session once
    # the victim approves it on plex.tv. A second factor, if set, still applies.
    if mfa.enabled(session, user.id):
        response = _mfa_challenge(user, "plex")
        response.delete_cookie(_PIN_COOKIE)
        return response
    audit.record(session, user.id, "login", detail={"method": "plex"})
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(_PIN_COOKIE)
    set_session_cookie(response, user)
    return response
