"""broadcast banner (persisted message + per-user dismissal)

Revision ID: d3bfc29a6099
Revises: f4a5b6c7d8e9
Create Date: 2026-08-03 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = 'd3bfc29a6099'
down_revision: Union[str, None] = 'f4a5b6c7d8e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Persisted broadcast text so /broadcast can also show an in-app banner,
    # independent of the Telegram/email push (which is fire-and-forget).
    op.create_table('broadcast',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('only_role', sa.Enum('superadmin', 'admin', 'moderator', 'user', name='role'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    # Per-user "last banner seen" marker: broadcast.id > this -> still unread.
    op.add_column('app_user', sa.Column(
        'dismissed_broadcast_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('app_user', 'dismissed_broadcast_id')
    op.drop_table('broadcast')
