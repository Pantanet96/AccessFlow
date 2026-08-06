"""session generation counter (server-side session invalidation)

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-02 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Incremented on logout / password change; the signed session cookie carries
    # the value it was issued with, so a mismatch invalidates stale/stolen cookies.
    op.add_column('app_user', sa.Column(
        'session_gen', sa.Integer(), nullable=False, server_default=sa.text('0')))


def downgrade() -> None:
    op.drop_column('app_user', 'session_gen')
