"""Add entry-context snapshot and running MFE/MAE columns to trade_journal

Revision ID: b005
Revises: b004
Create Date: 2026-08-21

External PDF review of momentum_v1, round 2 (sections 22-23): a practical
approximation of the review's proposed per-trade attribution schema. Rather
than fixed 5m/15m/30m snapshots (which would need a separate timed-sampling
job), underlying_mfe_pct/underlying_mae_pct/option_mfe_pct/option_mae_pct
are the best/worst excursion observed at any point between entry and exit,
updated every exit-check cycle by live_trading_engine.py. All nullable --
only single-leg entries (ema_crossover_v1/momentum_v1) populate these.

New columns (trade_journal):
  underlying_price_at_entry FLOAT, rvol_at_entry FLOAT, adx_at_entry FLOAT,
  dte_at_entry INT, delta_at_entry FLOAT,
  underlying_mfe_pct FLOAT, underlying_mae_pct FLOAT,
  option_mfe_pct FLOAT, option_mae_pct FLOAT
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


revision: str = 'b005'
down_revision: Union[str, Sequence[str], None] = 'b004'
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
        'underlying_price_at_entry', sa.Float(), nullable=True,
        comment='Underlying close price at entry'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'rvol_at_entry', sa.Float(), nullable=True,
        comment='RVOL at entry'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'adx_at_entry', sa.Float(), nullable=True,
        comment='ADX14 at entry'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'dte_at_entry', sa.Integer(), nullable=True,
        comment='Days to expiry at entry'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'delta_at_entry', sa.Float(), nullable=True,
        comment='Target option delta used for strike selection, if any (NULL = ATM)'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'underlying_mfe_pct', sa.Float(), nullable=True,
        comment='Best favorable underlying move since entry, % (running high-water mark)'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'underlying_mae_pct', sa.Float(), nullable=True,
        comment='Worst adverse underlying move since entry, % (running low-water mark)'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'option_mfe_pct', sa.Float(), nullable=True,
        comment='Best favorable option premium move since entry, %'
    ))
    _add_column_safe('trade_journal', sa.Column(
        'option_mae_pct', sa.Float(), nullable=True,
        comment='Worst adverse option premium move since entry, %'
    ))


def downgrade() -> None:
    op.drop_column('trade_journal', 'option_mae_pct')
    op.drop_column('trade_journal', 'option_mfe_pct')
    op.drop_column('trade_journal', 'underlying_mae_pct')
    op.drop_column('trade_journal', 'underlying_mfe_pct')
    op.drop_column('trade_journal', 'delta_at_entry')
    op.drop_column('trade_journal', 'dte_at_entry')
    op.drop_column('trade_journal', 'adx_at_entry')
    op.drop_column('trade_journal', 'rvol_at_entry')
    op.drop_column('trade_journal', 'underlying_price_at_entry')
