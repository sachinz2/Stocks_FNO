"""
Found while investigating momentum_v1 gate-tightening candidates (user:
"we should do something constructive of the time we have"): a 54-trade
RVOL-bucket analysis built on trade_journal.rvol_at_entry looked like a
clean, actionable pattern, but rvol_at_entry is market_data["rvol"] -- the
still-forming CURRENT bar's volume-so-far, which is NOT the variable
momentum_v1's own pullback+breakout entry logic (rvol_checked_internally=
True) actually gates on. That logic uses market_data["rvol_closed_bar"]
(ltp_poller.py) -- the last FINALIZED bar's volume, a different number.

Adds rvol_closed_bar_at_entry (migration b011) so a future RVOL analysis
for momentum_v1 has the right field to work with. NULL for ema_crossover_v1
(gated by the engine's shared RVOL check, which DOES use plain "rvol" --
rvol_at_entry is already correct for it, no mismatch to fix).

Per test_regime_label_entry_time_fix_2026_09_10.py's documented rationale,
_process_signal()/_log_trade_open() have too deep a precondition chain to
drive end-to-end with a mock -- source-level regression guards instead.
"""
import inspect

from src.live_trading.live_trading_engine import LiveTradingEngine


def test_log_trade_open_captures_rvol_closed_bar_from_market_data():
    src = inspect.getsource(LiveTradingEngine._log_trade_open)
    assert '_rvol_closed_e = market_data.get("rvol_closed_bar")' in src
    idx = src.index('"rvol_at_entry"')
    block = src[idx:idx + 300]
    assert '"rvol_closed_bar_at_entry": float(_rvol_closed_e) if _rvol_closed_e is not None else None' in block


def test_rvol_closed_bar_is_a_distinct_field_from_rvol_at_entry():
    """Guard against the exact mistake this fix corrects for -- the two
    fields must be populated from different market_data keys."""
    src = inspect.getsource(LiveTradingEngine._log_trade_open)
    assert '_rvol_e = market_data.get("rvol")' in src
    assert '_rvol_closed_e = market_data.get("rvol_closed_bar")' in src
    assert '_rvol_e = market_data.get("rvol_closed_bar")' not in src


def test_trade_journal_model_has_rvol_closed_bar_at_entry():
    from src.database.models.trade_journal import TradeJournal
    assert hasattr(TradeJournal, "rvol_closed_bar_at_entry")


def test_analytics_trade_to_dict_exposes_rvol_closed_bar_at_entry():
    from types import SimpleNamespace
    from src.api.routers.analytics_router import _trade_to_dict

    row = SimpleNamespace(
        id=1, strategy_name="momentum_v1", underlying="RELIANCE", structure_type="SINGLE_LEG",
        entry_time=None, exit_time=None, entry_price=40.0, exit_price=45.0, quantity=750,
        pnl=3750.0, hold_days=0, exit_reason="test", regime_atr_pct=None, iv_rank=None,
        vix_at_entry=None, day_of_week=None, hour_of_day=None, atr_at_exit=None, vix_at_exit=None,
        regime_label=None, total_slippage_pts=None, slippage=None,
        underlying_price_at_entry=None, rvol_at_entry=0.05, rvol_closed_bar_at_entry=1.42,
        adx_at_entry=None, dte_at_entry=None, delta_at_entry=None,
        underlying_mfe_pct=None, underlying_mae_pct=None, option_mfe_pct=None, option_mae_pct=None,
        daily_atr_pct=None, credit_to_max_loss_pct=None, wing_failed=None,
        put_short_delta=None, call_short_delta=None, put_long_delta=None, call_long_delta=None,
        put_iv=None, call_iv=None, put_wing_width=None, call_wing_width=None,
    )
    d = _trade_to_dict(row)
    assert d["rvol_closed_bar_at_entry"] == 1.42
    assert d["rvol_at_entry"] == 0.05
    assert d["rvol_closed_bar_at_entry"] != d["rvol_at_entry"], (
        "the two fields must stay independently readable, not collapsed to one"
    )
