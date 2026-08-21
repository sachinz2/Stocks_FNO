"""Add entry-time Greeks/IV/wing-width columns to trade_journal

Revision ID: b007
Revises: b006
Create Date: 2026-08-21

Second-opinion review of credit_spread_v1/iron_condor_v1 (2026-08-21, P1 #8):
"store actual entry deltas/IV/wing widths" for later strategy analysis.
credit_spread_v1/iron_condor_v1 only, computed from the final, actually-
resolved strikes/fills. Not gates -- pure data collection.

New columns (trade_journal):
  put_short_delta FLOAT, call_short_delta FLOAT,
  put_long_delta FLOAT, call_long_delta FLOAT,
  put_iv FLOAT, call_iv FLOAT,
  put_wing_width FLOAT, call_wing_width FLOAT
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


revision: str = 'b007'
down_revision: Union[str, Sequence[str], None] = 'b006'
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
    for col in (
        'put_short_delta', 'call_short_delta', 'put_long_delta', 'call_long_delta',
        'put_iv', 'call_iv', 'put_wing_width', 'call_wing_width',
    ):
        _add_column_safe('trade_journal', sa.Column(col, sa.Float(), nullable=True))


def downgrade() -> None:
    for col in (
        'call_wing_width', 'put_wing_width', 'call_iv', 'put_iv',
        'call_long_delta', 'put_long_delta', 'call_short_delta', 'put_short_delta',
    ):
        op.drop_column('trade_journal', col)
