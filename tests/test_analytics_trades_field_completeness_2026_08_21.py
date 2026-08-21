"""
analytics_router._trade_to_dict() was missing every field added across three
external-review rounds on 2026-08-21 (momentum_v1/ema_crossover_v1's entry-
context snapshot + running MFE/MAE, credit_spread_v1/iron_condor_v1's daily
ATR/credit-max-loss/wing-failure) -- the data was being written to
trade_journal correctly, but /analytics/trades (which feeds the dashboard's
Closed Trade Log) silently dropped all of it. Found while auditing the
dashboard for what needed updating after that work.
"""
from types import SimpleNamespace
from datetime import datetime

from src.api.routers.analytics_router import _trade_to_dict


def _fake_trade_journal_row(**overrides):
    base = dict(
        id=1, strategy_name="momentum_v1", underlying="RELIANCE",
        structure_type="SINGLE_LEG", entry_time=datetime(2026, 8, 21, 10, 0),
        exit_time=datetime(2026, 8, 21, 11, 0), entry_price=40.0, exit_price=45.0,
        quantity=750, pnl=3750.0, hold_days=0, exit_reason="Target hit",
        regime_atr_pct=1.2, iv_rank=0.4, vix_at_entry=13.5, day_of_week=4, hour_of_day=10,
        atr_at_exit=1.3, vix_at_exit=13.8, regime_label="TRENDING",
        total_slippage_pts=0.5, slippage=0.5,
        underlying_price_at_entry=2500.0, rvol_at_entry=1.6, adx_at_entry=30.0,
        dte_at_entry=25, delta_at_entry=0.6,
        underlying_mfe_pct=2.1, underlying_mae_pct=-0.5,
        option_mfe_pct=15.0, option_mae_pct=-8.0,
        daily_atr_pct=1.8, credit_to_max_loss_pct=25.0, wing_failed=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_trade_to_dict_includes_the_entry_context_and_mfe_mae_fields():
    row = _fake_trade_journal_row()
    d = _trade_to_dict(row)
    for field in (
        "underlying_price_at_entry", "rvol_at_entry", "adx_at_entry",
        "dte_at_entry", "delta_at_entry",
        "underlying_mfe_pct", "underlying_mae_pct", "option_mfe_pct", "option_mae_pct",
    ):
        assert field in d, f"missing field: {field}"
        assert d[field] == getattr(row, field)


def test_trade_to_dict_includes_the_premium_selling_analytics_fields():
    row = _fake_trade_journal_row(
        strategy_name="iron_condor_v1", structure_type="IRON_CONDOR",
        daily_atr_pct=1.9, credit_to_max_loss_pct=22.5, wing_failed="PUT",
    )
    d = _trade_to_dict(row)
    assert d["daily_atr_pct"] == 1.9
    assert d["credit_to_max_loss_pct"] == 22.5
    assert d["wing_failed"] == "PUT"


def test_trade_to_dict_handles_null_new_fields_for_older_rows():
    """Rows written before these columns existed (or by structure types that
    don't populate them) must not crash -- all new fields are nullable."""
    row = _fake_trade_journal_row(
        underlying_price_at_entry=None, rvol_at_entry=None, adx_at_entry=None,
        dte_at_entry=None, delta_at_entry=None,
        underlying_mfe_pct=None, underlying_mae_pct=None,
        option_mfe_pct=None, option_mae_pct=None,
        daily_atr_pct=None, credit_to_max_loss_pct=None, wing_failed=None,
    )
    d = _trade_to_dict(row)
    assert d["wing_failed"] is None
    assert d["daily_atr_pct"] is None
