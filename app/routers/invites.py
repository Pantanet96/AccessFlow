"""Invite flow: admin invites an email to Plex; a pending Invite is recorded."""
import json
import logging
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from app.auth.deps import require_capability
from app.db import get_session
from app.i18n import gettext as _
from app.models import AppUser, Invite, InviteStatus, Role, utcnow
from app.permissions import Capability, outranks
from app import runtime_config
from app.services import audit, notifications, plex_service
from app.services import subscriptions as sub_svc
from app.services import users as users_svc
from app.templating import templates

router = APIRouter()

log = logging.getLogger("pum.invites")

_ALLOWED_ROLES = {Role.user, Role.admin, Role.moderator}


def _invitable_roles(viewer: AppUser) -> list[Role]:
    """Roles the viewer may invite: only ranks they strictly outrank. Prevents an
    admin from minting a peer admin via the invite flow (role assignment proper is
    superadmin-only via `manage_roles` + `outranks` in users.set_role). Ordered by
    rank so the dropdown is stable."""
    return sorted(
        (r for r in _ALLOWED_ROLES if outranks(viewer.role, r)),
        key=lambda r: r.value,
    )


def _render(request, viewer, session, error=None, message=None, status_code=200):
    pending = list(
        session.exec(
            select(Invite)
            .where(Invite.status == InviteStatus.pending)
            .order_by(Invite.created_at.desc())
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "invites/list.html",
        {
            "current_user": viewer,
            "pending": pending,
            "candidates": users_svc.manager_candidates(session),
            "plans": sub_svc.list_plans(session),
            "roles": _invitable_roles(viewer),
            "sections": plex_service.list_sections_safe(),
            "smtp_configured": bool(runtime_config.smtp_config()["host"]),
            "default_sections": runtime_config.plex_default_sections(),
            "error": error,
            "message": message,
        },
        status_code=status_code,
    )


@router.get("/invites", response_class=HTMLResponse)
def invites_page(
    request: Request,
    viewer: AppUser = Depends(require_capability(Capability.invite_user)),
    session: Session = Depends(get_session),
):
    return _render(request, viewer, session)


@router.post("/invites")
def create_invite(
    request: Request,
    email: str = Form(...),
    real_name: str = Form(...),
    role: str = Form("user"),
    manager_id: str = Form(""),
    plan_slug: str = Form(""),
    trial_days: str = Form(""),
    libraries: list[str] = Form(default=[]),
    viewer: AppUser = Depends(require_capability(Capability.invite_user)),
    session: Session = Depends(get_session),
):
    try:
        intended_role = Role(role)
    except ValueError:
        intended_role = Role.user
    # Never let an inviter mint a role they don't outrank (e.g. an admin inviting
    # another admin). Fall back to the lowest role, which every inviter outranks.
    if intended_role not in _invitable_roles(viewer):
        intended_role = Role.user

    plan = sub_svc.get_plan_by_slug(session, plan_slug) if plan_slug else None
    # Validate the manager against real candidates (admins/moderators), mirroring
    # users.set_manager — otherwise an invite could carry a bogus/plain-user id
    # that corrupts moderator scoping and digests once the account is activated.
    mid = int(manager_id) if manager_id.isdigit() else None
    if mid is not None and mid not in {c.id for c in users_svc.manager_candidates(session)}:
        mid = None
    days = int(trial_days) if (plan and plan.is_trial and trial_days.isdigit()) else None

    # Chosen libraries, else the global default.
    titles = libraries or runtime_config.plex_default_sections()

    try:
        plex_service.invite_friend(email, sections=titles or None)
    except Exception as exc:  # noqa: BLE001 - surface any Plex error to the admin
        return _render(
            request,
            viewer,
            session,
            error=_("Plex invite failed: %s") % exc,
            status_code=502,
        )

    invite = Invite(
        email=email.strip(),
        real_name=real_name.strip(),
        intended_role=intended_role,
        manager_id=mid,
        plan_id=plan.id if plan else None,
        trial_days=days,
        libraries=json.dumps(titles) if titles else None,
        token=secrets.token_urlsafe(16),
        status=InviteStatus.pending,
        plex_invite_sent_at=utcnow(),
        created_by=viewer.id,
    )
    session.add(invite)
    session.commit()
    audit.record(session, viewer.id, "create_invite", "invite", invite.id, {"email": email})
    # The Plex share exists now; the mail explaining what to do with it is a
    # separate best-effort step. A dead SMTP must not undo a good invite --
    # tell the admin instead, they can retry from the pending list.
    try:
        sent = notifications.notify_invite(session, invite)
    except Exception as exc:  # noqa: BLE001
        log.warning("invite email failed for %s", email, exc_info=True)
        return _render(
            request,
            viewer,
            session,
            error=_("Invited on Plex, but the email could not be sent: %s") % exc,
        )
    if not sent and runtime_config.smtp_config()["host"]:
        # No host at all means email is switched off for this install (the page
        # says so); a configured host that sends nothing is worth flagging.
        return _render(
            request,
            viewer,
            session,
            error=_(
                "Invited on Plex, but no email was sent — check the SMTP settings."
            ),
        )
    return RedirectResponse("/invites", status_code=303)


@router.post("/invites/{invite_id}/resend")
def resend_invite_email(
    request: Request,
    invite_id: int,
    viewer: AppUser = Depends(require_capability(Capability.invite_user)),
    session: Session = Depends(get_session),
):
    """Send the invite email again (bounced, deleted, or landed in spam). Does
    not touch the Plex share, which is already in place."""
    invite = session.get(Invite, invite_id)
    if invite is None or invite.status != InviteStatus.pending:
        return RedirectResponse("/invites", status_code=303)
    try:
        sent = notifications.notify_invite(session, invite, resend=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("invite email resend failed for %s", invite.email, exc_info=True)
        return _render(
            request, viewer, session,
            error=_("Email could not be sent: %s") % exc,
        )
    audit.record(
        session, viewer.id, "resend_invite", "invite", invite_id,
        {"email": invite.email, "sent": sent},
    )
    if not sent:
        return _render(
            request, viewer, session,
            error=_("No email was sent — check the SMTP settings."),
        )
    return _render(
        request, viewer, session,
        message=_("Invite email sent again to %s.") % invite.email,
    )


@router.post("/invites/{invite_id}/delete")
def delete_invite(
    request: Request,
    invite_id: int,
    viewer: AppUser = Depends(require_capability(Capability.invite_user)),
    session: Session = Depends(get_session),
):
    """Withdraw a still-pending invite (wrong email, or never accepted)."""
    invite = session.get(Invite, invite_id)
    if invite is None or invite.status != InviteStatus.pending:
        return RedirectResponse("/invites", status_code=303)
    email = invite.email
    plex_error = None
    try:
        plex_service.cancel_invite(email)
    except plex_service.PlexShareNotFound:
        # Nothing left on plex.tv (withdrawn there already, or never created):
        # dropping the local row is the whole job.
        pass
    except Exception as exc:  # noqa: BLE001 - don't strand the invite here
        # Withdraw locally anyway, but say so: the share may still exist on
        # plex.tv, and swallowing this is what let the two sides drift apart.
        log.warning("Plex withdraw failed for %s", email, exc_info=True)
        plex_error = str(exc)
    session.delete(invite)
    session.commit()
    detail = {"email": email}
    if plex_error:
        detail["plex_error"] = plex_error
    audit.record(session, viewer.id, "delete_invite", "invite", invite_id, detail)
    if plex_error:
        return _render(
            request,
            viewer,
            session,
            error=_("Invite withdrawn here, but Plex did not confirm it: %s")
            % plex_error,
        )
    return RedirectResponse("/invites", status_code=303)
