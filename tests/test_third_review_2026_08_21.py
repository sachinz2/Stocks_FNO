"""
A third review pass (2026-08-21) covering code already touched twice the
same day. Several claims (bar_key=None edge case, pullback/breakout model
"not implemented", OI-bump delta revalidation, order-timeout FAILED
fallback, entry-price estimate fallback) were verified against the current
code and found to be STALE -- already fixed in the two prior rounds the
same day, describing an earlier snapshot of the code. Only the genuinely
new findings were implemented here:

- RS (top-10 vs NIFTY) was the one remaining shared-engine entry filter
  with no per-strategy override, unlike RVOL/MTF/ADX which already got
  this treatment. Added require_rs, defaulting False for ema_crossover_v1.
- GTT backstop placement raced with active-structure publication: the
  structure was visible to a concurrent exit-check cycle before its GTT id
  was resolved, so a normal exit during that window would call
  _cancel_gtt(None, ...) -- a no-op -- orphaning the real GTT placed
  moments later. Reordered to place the GTT first, publish the complete
  dict (gtt_id already resolved) in one atomic assignment.
- Entry Greeks (delta/IV/wing width) for credit_spread_v1/iron_condor_v1
  were not being stored, only derivable indirectly. Added, computed from
  the final resolved strikes/fills.
- Capital reservation (add_deployed_capital/release_deployed_capital) used
  the pre-trade quote-based net_credit rather than the actual entry fills,
  letting reserved risk diverge from real economic risk on every trade.
  Reservation now recomputes from real fills after they're known; the
  matching exit-side release uses the same real-fill basis.
"""
import inspect

from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.momentum import MomentumStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine
from src.api.routers.analytics_router import _trade_to_dict


def _ema(**overrides):
    strat = EMACrossoverStrategy("ema_crossover_v1", overrides)
    strat.initialize()
    return strat


# ── require_rs: strategy-specific RS override ────────────────────────────────

def test_ema_crossover_defaults_require_rs_false():
    strat = _ema()
    assert strat.require_rs is False


def test_momentum_keeps_the_shared_require_rs_default_true():
    """Guard against over-fixing -- momentum_v1 never sets require_rs, so
    getattr's own default (True, unchanged behavior) must still apply."""
    mom = MomentumStrategy("momentum_v1", {})
    mom.initialize()
    assert getattr(mom, "require_rs", True) is True


def test_engine_rs_gate_respects_require_rs_override():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    rs_block = src[src.index("Relative Strength filter"):src.index("Multi-timeframe confirmation")]
    assert 'getattr(strategy, "require_rs", True)' in rs_block


def test_main_py_wires_require_rs_false_for_ema_crossover():
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    idx = src.index('StrategyRegistry.load_strategy("EMA_CROSSOVER"')
    block = src[idx:idx + 2400]
    assert '"require_rs": False' in block


# ── GTT / active-structure publication race ──────────────────────────────────

def test_credit_spread_places_gtt_before_publishing_active_structure():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    gtt_idx = src.index("_gtt_id = await self._place_gtt_backstop(short_contract")
    publish_idx = src.index("self._active_spreads[symbol] = {")
    assert gtt_idx < publish_idx, "GTT must be placed BEFORE the structure becomes visible to exit checks"
    # The published dict must use the already-resolved gtt_id directly, not
    # a None placeholder filled in afterward.
    block = src[publish_idx:publish_idx + 900]
    assert '"gtt_id":         _gtt_id,' in block


def test_iron_condor_places_gtts_before_publishing_active_structure():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    gtt_idx = src.index("_put_gtt  = await self._place_gtt_backstop(psc")
    publish_idx = src.index("self._active_condors[symbol] = {")
    assert gtt_idx < publish_idx
    block = src[publish_idx:publish_idx + 1200]
    assert '"put_short_gtt_id":    _put_gtt,' in block
    assert '"call_short_gtt_id":   _call_gtt,' in block


# ── Entry Greeks storage ──────────────────────────────────────────────────────

def test_credit_spread_computes_and_stores_entry_greeks():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    assert "bs_delta(underlying_price, short_strike" in src
    assert "implied_vol as _implied_vol_fn" in src
    assert "put_short_delta=_short_delta_val" in src or "call_short_delta=_short_delta_val" in src


def test_iron_condor_computes_and_stores_entry_greeks_for_both_wings():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert "bs_delta(underlying_price, put_short_strike" in src
    assert "bs_delta(underlying_price, call_short_strike" in src
    assert "put_iv=_put_iv_val, call_iv=_call_iv_val" in src


