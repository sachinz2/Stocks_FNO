"""Add slippage columns to orders and analytics columns to trade_journal

Revision ID: b003
Revises: b002
Create Date: 2026-06-19

New columns:
  orders          : fill_price DECIMAL(18,4), slippage DECIMAL(18,4)
  trade_journal   : day_of_week INT, hour_of_day INT,
                    atr_at_exit FLOAT, vix_at_exit FLOAT,
                    regime_label VARCHAR(30),
                    total_slippage_pts FLOAT, slippage FLOAT
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b003'
down_revision: Union[str, Sequence[str], None] = 'b002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── orders table ──────────────────────────────────────────────────────────
    op.add_column('orders', sa.Column(
        'fill_price', sa.Numeric(18, 4), nullable=True,
        comment='Actual fill price after bid-ask slippage (PaperBroker / Zerodha avg)'
    ))
    op.add_column('orders', sa.Column(
        'slippage', sa.Numeric(18, 4), nullable=True,
        comment='fill_price - price per unit (negative for SELL orders)'
    ))

    # ── trade_journal table ───────────────────────────────────────────────────
    # Entry timing
    op.add_column('trade_journal', sa.Column(
        'day_of_week', sa.Integer(), nullable=True,
        comment='0=Monday … 4=Friday (IST)'
    ))
    op.add_column('trade_journal', sa.Column(
        'hour_of_day', sa.Integer(), nullable=True,
        comment='Hour of entry in IST (9–15)'
    ))

    # Exit context
    op.add_column('trade_journal', sa.Column(
        'atr_at_exit', sa.Float(), nullable=True,
        comment='ATR14 value at trade close time'
    ))
    op.add_column('trade_journal', sa.Column(
        'vix_at_exit', sa.Float(), nullable=True,
        comment='India VIX at trade close time'
    ))
    op.add_column('trade_journal', sa.Column(
        'regime_label', sa.String(30), nullable=True,
        comment='TRENDING / RANGE_BOUND / VOLATILE — derived from ATR%'
    ))
    op.add_column('trade_journal', sa.Column(
        'total_slippage_pts', sa.Float(), nullable=True,
        comment='Sum of |slippage| across all legs × lot size for the structure'
    ))
    op.add_column('trade_journal', sa.Column(
        'slippage', sa.Float(), nullable=True,
        comment='Legacy alias for total_slippage_pts (kept for backwards compat)'
    ))


def downgrade() -> None:
    # trade_journal
    op.drop_column('trade_journal', 'slippage')
    op.drop_column('trade_journal', 'total_slippage_pts')
    op.drop_column('trade_journal', 'regime_label')
    op.drop_column('trade_journal', 'vix_at_exit')
    op.drop_column('trade_journal', 'atr_at_exit')
    op.drop_column('trade_journal', 'hour_of_day')
    op.drop_column('trade_journal', 'day_of_week')

    # orders
    op.drop_column('orders', 'slippage')
    op.drop_column('orders', 'fill_price')
