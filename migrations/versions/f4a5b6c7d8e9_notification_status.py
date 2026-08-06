"""notification_log status + error (failure logging)

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-08-01 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4a5b6c7d8e9'
down_revision: Union[str, None] = 'e3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Track send outcome so the history view can surface failures. Existing rows
    # are all successful sends -> default 'sent'. Failed rows carry a reason.
    op.add_column('notification_log', sa.Column(
        'status', sa.String(), nullable=False, server_default='sent'))
    op.add_column('notification_log', sa.Column(
        'error', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('notification_log', 'error')
    op.drop_column('notification_log', 'status')
