"""plan libraries

Revision ID: 8f8803b913c6
Revises: cd077ecd8409
Create Date: 2026-06-19 13:13:01.823877
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '8f8803b913c6'
down_revision: Union[str, None] = 'cd077ecd8409'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('plan', sa.Column('libraries', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column('plan', 'libraries')
