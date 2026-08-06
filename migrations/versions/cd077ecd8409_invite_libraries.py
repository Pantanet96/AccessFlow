"""invite libraries

Revision ID: cd077ecd8409
Revises: 55573c849012
Create Date: 2026-06-19 11:17:18.179076
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = 'cd077ecd8409'
down_revision: Union[str, None] = '55573c849012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('invite', sa.Column('libraries', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column('invite', 'libraries')
