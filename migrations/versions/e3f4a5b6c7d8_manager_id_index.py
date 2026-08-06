"""index on app_user.manager_id

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-08 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # list_users_for/assigned_active_count filter app_user by manager_id on
    # every users-list render and every manager reassignment.
    op.create_index('ix_app_user_manager_id', 'app_user', ['manager_id'])


def downgrade() -> None:
    op.drop_index('ix_app_user_manager_id', table_name='app_user')
