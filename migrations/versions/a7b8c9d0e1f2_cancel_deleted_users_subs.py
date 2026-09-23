"""cancel subscriptions of already-deleted users

Revision ID: a7b8c9d0e1f2
Revises: bab6915f4e8c
Create Date: 2026-09-23 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'bab6915f4e8c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # soft_delete used to leave subscriptions live: deleted users kept getting
    # reminders, counted in reports and sat in /requests. It now cancels them;
    # do the same for users deleted before this fix (deletion is final).
    deleted_subs = (
        "SELECT s.id FROM subscription s JOIN app_user u ON u.id = s.user_id "
        "WHERE u.is_active = 0"
    )
    op.execute(
        f"DELETE FROM renewal WHERE status = 'pending' "
        f"AND subscription_id IN ({deleted_subs})"
    )
    op.execute(
        f"UPDATE subscription SET status = 'cancelled' "
        f"WHERE status != 'cancelled' AND id IN ({deleted_subs})"
    )


def downgrade() -> None:
    pass  # data fix: the previous state was the bug
