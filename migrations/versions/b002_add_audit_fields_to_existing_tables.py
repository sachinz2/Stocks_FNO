"""Add soft delete and audit fields to existing tables

Revision ID: b002
Revises: b001
Create Date: 2026-06-11 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b002'
down_revision: Union[str, Sequence[str], None] = 'b001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Add soft delete and audit fields to existing tables."""
    
    # Add deleted_at to stocks
    op.add_column('stocks', sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True))
    
    # Add deleted_at to instruments
    op.add_column('instruments', sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('instruments', sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('instruments', sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True))
    
    # Add soft delete and audit to ohlc_data
    op.add_column('ohlc_data', sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('ohlc_data', sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('ohlc_data', sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True))
    
    # Add soft delete and audit to indicators
    op.add_column('indicators', sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('indicators', sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('indicators', sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True))
    
    # Add soft delete and audit to signals
    op.add_column('signals', sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('signals', sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True))
    
    # Add soft delete and audit to orders
    op.add_column('orders', sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('orders', sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True))
    
    # Add soft delete and audit to positions
    op.add_column('positions', sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('positions', sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True))
    
    # Add soft delete and audit to trades
    op.add_column('trades', sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('trades', sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False))
    op.add_column('trades', sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True))
    
    # Add indexes for soft deletes and audit fields on existing tables
    op.create_index('idx_stocks_deleted', 'stocks', ['deleted_at'], unique=False)
    op.create_index('idx_instruments_deleted', 'instruments', ['deleted_at'], unique=False)
    op.create_index('idx_ohlc_deleted', 'ohlc_data', ['deleted_at'], unique=False)
    op.create_index('idx_indicators_deleted', 'indicators', ['deleted_at'], unique=False)
    op.create_index('idx_signals_deleted', 'signals', ['deleted_at'], unique=False)
    op.create_index('idx_orders_deleted', 'orders', ['deleted_at'], unique=False)
    op.create_index('idx_positions_deleted', 'positions', ['deleted_at'], unique=False)
    op.create_index('idx_trades_deleted', 'trades', ['deleted_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema - Remove soft delete and audit fields from existing tables."""
    
    # Remove indexes
    op.drop_index('idx_trades_deleted', table_name='trades')
    op.drop_index('idx_positions_deleted', table_name='positions')
    op.drop_index('idx_orders_deleted', table_name='orders')
    op.drop_index('idx_signals_deleted', table_name='signals')
    op.drop_index('idx_indicators_deleted', table_name='indicators')
    op.drop_index('idx_ohlc_deleted', table_name='ohlc_data')
    op.drop_index('idx_instruments_deleted', table_name='instruments')
    op.drop_index('idx_stocks_deleted', table_name='stocks')
    
    # Remove columns
    op.drop_column('trades', 'deleted_at')
    op.drop_column('trades', 'updated_at')
    op.drop_column('trades', 'created_at')
    
    op.drop_column('positions', 'deleted_at')
    op.drop_column('positions', 'created_at')
    
    op.drop_column('orders', 'deleted_at')
    op.drop_column('orders', 'updated_at')
    
    op.drop_column('signals', 'deleted_at')
    op.drop_column('signals', 'updated_at')
    
    op.drop_column('indicators', 'deleted_at')
    op.drop_column('indicators', 'updated_at')
    op.drop_column('indicators', 'created_at')
    
    op.drop_column('ohlc_data', 'deleted_at')
    op.drop_column('ohlc_data', 'updated_at')
    op.drop_column('ohlc_data', 'created_at')
    
    op.drop_column('instruments', 'deleted_at')
    op.drop_column('instruments', 'updated_at')
    op.drop_column('instruments', 'created_at')
    
    op.drop_column('stocks', 'deleted_at')
