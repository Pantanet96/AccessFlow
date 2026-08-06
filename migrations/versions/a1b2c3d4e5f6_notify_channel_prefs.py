"""notify channel prefs

Revision ID: a1b2c3d4e5f6
Revises: 8f8803b913c6
Create Date: 2026-06-19 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '8f8803b913c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('app_user', sa.Column(
        'notify_via_email', sa.Boolean(), nullable=False,
        server_default=sa.text('1')))
    op.add_column('app_user', sa.Column(
        'notify_via_telegram', sa.Boolean(), nullable=False,
        server_default=sa.text('1')))


def downgrade() -> None:
    op.drop_column('app_user', 'notify_via_telegram')
    op.drop_column('app_user', 'notify_via_email')
