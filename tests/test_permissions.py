from app.models import Role
from app.permissions import Capability, capabilities_for, has_capability


class _U:
    def __init__(self, role):
        self.role = role


def test_superadmin_has_everything():
    assert capabilities_for(Role.superadmin) == set(Capability)


def test_admin_caps():
    caps = capabilities_for(Role.admin)
    assert Capability.invite_user in caps
    assert Capability.delete_user in caps
    assert Capability.view_reports in caps
    assert Capability.change_plan_any in caps
    assert Capability.manage_roles not in caps


def test_moderator_caps():
    caps = capabilities_for(Role.moderator)
    assert Capability.change_plan_paid in caps
    assert Capability.renew_subscription in caps
    assert Capability.change_plan_any not in caps
    assert Capability.invite_user not in caps
    assert Capability.delete_user not in caps


def test_user_caps():
    caps = capabilities_for(Role.user)
    assert caps == {Capability.view_own, Capability.edit_own}


def test_has_capability_helper():
    assert has_capability(_U(Role.admin), Capability.invite_user)
    assert not has_capability(_U(Role.user), Capability.invite_user)
