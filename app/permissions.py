"""Role -> capability matrix. Single source of truth for authorization."""
import enum

from app.models import Role


class Capability(str, enum.Enum):
    invite_user = "invite_user"
    change_plan_any = "change_plan_any"       # any plan, incl. free/F&F
    change_plan_paid = "change_plan_paid"     # paid plans only
    renew_subscription = "renew_subscription"
    delete_user = "delete_user"
    manage_roles = "manage_roles"             # create/edit admins & moderators
    view_reports = "view_reports"
    view_own = "view_own"
    edit_own = "edit_own"


_BASE = {Capability.view_own, Capability.edit_own}

ROLE_CAPS: dict[Role, set[Capability]] = {
    Role.user: set(_BASE),
    Role.moderator: _BASE | {
        Capability.change_plan_paid,
        Capability.renew_subscription,
    },
    Role.admin: _BASE | {
        Capability.change_plan_paid,
        Capability.change_plan_any,
        Capability.renew_subscription,
        Capability.invite_user,
        Capability.delete_user,
        Capability.view_reports,
    },
    Role.superadmin: set(Capability),  # everything
}


def capabilities_for(role: Role) -> set[Capability]:
    return ROLE_CAPS.get(role, set())


def has_capability(user, cap: Capability) -> bool:
    return cap in capabilities_for(user.role)


# Hierarchy: superadmin > admin > moderator > user. A higher rank may act on
# (manage / change plan / disable / delete) strictly-lower ranks only — never a
# peer, never a superior. See users.can_manage_user for the scoped check.
_RANK: dict[Role, int] = {
    Role.user: 0,
    Role.moderator: 1,
    Role.admin: 2,
    Role.superadmin: 3,
}


def rank(role: Role) -> int:
    return _RANK.get(role, 0)


def outranks(actor: Role, target: Role) -> bool:
    return rank(actor) > rank(target)
