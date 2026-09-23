"""Provision a subscription when an invited user is activated via Plex login."""
from sqlmodel import Session

from app.models import AppUser, Invite, Plan
from app.services import subscriptions as sub_svc


def on_user_activated(session: Session, user: AppUser, invite: Invite) -> None:
    # Carry the invite's chosen libraries onto the user (null = global default).
    if invite.libraries:
        user.shared_libraries = invite.libraries
        session.add(user)
        session.commit()
    plan = session.get(Plan, invite.plan_id) if invite.plan_id else None
    if plan is not None:
        sub = sub_svc.create_subscription(
            session, user, plan, trial_days=invite.trial_days
        )
        # A paid invite is collected before it is sent: log that first period as
        # a paid renewal (revenue in reports), like a manual first setup. No
        # pending renewal: it asked to collect again what was already paid and,
        # once confirmed, granted a second period. No-op for trial/F&F.
        sub_svc.record_setup_payment(
            session, sub, plan, actor_id=invite.created_by,
            collected_by=user.manager_id,
        )
        # One-time welcome / onboarding notification (idempotent via notification_log).
        # Wrapped: a notification failure must never abort first-login activation.
        try:
            from app.services.notifications import notify_welcome

            notify_welcome(session, user, plan, sub)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("pum.activation").warning(
                "welcome notification failed", exc_info=True
            )
    # Grant Overseerr access reflecting the plan (trial = view-only). Runs even
    # without a plan so any newly activated user can sign in to Overseerr.
    from app.services import access_service

    access_service.grant_overseerr(session, user)
