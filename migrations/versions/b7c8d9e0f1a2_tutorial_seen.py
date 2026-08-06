"""onboarding tutorial seen flag

Revision ID: b7c8d9e0f1a2
Revises: d3bfc29a6099
Create Date: 2026-08-03 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'd3bfc29a6099'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('app_user', sa.Column(
        'tutorial_seen', sa.Boolean(), nullable=False,
        server_default=sa.text('0')))


def downgrade() -> None:
    op.drop_column('app_user', 'tutorial_seen')
