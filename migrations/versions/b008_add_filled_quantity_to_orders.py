"""Add filled_quantity column to orders

Revision ID: b008
Revises: b007
Create Date: 2026-08-21

Deep review (2026-08-21, Orders/broker #2): the orders table had no way to
distinguish requested quantity from actually-filled quantity. Kite Connect
can leave a resting LIMIT order at status OPEN while partially filled --
without this column, expire_stale_orders()'s retry path resubmitted the
FULL original quantity, risking a silent over-buy of the already-filled
portion on every retry of a partially-filled order.

New column (orders): filled_quantity INTEGER, nullable
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


revision: str = 'b008'
down_revision: Union[str, Sequence[str], None] = 'b007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_safe(table: str, column: sa.Column) -> None:
    """Add column, silently skip if it already exists (MySQL errno 1060)."""
    try:
        op.add_column(table, column)
    except OperationalError as e:
        if "1060" in str(e) or "Duplicate column" in str(e):
            pass  # column already present — nothing to do
        else:
            raise


def upgrade() -> None:
    _add_column_safe('orders', sa.Column('filled_quantity', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'filled_quantity')
