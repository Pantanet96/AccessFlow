"""drop pending renewals left behind by a plan change

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-23 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # change_plan used to keep the open renewal of the old plan; paying it
    # billed the old price or put an expiry on an unlimited sub. It now drops
    # it; do the same for renewals already orphaned that way.
    op.execute(
        "DELETE FROM renewal WHERE status = 'pending' AND plan_id != "
        "(SELECT s.plan_id FROM subscription s WHERE s.id = renewal.subscription_id)"
    )


def downgrade() -> None:
    pass  # data fix: the previous state was the bug
