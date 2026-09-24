"""Self-service profile: edit own contacts + personal info."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app import runtime_config
from app.auth import mfa, throttle
from app.auth.deps import require_user
from app.auth.session import set_session_cookie
from app.db import engine, get_session
from app.i18n import gettext as _
from app.models import AppUser, Role
from app.security import hash_password, password_problem, verify_password
from app.services import (
    audit,
    overseerr_service,
    telegram_service,
    users as users_svc,
)
from app.services.telegram_link import make_link_token
from app.templating import templates

router = APIRouter()


def _send_tg_confirm(chat_id) -> str:
    """Ping a freshly-set chat_id so we know if it's actually reachable."""
    return telegram_service.deliver(
        chat_id,
        _("✅ Telegram notifications are now connected to AccessFlow."),
    )


def _tg_status_msgs(status: str) -> tuple[str | None, str | None]:
    """Map a deliver() status to a (message, error) pair for the profile page."""
    if status == "ok":
        return _("Confirmation message sent on Telegram."), None
    if status == "forbidden":
        return None, _(
            "Saved, but the user must open the bot and press Start before "
            "Telegram notifications can be delivered."
        )
    if status == "error":
        return None, _("Saved, but the Telegram confirmation could not be sent.")
    if status == "taken":
        return None, _("This Telegram ID is already linked to another account.")
    return None, None  # no_config / unknown -> nothing to report


def _overseerr_sync_ctx(user: AppUser) -> dict:
    """Best-effort Overseerr Telegram chat-id state for the link page."""
    ctx = {"ov_enabled": False, "ov_chat_id": None, "ov_mismatch": False}
    if not overseerr_service.enabled() or not (user.plex_account_id or user.plex_email):
        return ctx
    ctx["ov_enabled"] = True
    try:
        ov_cid = overseerr_service.get_user_chat_id(
            plex_id=user.plex_account_id, email=user.plex_email
        )
    except Exception:  # noqa: BLE001
        return ctx
    ctx["ov_chat_id"] = ov_cid
    pum_cid = user.telegram_id or None
    ctx["ov_mismatch"] = bool(pum_cid or ov_cid) and (pum_cid != ov_cid)
    return ctx


@router.get("/telegram/link")
def telegram_link(request: Request, user: AppUser = Depends(require_user)):
    # Telegram setup now lives in a section of /profile; keep the old URL working.
    return RedirectResponse("/profile", status_code=303)


