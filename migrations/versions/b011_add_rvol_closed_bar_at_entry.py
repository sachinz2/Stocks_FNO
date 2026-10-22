"""Add rvol_closed_bar_at_entry to trade_journal

Revision ID: b011
Revises: b010
Create Date: 2026-10-22

rvol_at_entry (b005) is market_data["rvol"] -- the still-forming CURRENT
bar's volume-so-far. momentum_v1's own pullback+breakout entry logic
(rvol_checked_internally=True) doesn't gate on that field at all -- it gates
on market_data["rvol_closed_bar"] (ltp_poller.py), the last FINALIZED bar's
volume, a different number. Found live: a 54-trade RVOL-bucket analysis
built on rvol_at_entry looked actionable for tightening momentum_v1's RVOL
gate, but rvol_at_entry turned out not to be the variable that actually
gated the entry -- this column exists so a future analysis has the right
number to work with. NULL for ema_crossover_v1 (its entries are gated by
the engine's shared RVOL check, which DOES use plain "rvol" -- no mismatch
there, so no need to populate this for it).

New columns (trade_journal):
  rvol_closed_bar_at_entry FLOAT
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


revision: str = 'b011'
down_revision: Union[str, Sequence[str], None] = 'b010'
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
    _add_column_safe('trade_journal', sa.Column(
        'rvol_closed_bar_at_entry', sa.Float(), nullable=True,
        comment='Last finalized bar\'s RVOL at entry -- the variable momentum_v1 actually gates on, distinct from rvol_at_entry'
    ))


def downgrade() -> None:
    op.drop_column('trade_journal', 'rvol_closed_bar_at_entry')
