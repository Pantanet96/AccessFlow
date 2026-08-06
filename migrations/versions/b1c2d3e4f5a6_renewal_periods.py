"""renewal periods (multi-period renewal)

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-22 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('renewal', sa.Column(
        'periods', sa.Integer(), nullable=False, server_default=sa.text('1')))


def downgrade() -> None:
    op.drop_column('renewal', 'periods')