@router.post("/telegram/sync-overseerr", response_class=HTMLResponse)
def sync_overseerr_chat_id(
    request: Request,
    direction: str = Form(...),
    user: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    message = error = None
    if not overseerr_service.enabled():
        error = _("Overseerr integration is not enabled.")
    elif direction == "to_overseerr":
        if not user.telegram_id:
            error = _("Set your Telegram ID first.")
        else:
            try:
                ok = overseerr_service.push_user_chat_id(
                    user.telegram_id,
                    plex_id=user.plex_account_id,
                    email=user.plex_email,
                )
                message = (
                    _("Overseerr now uses your AccessFlow Telegram ID.")
                    if ok else _("Your Overseerr account was not found.")
                )
            except Exception as exc:  # noqa: BLE001
                error = _("Sync failed: %s") % exc
    elif direction == "from_overseerr":
        try:
            cid = overseerr_service.get_user_chat_id(
                plex_id=user.plex_account_id, email=user.plex_email
            )
            if cid and users_svc.telegram_id_taken(session, cid, user.id):
                error = _tg_status_msgs("taken")[1]
            elif cid:
                user.telegram_id = cid
                session.add(user)
                session.commit()
                message = _("Imported the Telegram ID from Overseerr.")
                m2, e2 = _tg_status_msgs(_send_tg_confirm(cid))
                if e2:
                    error = e2
                elif m2:
                    message = f"{message} {m2}"
            else:
                error = _("No Telegram ID found on Overseerr.")
        except Exception as exc:  # noqa: BLE001
            error = _("Sync failed: %s") % exc
    if message or direction in ("to_overseerr", "from_overseerr"):
        audit.record(session, user.id, "telegram_sync_overseerr",
                     detail={"direction": direction, "ok": bool(message)})
    return profile_form(request, user=user, tg_message=message, tg_error=error)


@router.get("/profile", response_class=HTMLResponse)
def profile_form(
    request: Request,
    user: AppUser = Depends(require_user),
    pwd_message: str | None = None,
    pwd_error: str | None = None,
    tg_message: str | None = None,
    tg_error: str | None = None,
    tg: str | None = None,
    mfa_message: str | None = None,
    mfa_error: str | None = None,
):
    # `tg` is a deliver()-status code carried across the post/redirect of a
    # profile save; map it to the localized confirmation message/warning.
    if tg:
        m, e = _tg_status_msgs(tg)
        tg_message = tg_message or m
        tg_error = tg_error or e
    token = make_link_token(user.id, bind=user.telegram_id or "")
    username = runtime_config.telegram_config()["username"]
    ctx = {
        "current_user": user,
        "saved": False,
        "bot_username": username,
        "token": token,
        "deep_link": f"https://t.me/{username}?start={token}" if username else None,
        "pwd_message": pwd_message,
        "pwd_error": pwd_error,
        "tg_message": tg_message,
        "tg_error": tg_error,
        "mfa_message": mfa_message,
        "mfa_error": mfa_error,
    }
    if user.password_hash:
        # Own short session: this handler is also called directly (no Depends).
        with Session(engine) as s:
            ctx["mfa_enabled"] = mfa.enabled(s, user.id)
            ctx["mfa_recovery_left"] = mfa.recovery_left(s, user.id)
    ctx.update(_overseerr_sync_ctx(user))
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.post("/profile/password", response_class=HTMLResponse)
def profile_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    message = error = None
    # Rate-limit current-password guesses: a stolen session cookie must not allow
    # unlimited online brute-forcing of the current password to take over the account.
    # Namespaced keys (both username and ip) so this limiter is independent from
    # the login lockout and can't accidentally lock a user out of login.
    ip = request.client.host if request.client else "unknown"
    tkey, tip = f"pwchange:{user.id}", f"pw:{ip}"
    locked = throttle.check_locked(tkey, tip)
    if locked is not None:
        mins = (locked + 59) // 60
        return profile_form(request, user=user, pwd_error=_(
            "Too many attempts. Try again in about %(minutes)d minute(s)."
        ) % {"minutes": mins})
    if not user.password_hash:
        error = _("This account has no password (Plex login only).")
    elif not verify_password(current_password, user.password_hash):
        throttle.register_failure(tkey, tip)
        error = _("Current password is incorrect.")
    elif (problem := password_problem(new_password, user.username)) is not None:
        error = _(problem)
    elif new_password != confirm_password:
        error = _("The new passwords do not match.")
    else:
        throttle.reset(tkey, tip)
        user.password_hash = hash_password(new_password)
        user.password_weak = False  # it just passed the policy
        # Invalidate every existing session cookie (a changed password should log
        # out other devices / any stolen cookie); re-issue one for THIS session.
        user.session_gen = (user.session_gen or 0) + 1
        session.add(user)
        session.commit()
        audit.record(session, user.id, "change_password", "app_user", user.id)
        message = _("Password changed.")
        resp = profile_form(request, user=user, pwd_message=message)
        set_session_cookie(resp, user)
        return resp
    return profile_form(request, user=user, pwd_message=message, pwd_error=error)


def _pw_throttle(request: Request, user: AppUser) -> tuple[str, str]:
    # Same keys as the password change: one budget for current-password guesses.
    ip = request.client.host if request.client else "unknown"
    return f"pwchange:{user.id}", f"pw:{ip}"


def _too_many(locked: int) -> str:
    return _("Too many attempts. Try again in about %(minutes)d minute(s).") % {
        "minutes": (locked + 59) // 60
    }


def _mfa_setup_page(request, user, session, error=None, codes=None, status_code=200):
    ctx = {"current_user": user, "error": error, "codes": codes}
    if codes is None:
        secret = mfa.pending_secret(session, user.id) or mfa.start_setup(session, user.id)
        uri = mfa.otpauth_uri(secret, user.username or str(user.id))
        ctx.update(secret=secret, qr=mfa.qr_svg(uri))
    return templates.TemplateResponse(request, "profile_mfa.html", ctx, status_code=status_code)


@router.get("/profile/mfa", response_class=HTMLResponse)
def profile_mfa_setup(
    request: Request,
    user: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not user.password_hash or mfa.enabled(session, user.id):
        return RedirectResponse("/profile", status_code=303)
    return _mfa_setup_page(request, user, session)


@router.post("/profile/mfa/enable", response_class=HTMLResponse)
def profile_mfa_enable(
    request: Request,
    current_password: str = Form(...),
    code: str = Form(...),
    user: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not user.password_hash or mfa.enabled(session, user.id):
        return RedirectResponse("/profile", status_code=303)
    # Password required too: a stolen session must not be able to turn MFA on
    # and lock the real owner out.
    tkey, tip = _pw_throttle(request, user)
    if (locked := throttle.check_locked(tkey, tip)) is not None:
        return _mfa_setup_page(request, user, session, _too_many(locked), status_code=429)
    if not verify_password(current_password, user.password_hash):
        throttle.register_failure(tkey, tip)
        return _mfa_setup_page(request, user, session, _("Current password is incorrect."),
                               status_code=400)
    codes = mfa.enable(session, user.id, code)
    if codes is None:
        throttle.register_failure(tkey, tip)
        return _mfa_setup_page(request, user, session,
                               _("Invalid code. Check the time on your phone and try again."),
                               status_code=400)
    throttle.reset(tkey, tip)
    # Log out every other session: one may be the reason MFA is going on.
    user.session_gen = (user.session_gen or 0) + 1
    session.add(user)
    session.commit()
    audit.record(session, user.id, "enable_mfa", "app_user", user.id)
    resp = _mfa_setup_page(request, user, session, codes=codes)
    set_session_cookie(resp, user)
    return resp


@router.post("/profile/mfa/disable", response_class=HTMLResponse)
def profile_mfa_disable(
    request: Request,
    current_password: str = Form(...),
    code: str = Form(...),
    user: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not user.password_hash or not mfa.enabled(session, user.id):
        return RedirectResponse("/profile", status_code=303)
    tkey, tip = _pw_throttle(request, user)
    if (locked := throttle.check_locked(tkey, tip)) is not None:
        return profile_form(request, user=user, mfa_error=_too_many(locked))
    if not verify_password(current_password, user.password_hash) or \
            mfa.verify(session, user.id, code) is None:
        throttle.register_failure(tkey, tip)
        return profile_form(request, user=user,
                            mfa_error=_("Wrong password or code."))
    throttle.reset(tkey, tip)
    mfa.disable(session, user.id)
    audit.record(session, user.id, "disable_mfa", "app_user", user.id)
    return profile_form(request, user=user,
                        mfa_message=_("Two-step verification turned off."))


@router.post("/profile", response_class=HTMLResponse)
def profile_save(
    request: Request,
    real_name: str = Form(...),
    notify_email: str = Form(""),
    telegram_id: str = Form(""),
    locale: str = Form("it"),
    notify_via_email: str = Form("off"),
    notify_via_telegram: str = Form("off"),
    digest_enabled: str = Form("off"),
    digest_weekday: str = Form("0"),
    user: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    # A Telegram chat id is always an integer; reject anything else so an
    # authenticated user can't make the bot ping arbitrary strings (Fix #9).
    tid = (telegram_id or "").strip()
    taken = bool(tid) and users_svc.telegram_id_taken(session, tid, user.id)
    if taken or (tid and not tid.lstrip("-").isdigit()):
        telegram_id = user.telegram_id or ""
    else:
        telegram_id = tid

    # Capture what the user actually changed, for the audit trail.
    changed = []
    if real_name and real_name != user.real_name:
        changed.append("real_name")
    if (notify_email or None) != (user.notify_email or None):
        changed.append("notify_email")
    if (telegram_id or None) != (user.telegram_id or None):
        changed.append("telegram_id")
    if locale != user.locale:
        changed.append("locale")
    if (notify_via_email == "on") != user.notify_via_email:
        changed.append("notify_via_email")
    if (notify_via_telegram == "on") != user.notify_via_telegram:
        changed.append("notify_via_telegram")

    # Digest prefs are manager-only (a plain user's form omits them, so don't clobber).
    is_mgr = user.role != Role.user
    if is_mgr:
        if (digest_enabled == "on") != user.digest_enabled:
            changed.append("digest_enabled")
        if digest_weekday.isdigit() and int(digest_weekday) != user.digest_weekday:
            changed.append("digest_weekday")

    users_svc.update_profile(
        session,
        user,
        real_name=real_name,
        notify_email=notify_email,
        telegram_id=telegram_id,
        locale=locale,
        notify_via_email=(notify_via_email == "on"),
        notify_via_telegram=(notify_via_telegram == "on"),
        digest_enabled=(digest_enabled == "on") if is_mgr else None,
        digest_weekday=(int(digest_weekday) if (is_mgr and digest_weekday.isdigit()) else None),
    )
    if changed:
        audit.record(session, user.id, "update_profile", "app_user", user.id,
                     {"changed": changed})
    # If the Telegram ID was set/changed manually, confirm it's reachable now
    # (a bot can't message a user who never pressed Start). Carry the result
    # across the redirect so the page can warn if the ID is unusable.
    tg = "taken" if taken else None
    if "telegram_id" in changed and (telegram_id or "").strip():
        status = _send_tg_confirm(telegram_id.strip())
        if status != "no_config":
            tg = status
    url = f"/profile?tg={tg}" if tg else "/profile"
    return RedirectResponse(url, status_code=303)
