"""user access fields

Revision ID: 55573c849012
Revises: 607493c01d77
Create Date: 2026-06-19 11:06:25.763078
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '55573c849012'
down_revision: Union[str, None] = '607493c01d77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMN (no batch): SQLite supports it incl. NOT NULL + default,
    # and avoids recreating app_user (which has inbound FKs + FK enforcement on).
    op.add_column('app_user', sa.Column('shared_libraries', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('app_user', sa.Column('access_suspended', sa.Boolean(), nullable=False, server_default=sa.text('0')))
    op.add_column('app_user', sa.Column('grace_days', sa.Integer(), nullable=False, server_default=sa.text('0')))
    op.add_column('app_user', sa.Column('overseerr_prev_permissions', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('app_user', 'overseerr_prev_permissions')
    op.drop_column('app_user', 'grace_days')
    op.drop_column('app_user', 'access_suspended')
    op.drop_column('app_user', 'shared_libraries')
