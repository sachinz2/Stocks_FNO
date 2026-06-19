"""Add walk_forward_results table for strategy robustness testing

Revision ID: b004
Revises: b003
Create Date: 2026-06-19

Stores IS and OOS metrics from walk-forward analysis windows.
Used by /analytics/walk-forward and /analytics/walk-forward-results endpoints.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b004'
down_revision: Union[str, Sequence[str], None] = 'b003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'walk_forward_results',
        sa.Column('id',            sa.BigInteger(),  primary_key=True, autoincrement=True),
        sa.Column('strategy_name', sa.String(50),    nullable=False),
        sa.Column('symbol',        sa.String(30),    nullable=True),
        sa.Column('window_start',  sa.String(20),    nullable=False),
        sa.Column('train_end',     sa.String(20),    nullable=False),
        sa.Column('window_end',    sa.String(20),    nullable=False),
        sa.Column('is_oos',        sa.Integer(),     nullable=False, default=0),
        sa.Column('profit_factor', sa.Float(),       nullable=True),
        sa.Column('sharpe_ratio',  sa.Float(),       nullable=True),
        sa.Column('max_drawdown',  sa.Float(),       nullable=True),
        sa.Column('win_rate',      sa.Float(),       nullable=True),
        sa.Column('total_pnl',     sa.Float(),       nullable=True),
        sa.Column('trade_count',   sa.Integer(),     nullable=True),
        sa.Column('avg_pnl',       sa.Float(),       nullable=True),
        sa.Column('expectancy',    sa.Float(),       nullable=True),
        sa.Column('parameters',    sa.JSON(),        nullable=True),
        sa.Column('run_at',        sa.TIMESTAMP(),   nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('idx_wf_strategy', 'walk_forward_results', ['strategy_name'])
    op.create_index('idx_wf_window',   'walk_forward_results', ['window_start', 'window_end'])


def downgrade() -> None:
    op.drop_index('idx_wf_window',   table_name='walk_forward_results')
    op.drop_index('idx_wf_strategy', table_name='walk_forward_results')
    op.drop_table('walk_forward_results')
