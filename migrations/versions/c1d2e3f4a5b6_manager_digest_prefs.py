"""manager digest prefs (weekly collect digest)

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
Create Date: 2026-06-22 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-manager weekly "collect" digest: receive it or not, and on which weekday
    # (0=Mon..6=Sun). Channels reuse the existing notify_via_email/telegram prefs.
    op.add_column('app_user', sa.Column(
        'digest_enabled', sa.Boolean(), nullable=False, server_default=sa.text('1')))
    op.add_column('app_user', sa.Column(
        'digest_weekday', sa.Integer(), nullable=False, server_default=sa.text('0')))


def downgrade() -> None:
    op.drop_column('app_user', 'digest_weekday')
    op.drop_column('app_user', 'digest_enabled')
