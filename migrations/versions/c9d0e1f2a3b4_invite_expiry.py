"""invite expiry

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-24 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('invite', sa.Column('expires_at', sa.DateTime(), nullable=True))
    # Invites already out get the standard 30 days from when they were sent;
    # older ones expire on the next daily scan.
    op.execute(
        "UPDATE invite SET expires_at = datetime(created_at, '+30 days') "
        "WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.execute("UPDATE invite SET status = 'pending' WHERE status = 'expired'")
    with op.batch_alter_table('invite', schema=None) as batch_op:
        batch_op.drop_column('expires_at')
