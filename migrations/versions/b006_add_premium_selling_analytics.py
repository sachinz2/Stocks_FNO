"""Add daily-ATR, credit/max-loss, and wing-failure columns to trade_journal

Revision ID: b006
Revises: b005
Create Date: 2026-08-21

External review of credit_spread_v1/iron_condor_v1 (2026-08-21): data
collection for premium-selling strategies, per the review's own "first
collect the data, don't gate on it yet" recommendation. None of these are
entry/exit gates.

New columns (trade_journal):
  daily_atr_pct FLOAT, credit_to_max_loss_pct FLOAT, wing_failed VARCHAR(10)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


revision: str = 'b006'
down_revision: Union[str, Sequence[str], None] = 'b005'
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
        'daily_atr_pct', sa.Float(), nullable=True,
        comment='Daily ATR14 %, vs the existing 5m-derived regime_atr_pct'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'credit_to_max_loss_pct', sa.Float(), nullable=True,
        comment='net_credit / max_loss * 100, at entry'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'wing_failed', sa.String(10), nullable=True,
        comment='iron_condor_v1 only: PUT / CALL / BOTH / NULL'
    ))


def downgrade() -> None:
    op.drop_column('trade_journal', 'wing_failed')
    op.drop_column('trade_journal', 'credit_to_max_loss_pct')
    op.drop_column('trade_journal', 'daily_atr_pct')