def test_analytics_trade_to_dict_includes_entry_greeks():
    from types import SimpleNamespace
    row = SimpleNamespace(
        id=1, strategy_name="iron_condor_v1", underlying="SBIN", structure_type="IRON_CONDOR",
        entry_time=None, exit_time=None, entry_price=10.0, exit_price=5.0, quantity=750,
        pnl=100.0, hold_days=1, exit_reason="test", regime_atr_pct=None, iv_rank=None,
        vix_at_entry=None, day_of_week=None, hour_of_day=None, atr_at_exit=None, vix_at_exit=None,
        regime_label=None, total_slippage_pts=None, slippage=None,
        underlying_price_at_entry=None, rvol_at_entry=None, adx_at_entry=None, dte_at_entry=None,
        delta_at_entry=None, underlying_mfe_pct=None, underlying_mae_pct=None,
        option_mfe_pct=None, option_mae_pct=None, daily_atr_pct=None, credit_to_max_loss_pct=None,
        wing_failed=None,
        put_short_delta=-0.19, call_short_delta=0.21, put_long_delta=-0.09, call_long_delta=0.11,
        put_iv=0.22, call_iv=0.24, put_wing_width=100.0, call_wing_width=100.0,
    )
    d = _trade_to_dict(row)
    assert d["put_short_delta"] == -0.19
    assert d["call_iv"] == 0.24
    assert d["put_wing_width"] == 100.0


def test_trade_journal_model_has_the_greek_columns():
    from src.database.models.trade_journal import TradeJournal
    for col in (
        "put_short_delta", "call_short_delta", "put_long_delta", "call_long_delta",
        "put_iv", "call_iv", "put_wing_width", "call_wing_width",
    ):
        assert hasattr(TradeJournal, col), f"missing column: {col}"


def test_migration_b007_is_the_new_head_after_b006():
    import importlib
    b007 = importlib.import_module("migrations.versions.b007_add_entry_greeks")
    assert b007.down_revision == "b006"
    assert b007.revision == "b007"


# ── Capital reservation from real fills, not quotes ──────────────────────────

def test_credit_spread_add_deployed_capital_uses_real_fills():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("self.risk_manager.add_deployed_capital(strategy.name, _capital_at_risk_actual)")
    block = src[max(0, idx - 400):idx]
    assert "_capital_at_risk_actual = (spread_width - (short_fill - long_fill)) * lot_size" in block


def test_credit_spread_release_deployed_capital_uses_real_fills():
    src = inspect.getsource(LiveTradingEngine._check_spread_exits)
    assert 'spread["short_premium"] - spread["long_premium"]' in src
    assert 'spread["net_credit"]' not in src.split("release_deployed_capital")[1][:400] if "release_deployed_capital" in src else True


def test_iron_condor_add_deployed_capital_uses_real_fills():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert "_fill_net_credit = (put_short_fill - put_long_fill) + (call_short_fill - call_long_fill)" in src
    assert "_capital_at_risk_actual = (wing_spread - _fill_net_credit) * lot_size" in src


def test_iron_condor_release_deployed_capital_uses_real_fills():
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    idx = src.index("_fill_net_credit = (")
    block = src[idx:idx + 300]
    assert 'c["put_short_premium"] - c["put_long_premium"]' in block
    assert 'c["call_short_premium"] - c["call_long_premium"]' in block


# ── Verify prior-round claims are genuinely already fixed (not re-broken) ────

def test_bar_key_none_first_transition_still_does_not_advance_momentum():
    strat = MomentumStrategy("momentum_v1", {
        "signal_confirm_bars": 2, "adx_rising_required": False, "ema_slope_required": False,
        "extension_atr_mult": 0, "vwap_extension_pct": 0, "use_pullback_continuation_model": False,
    })
    strat.initialize()
    bar = dict(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx14=40.0)
    strat.generate_signal({**bar, "ohlc_bar_key": None})
    assert strat.generate_signal({**bar, "ohlc_bar_key": "live:t0"}) == "HOLD"
    assert strat._pending_count["RELIANCE"] == 1


def test_pullback_continuation_model_is_still_the_default():
    mom = MomentumStrategy("momentum_v1", {})
    mom.initialize()
    assert mom.use_pullback_continuation_model is True


def test_oi_bump_delta_revalidation_still_present():
    assert hasattr(LiveTradingEngine, "_find_non_crowded_strike_within_delta_tolerance")
    assert hasattr(LiveTradingEngine, "_resolved_strike_delta_ok")


def test_order_timeout_pending_verification_still_present():
    from src.orders.order_manager import OrderManager
    src = inspect.getsource(OrderManager.place_order)
    assert "PENDING_VERIFICATION" in src


def test_entry_estimate_fallback_still_removed():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    block = src[src.index("get_option_quote(contract"):src.index("order = await self.order_manager.place_order")]
    assert "estimate_option_premium" not in block
