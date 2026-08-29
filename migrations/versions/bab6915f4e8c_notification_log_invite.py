"""notification_log: nullable user_id + invite_id (invite emails)

Revision ID: bab6915f4e8c
Revises: b7c8d9e0f1a2
Create Date: 2026-08-29 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bab6915f4e8c'
down_revision: Union[str, None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # An invited address has no app_user yet -- the AppUser is created on first
    # Plex sign-in -- so the invite email can only be logged with user_id NULL
    # and the invite it belongs to instead.
    #
    # invite_id is deliberately NOT a foreign key: withdrawing an invite deletes
    # the invite row, and with PRAGMA foreign_keys=ON (app/db.py) a real FK would
    # either block the withdrawal or take the send history down with it. The log
    # has to outlive the invite -- that's the whole point of keeping it.
    with op.batch_alter_table('notification_log', schema=None) as batch_op:
        batch_op.alter_column(
            'user_id', existing_type=sa.Integer(), nullable=True
        )
        batch_op.add_column(sa.Column('invite_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            'ix_notification_log_invite_id', ['invite_id'], unique=False
        )


def downgrade() -> None:
    # Invite-email rows have no user to point at; drop them before restoring
    # the NOT NULL constraint.
    op.execute('DELETE FROM notification_log WHERE user_id IS NULL')
    with op.batch_alter_table('notification_log', schema=None) as batch_op:
        batch_op.drop_index('ix_notification_log_invite_id')
        batch_op.drop_column('invite_id')
        batch_op.alter_column(
            'user_id', existing_type=sa.Integer(), nullable=False
        )
