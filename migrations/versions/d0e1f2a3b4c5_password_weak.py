"""password weak flag

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-09-24 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('app_user', sa.Column(
        'password_weak', sa.Boolean(), nullable=False,
        server_default=sa.text('0')))


def downgrade() -> None:
    op.drop_column('app_user', 'password_weak')
