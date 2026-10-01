"""Add regime and market_direction columns to rejected_signal_outcome

Revision ID: b010
Revises: b009
Create Date: 2026-10-01

"RANGE exception experiment" (external review): regime alone can't
distinguish "RANGE_BOUND while NIFTY is bearish" from "RANGE_BOUND while
NIFTY is bullish" -- the review's proposed 4-way condition table
(RANGE+bullish+EMA BUY, RANGE+bearish+EMA SELL, RANGE+bullish+Momentum BUY,
RANGE+bearish+Momentum SELL) needs both dimensions to be queryable.

New columns (rejected_signal_outcome):
  regime           VARCHAR(20), nullable
  market_direction VARCHAR(10), nullable
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


revision: str = 'b010'
down_revision: Union[str, Sequence[str], None] = 'b009'
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
    _add_column_safe('rejected_signal_outcome', sa.Column('regime', sa.String(length=20), nullable=True))
    _add_column_safe('rejected_signal_outcome', sa.Column('market_direction', sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column('rejected_signal_outcome', 'market_direction')
    op.drop_column('rejected_signal_outcome', 'regime')
