"""Add backtest, risk, ML, and audit tables

Revision ID: b001
Revises: aeedfa3b6aaa
Create Date: 2026-06-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b001'
down_revision: Union[str, Sequence[str], None] = 'aeedfa3b6aaa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Add backtest, risk, ML, and audit tables."""
    
    # ==================== MARKET TICKS TABLE ====================
    # For 30-day retention only
    op.create_table('ticks',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(length=30), nullable=False),
        sa.Column('tick_timestamp', sa.TIMESTAMP(), nullable=False),
        sa.Column('last_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('bid_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('ask_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('bid_quantity', sa.BigInteger(), nullable=True),
        sa.Column('ask_quantity', sa.BigInteger(), nullable=True),
        sa.Column('volume', sa.BigInteger(), nullable=True),
        sa.Column('oi', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ticks'))
    )
    op.create_index('idx_ticks_symbol_time', 'ticks', ['symbol', 'tick_timestamp'], unique=False)
    op.create_index('idx_ticks_timestamp', 'ticks', ['tick_timestamp'], unique=False)
    
    # ==================== BACKTEST TABLES ====================
    
    # Backtest Runs - Master record of each backtest execution
    op.create_table('backtest_runs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('strategy_name', sa.String(length=100), nullable=False),
        sa.Column('start_date', sa.DATE(), nullable=False),
        sa.Column('end_date', sa.DATE(), nullable=False),
        sa.Column('initial_capital', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('final_capital', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('total_return', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('sharpe_ratio', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('sortino_ratio', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('max_drawdown', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('win_rate', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('profit_factor', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('total_trades', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
        sa.Column('parameters', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_runs'))
    )
    op.create_index('idx_backtest_runs_strategy', 'backtest_runs', ['strategy_name'], unique=False)
    op.create_index('idx_backtest_runs_dates', 'backtest_runs', ['start_date', 'end_date'], unique=False)
    
    # Backtest Results - Individual trade results
    op.create_table('backtest_results',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('backtest_run_id', sa.BigInteger(), nullable=False),
        sa.Column('symbol', sa.String(length=30), nullable=False),
        sa.Column('entry_time', sa.TIMESTAMP(), nullable=False),
        sa.Column('exit_time', sa.TIMESTAMP(), nullable=True),
        sa.Column('entry_price', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('exit_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('side', sa.String(length=10), nullable=False),
        sa.Column('pnl', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('pnl_percent', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('commission', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('slippage', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.ForeignKeyConstraint(['backtest_run_id'], ['backtest_runs.id'], name=op.f('fk_backtest_results_run'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_results'))
    )
    op.create_index('idx_backtest_results_run', 'backtest_results', ['backtest_run_id'], unique=False)
    op.create_index('idx_backtest_results_symbol', 'backtest_results', ['symbol'], unique=False)
    
    # Backtest Trades - Trade-by-trade execution
    op.create_table('backtest_trades',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('backtest_run_id', sa.BigInteger(), nullable=False),
        sa.Column('trade_number', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(length=30), nullable=False),
        sa.Column('entry_date', sa.DATE(), nullable=False),
        sa.Column('exit_date', sa.DATE(), nullable=True),
        sa.Column('entry_price', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('exit_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('pnl', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('return_pct', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('holding_days', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['backtest_run_id'], ['backtest_runs.id'], name=op.f('fk_backtest_trades_run'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_trades'))
    )
    op.create_index('idx_backtest_trades_run', 'backtest_trades', ['backtest_run_id'], unique=False)
    op.create_index('idx_backtest_trades_symbol', 'backtest_trades', ['symbol'], unique=False)
    
    # Backtest Equity Curve - Daily equity progression
    op.create_table('backtest_equity_curve',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('backtest_run_id', sa.BigInteger(), nullable=False),
        sa.Column('date', sa.DATE(), nullable=False),
        sa.Column('equity', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('daily_return', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('cumulative_return', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.ForeignKeyConstraint(['backtest_run_id'], ['backtest_runs.id'], name=op.f('fk_backtest_equity_run'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_equity_curve'))
    )
    op.create_index('idx_backtest_equity_run_date', 'backtest_equity_curve', ['backtest_run_id', 'date'], unique=False)
    
    # Backtest Drawdowns - Drawdown analysis
    op.create_table('backtest_drawdowns',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('backtest_run_id', sa.BigInteger(), nullable=False),
        sa.Column('peak_date', sa.DATE(), nullable=False),
        sa.Column('trough_date', sa.DATE(), nullable=False),
        sa.Column('recovery_date', sa.DATE(), nullable=True),
        sa.Column('peak_equity', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('trough_equity', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('drawdown_amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('drawdown_percent', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('recovery_days', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['backtest_run_id'], ['backtest_runs.id'], name=op.f('fk_backtest_drawdown_run'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_drawdowns'))
    )
    op.create_index('idx_backtest_drawdown_run', 'backtest_drawdowns', ['backtest_run_id'], unique=False)
    
    # ==================== RISK TABLES ====================
    
    # Risk Rules - Risk management rules configuration
    op.create_table('risk_rules',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('rule_name', sa.String(length=100), nullable=False, unique=True),
        sa.Column('rule_type', sa.String(length=50), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('max_daily_loss_amount', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('max_daily_loss_percent', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('max_position_size', sa.Integer(), nullable=True),
        sa.Column('max_open_positions', sa.Integer(), nullable=True),
        sa.Column('max_exposure_percent', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('max_leverage', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('circuit_breaker_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_rules'))
    )
    op.create_index('idx_risk_rules_active', 'risk_rules', ['is_active'], unique=False)
    
    # Risk Events - Risk event logging
    op.create_table('risk_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('severity', sa.String(length=20), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('current_value', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('threshold_value', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('affected_symbol', sa.String(length=30), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_events'))
    )
    op.create_index('idx_risk_events_severity', 'risk_events', ['severity'], unique=False)
    op.create_index('idx_risk_events_timestamp', 'risk_events', ['created_at'], unique=False)
    
    # Risk Violations - Rule violations and breaches
    op.create_table('risk_violations',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('risk_rule_id', sa.BigInteger(), nullable=False),
        sa.Column('violation_type', sa.String(length=50), nullable=False),
        sa.Column('severity', sa.String(length=20), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('current_value', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('limit_value', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('action_taken', sa.String(length=100), nullable=True),
        sa.Column('order_blocked', sa.Boolean(), server_default=sa.text('false'), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('resolved_at', sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(['risk_rule_id'], ['risk_rules.id'], name=op.f('fk_violations_rule'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_violations'))
    )
    op.create_index('idx_risk_violations_rule', 'risk_violations', ['risk_rule_id'], unique=False)
    op.create_index('idx_risk_violations_timestamp', 'risk_violations', ['created_at'], unique=False)
    
    # Daily Risk Metrics - Daily risk summary
    op.create_table('daily_risk_metrics',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('date', sa.DATE(), nullable=False, unique=True),
        sa.Column('daily_pnl', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('daily_loss_percent', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('max_open_positions', sa.Integer(), nullable=True),
        sa.Column('total_exposure_percent', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('max_single_loss', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('violations_count', sa.Integer(), nullable=True),
        sa.Column('circuit_breaker_triggered', sa.Boolean(), server_default=sa.text('false'), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_daily_risk_metrics'))
    )
    op.create_index('idx_daily_risk_metrics_date', 'daily_risk_metrics', ['date'], unique=False)
    
    # ==================== ML TABLES ====================
    
    # Feature Store - ML feature versioning and storage
    op.create_table('feature_store',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(length=30), nullable=False),
        sa.Column('timestamp', sa.TIMESTAMP(), nullable=False),
        sa.Column('version', sa.String(length=20), nullable=False, server_default='1.0'),
        sa.Column('close_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('returns_1d', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('returns_5d', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('returns_20d', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('volume_delta', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('atr_14', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('rsi_14', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('ema_20_dist', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('ema_50_dist', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('vwap_dist', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('macd', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('open_interest_change', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('bollinger_upper', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('bollinger_lower', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_feature_store'))
    )
    op.create_index('idx_feature_symbol_time_version', 'feature_store', ['symbol', 'timestamp', 'version'], unique=False)
    op.create_index('idx_feature_symbol', 'feature_store', ['symbol'], unique=False)
    
    # Training Runs - ML model training sessions
    op.create_table('training_runs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('model_name', sa.String(length=100), nullable=False),
        sa.Column('model_type', sa.String(length=50), nullable=False),
        sa.Column('start_date', sa.DATE(), nullable=False),
        sa.Column('end_date', sa.DATE(), nullable=False),
        sa.Column('feature_version', sa.String(length=20), nullable=False),
        sa.Column('train_size', sa.Integer(), nullable=True),
        sa.Column('test_size', sa.Integer(), nullable=True),
        sa.Column('hyperparameters', sa.JSON(), nullable=True),
        sa.Column('train_accuracy', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('test_accuracy', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('train_auc', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('test_auc', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('precision', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('recall', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('f1_score', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_training_runs'))
    )
    op.create_index('idx_training_model', 'training_runs', ['model_name'], unique=False)
    op.create_index('idx_training_status', 'training_runs', ['status'], unique=False)
    
    # Model Registry - Production model tracking
    op.create_table('model_registry',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('model_name', sa.String(length=100), nullable=False),
        sa.Column('model_type', sa.String(length=50), nullable=False),
        sa.Column('version', sa.String(length=20), nullable=False),
        sa.Column('training_run_id', sa.BigInteger(), nullable=True),
        sa.Column('model_path', sa.String(length=500), nullable=False),
        sa.Column('framework', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='DRAFT', nullable=False),
        sa.Column('accuracy', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('production_date', sa.DATE(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(['training_run_id'], ['training_runs.id'], name=op.f('fk_registry_training'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_model_registry'))
    )
    op.create_index('idx_model_registry_name_version', 'model_registry', ['model_name', 'version'], unique=False)
    op.create_index('idx_model_registry_status', 'model_registry', ['status'], unique=False)
    
    # Model Metrics - Model performance metrics over time
    op.create_table('model_metrics',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('model_id', sa.BigInteger(), nullable=False),
        sa.Column('date', sa.DATE(), nullable=False),
        sa.Column('predictions_count', sa.Integer(), nullable=True),
        sa.Column('accuracy', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('precision', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('recall', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('auc', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('mse', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column('rmse', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column('mae', sa.Numeric(precision=18, scale=8), nullable=True),
        sa.ForeignKeyConstraint(['model_id'], ['model_registry.id'], name=op.f('fk_metrics_model'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_model_metrics'))
    )
    op.create_index('idx_model_metrics_model_date', 'model_metrics', ['model_id', 'date'], unique=False)
    
    # Predictions - Model predictions for live trading
    op.create_table('predictions',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('model_id', sa.BigInteger(), nullable=False),
        sa.Column('symbol', sa.String(length=30), nullable=False),
        sa.Column('timestamp', sa.TIMESTAMP(), nullable=False),
        sa.Column('prediction', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('confidence', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('actual_value', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('is_correct', sa.Boolean(), nullable=True),
        sa.Column('features_used', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['model_id'], ['model_registry.id'], name=op.f('fk_predictions_model'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_predictions'))
    )
    op.create_index('idx_predictions_symbol_timestamp', 'predictions', ['symbol', 'timestamp'], unique=False)
    op.create_index('idx_predictions_model', 'predictions', ['model_id'], unique=False)
    
    # ==================== AUDIT TABLES ====================
    
    # Audit Logs - General audit trail
    op.create_table('audit_logs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('entity_type', sa.String(length=100), nullable=False),
        sa.Column('entity_id', sa.BigInteger(), nullable=False),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('old_values', sa.JSON(), nullable=True),
        sa.Column('new_values', sa.JSON(), nullable=True),
        sa.Column('changed_by', sa.String(length=100), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    op.create_index('idx_audit_entity', 'audit_logs', ['entity_type', 'entity_id'], unique=False)
    op.create_index('idx_audit_action', 'audit_logs', ['action'], unique=False)
    op.create_index('idx_audit_timestamp', 'audit_logs', ['created_at'], unique=False)
    
    # System Logs - System operation logs
    op.create_table('system_logs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('level', sa.String(length=20), nullable=False),
        sa.Column('component', sa.String(length=100), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('stack_trace', sa.Text(), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_system_logs'))
    )
    op.create_index('idx_system_logs_level', 'system_logs', ['level'], unique=False)
    op.create_index('idx_system_logs_component', 'system_logs', ['component'], unique=False)
    op.create_index('idx_system_logs_timestamp', 'system_logs', ['created_at'], unique=False)
    
    # API Logs - API request/response logging
    op.create_table('api_logs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('request_id', sa.String(length=100), nullable=False, unique=True),
        sa.Column('method', sa.String(length=20), nullable=False),
        sa.Column('endpoint', sa.String(length=500), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('response_time_ms', sa.Integer(), nullable=True),
        sa.Column('request_size', sa.Integer(), nullable=True),
        sa.Column('response_size', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.String(length=100), nullable=True),
        sa.Column('ip_address', sa.String(length=50), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_api_logs'))
    )
    op.create_index('idx_api_logs_endpoint', 'api_logs', ['endpoint'], unique=False)
    op.create_index('idx_api_logs_status', 'api_logs', ['status_code'], unique=False)
    op.create_index('idx_api_logs_timestamp', 'api_logs', ['created_at'], unique=False)
    
    # Deployment Logs - Deployment and release tracking
    op.create_table('deployment_logs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('deployment_id', sa.String(length=100), nullable=False, unique=True),
        sa.Column('version', sa.String(length=50), nullable=False),
        sa.Column('environment', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('deployed_by', sa.String(length=100), nullable=True),
        sa.Column('commit_hash', sa.String(length=100), nullable=True),
        sa.Column('deployment_time_sec', sa.Integer(), nullable=True),
        sa.Column('previous_version', sa.String(length=50), nullable=True),
        sa.Column('rollback_info', sa.JSON(), nullable=True),
        sa.Column('changes', sa.Text(), nullable=True),
        sa.Column('health_check_status', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_deployment_logs'))
    )
    op.create_index('idx_deployment_version', 'deployment_logs', ['version'], unique=False)
    op.create_index('idx_deployment_status', 'deployment_logs', ['status'], unique=False)
    op.create_index('idx_deployment_timestamp', 'deployment_logs', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema - Remove backtest, risk, ML, and audit tables."""
    
    # Reverse order of table creation
    op.drop_index('idx_deployment_timestamp', table_name='deployment_logs')
    op.drop_index('idx_deployment_status', table_name='deployment_logs')
    op.drop_index('idx_deployment_version', table_name='deployment_logs')
    op.drop_table('deployment_logs')
    
    op.drop_index('idx_api_logs_timestamp', table_name='api_logs')
    op.drop_index('idx_api_logs_status', table_name='api_logs')
    op.drop_index('idx_api_logs_endpoint', table_name='api_logs')
    op.drop_table('api_logs')
    
    op.drop_index('idx_system_logs_timestamp', table_name='system_logs')
    op.drop_index('idx_system_logs_component', table_name='system_logs')
    op.drop_index('idx_system_logs_level', table_name='system_logs')
    op.drop_table('system_logs')
    
    op.drop_index('idx_audit_timestamp', table_name='audit_logs')
    op.drop_index('idx_audit_action', table_name='audit_logs')
    op.drop_index('idx_audit_entity', table_name='audit_logs')
    op.drop_table('audit_logs')
    
    op.drop_index('idx_predictions_model', table_name='predictions')
    op.drop_index('idx_predictions_symbol_timestamp', table_name='predictions')
    op.drop_table('predictions')
    
    op.drop_index('idx_model_metrics_model_date', table_name='model_metrics')
    op.drop_table('model_metrics')
    
    op.drop_index('idx_model_registry_status', table_name='model_registry')
    op.drop_index('idx_model_registry_name_version', table_name='model_registry')
    op.drop_table('model_registry')
    
    op.drop_index('idx_training_status', table_name='training_runs')
    op.drop_index('idx_training_model', table_name='training_runs')
    op.drop_table('training_runs')
    
    op.drop_index('idx_feature_symbol', table_name='feature_store')
    op.drop_index('idx_feature_symbol_time_version', table_name='feature_store')
    op.drop_table('feature_store')
    
    op.drop_index('idx_daily_risk_metrics_date', table_name='daily_risk_metrics')
    op.drop_table('daily_risk_metrics')
    
    op.drop_index('idx_risk_violations_timestamp', table_name='risk_violations')
    op.drop_index('idx_risk_violations_rule', table_name='risk_violations')
    op.drop_table('risk_violations')
    
    op.drop_index('idx_risk_events_timestamp', table_name='risk_events')
    op.drop_index('idx_risk_events_severity', table_name='risk_events')
    op.drop_table('risk_events')
    
    op.drop_index('idx_risk_rules_active', table_name='risk_rules')
    op.drop_table('risk_rules')
    
    op.drop_index('idx_backtest_drawdown_run', table_name='backtest_drawdowns')
    op.drop_table('backtest_drawdowns')
    
    op.drop_index('idx_backtest_equity_run_date', table_name='backtest_equity_curve')
    op.drop_table('backtest_equity_curve')
    
    op.drop_index('idx_backtest_trades_symbol', table_name='backtest_trades')
    op.drop_index('idx_backtest_trades_run', table_name='backtest_trades')
    op.drop_table('backtest_trades')
    
    op.drop_index('idx_backtest_results_symbol', table_name='backtest_results')
    op.drop_index('idx_backtest_results_run', table_name='backtest_results')
    op.drop_table('backtest_results')
    
    op.drop_index('idx_backtest_runs_dates', table_name='backtest_runs')
    op.drop_index('idx_backtest_runs_strategy', table_name='backtest_runs')
    op.drop_table('backtest_runs')
    
    op.drop_index('idx_ticks_timestamp', table_name='ticks')
    op.drop_index('idx_ticks_symbol_time', table_name='ticks')
    op.drop_table('ticks')
