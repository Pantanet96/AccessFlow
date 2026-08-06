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
        # First renewal (pending) only for paid, non-trial, non-unlimited plans.
        if plan.is_paid and not plan.is_trial and not plan.is_unlimited:
            sub_svc.create_renewal(
                session, sub, actor_id=None, collected_by=user.manager_id
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
