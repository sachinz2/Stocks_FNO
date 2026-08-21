"""
External PDF review of credit_spread_v1/iron_condor_v1 (2026-08-21) -- unlike
the momentum_v1/ema_crossover_v1 reviews the same week, this one's verdict was
"keep/refine, don't redesign" (both strategies scored 8.5/10 on code quality
and design). Per the review's own explicit "I would NOT fix what's already
working" and "don't start changing 10 parameters," only concrete bugs and
non-gating data-collection additions are implemented here -- NOT the
parameter changes the review itself frames as "test before changing"
(asymmetric CE/PE delta selection, profit-target percentage, regime-shift
exit timing, ATR-normalized EMA-flat threshold, R/R threshold). See
docs/LIVE_TRADING_CHECKLIST.md for the full accounting.

Covers:
  1. Credit Spread issue #1 / Iron Condor issue #1: crowded-strike avoidance
     now verifies the resulting delta stays within tolerance instead of
     blindly moving 1 interval.
  2. Credit Spread issue #5: a GTT firing on the short leg while the process
     is offline is now detected and reconciled on restart (long leg
     force-flattened, structure removed from tracking) instead of silently
     leaving the engine's model out of sync with the broker.
  3. Data collection (non-gating): daily ATR%, credit/max-loss%, iron condor
     wing-failure classification.
"""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.market_data.rs_ranker import RSRanker
from src.core.enums import TradingMode


# ── Crowded-strike delta verification ────────────────────────────────────────

def test_finds_non_crowded_strike_within_delta_tolerance():
    # base_strike is what find_delta_strike(-0.20) would already have
    # returned for this underlying/dte/sigma (~19300, delta ~-0.199) --
    # matches how this helper is actually called in production (AFTER the
    # initial delta-targeted strike is computed, not from ATM).
    import src.market_data.nse_oi as nse_oi_module
    crowded_strikes = {19250}  # 1 interval OTM from the base strike

    original = nse_oi_module.is_strike_crowded
    nse_oi_module.is_strike_crowded = lambda strike, oi_data, opt: strike in crowded_strikes
    try:
        result = LiveTradingEngine._find_non_crowded_strike_within_delta_tolerance(
            oi_data={}, opt="PE", base_strike=19300, interval=50,
            target_delta=-0.20, underlying_price=20000, dte=21, sigma=0.20,
        )
        assert result is not None
        assert result != 19250  # the crowded one must be skipped
    finally:
        nse_oi_module.is_strike_crowded = original


def test_returns_none_when_no_acceptable_strike_found_fails_closed():
    import src.market_data.nse_oi as nse_oi_module
    original = nse_oi_module.is_strike_crowded
    nse_oi_module.is_strike_crowded = lambda strike, oi_data, opt: True  # everything crowded
    try:
        result = LiveTradingEngine._find_non_crowded_strike_within_delta_tolerance(
            oi_data={}, opt="PE", base_strike=20000, interval=50,
            target_delta=-0.20, underlying_price=20000, dte=21, sigma=0.20,
        )
        assert result is None
    finally:
        nse_oi_module.is_strike_crowded = original


def test_skips_a_non_crowded_strike_whose_delta_has_drifted_too_far():
    """A far-OTM strike might not be crowded but its delta could be well
    outside tolerance -- must not be accepted just because it's non-crowded."""
    import src.market_data.nse_oi as nse_oi_module
    original = nse_oi_module.is_strike_crowded
    # Nothing is "crowded" per this OI check, but with max_steps=1 and a tiny
    # tolerance, a single 1-interval step from a 0.20 target with this
    # sigma/dte should still land close enough -- use a deliberately huge
    # interval so 1 step overshoots the tolerance band instead.
    nse_oi_module.is_strike_crowded = lambda strike, oi_data, opt: False
    try:
        result = LiveTradingEngine._find_non_crowded_strike_within_delta_tolerance(
            oi_data={}, opt="PE", base_strike=19300, interval=2000,  # absurdly wide interval
            target_delta=-0.20, underlying_price=20000, dte=21, sigma=0.20,
            max_steps=1, delta_tol=0.01,  # razor-thin tolerance
        )
        assert result is None  # 1 step of 2000 points blows way past 0.01 tolerance
    finally:
        nse_oi_module.is_strike_crowded = original


def test_credit_spread_crowd_avoidance_uses_the_delta_verified_helper():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    block = src[src.index("Avoid selling at crowded"):src.index("_entry_prices = await get_entry_prices_for_spread")]
    assert "_find_non_crowded_strike_within_delta_tolerance" in block
    assert "short_strike -= interval" not in block  # old unconditional move must be gone
    assert "short_strike += interval" not in block


def test_iron_condor_crowd_avoidance_uses_the_delta_verified_helper():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    block = src[src.index("Crowded-strike avoidance"):src.index("if underlying_price <= put_short_strike")]
    assert block.count("_find_non_crowded_strike_within_delta_tolerance") == 2
    assert "put_short_strike -= interval" not in block
    assert "call_short_strike += interval" not in block


# ── GTT-orphan reconciliation (Credit Spread issue #5) ───────────────────────

class _FakeReconcileEngine:
    _reconcile_partially_closed_multi_leg_legs = LiveTradingEngine._reconcile_partially_closed_multi_leg_legs
    _cancel_gtt = LiveTradingEngine._cancel_gtt

    def __init__(self, active_spreads=None, active_condors=None, broker_positions=None):
        self.mode = TradingMode.LIVE
        self._active_spreads = active_spreads or {}
        self._active_condors = active_condors or {}
        self.broker = SimpleNamespace(get_positions=AsyncMock(return_value=broker_positions or []))
        self.order_manager = SimpleNamespace(place_order=AsyncMock(return_value=SimpleNamespace(order_status="OPEN", fill_price=None)))
        self.risk_manager = SimpleNamespace(release_deployed_capital=lambda *a, **k: None)
        self._notify = AsyncMock()
        self._log_trade_close = AsyncMock()
        self._kite = None
        self._redis = None


def _spread(short_c="SBIN26SEP800PE", long_c="SBIN26SEP780PE"):
    return {
        "short_contract": short_c, "long_contract": long_c,
        "short_strike": 800, "long_strike": 780, "net_credit": 20.0,
        "lot_size": 750, "journal_id": 1, "strategy_name": "credit_spread_v1",
        "gtt_id": 999,
    }


@pytest.mark.asyncio
async def test_paper_mode_never_runs_the_reconcile():
    fake = _FakeReconcileEngine(active_spreads={"SBIN": _spread()})
    fake.mode = TradingMode.PAPER
    await fake._reconcile_partially_closed_multi_leg_legs()
    fake.broker.get_positions.assert_not_called()


@pytest.mark.asyncio
async def test_intact_spread_short_and_long_both_open_is_left_alone():
    fake = _FakeReconcileEngine(
        active_spreads={"SBIN": _spread()},
        broker_positions=[
            {"symbol": "SBIN26SEP800PE", "quantity": -750, "avg_price": 20.0},
            {"symbol": "SBIN26SEP780PE", "quantity": 750,  "avg_price": 5.0},
        ],
    )
    await fake._reconcile_partially_closed_multi_leg_legs()
    assert "SBIN" in fake._active_spreads  # untouched
    fake.order_manager.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_gtt_fired_short_leg_flat_long_leg_still_open_gets_flattened():
    """The exact scenario the review flagged: short leg closed via GTT while
    offline, long leg survives at the broker -- must be force-flattened and
    the structure removed from tracking."""
    fake = _FakeReconcileEngine(
        active_spreads={"SBIN": _spread()},
        broker_positions=[
            # short leg absent entirely (GTT fully closed it -- flat = not listed)
            {"symbol": "SBIN26SEP780PE", "quantity": 750, "avg_price": 5.0},
        ],
    )
    await fake._reconcile_partially_closed_multi_leg_legs()
    assert "SBIN" not in fake._active_spreads
    fake.order_manager.place_order.assert_awaited_once()
    _, kwargs_or_args = fake.order_manager.place_order.call_args, None
    call = fake.order_manager.place_order.call_args
    assert call.args[0] == "SBIN26SEP780PE"
    assert call.args[1] == "SELL"  # long leg (positive qty) closes via SELL
    fake._log_trade_close.assert_awaited_once()
    fake._notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_both_legs_already_flat_is_not_treated_as_orphaned():
    """If the long leg is ALSO already flat (fully closed normally, just not
    yet removed from tracking for some other reason), this isn't the GTT-
    asymmetry scenario -- must not misfire."""
    fake = _FakeReconcileEngine(
        active_spreads={"SBIN": _spread()},
        broker_positions=[],  # both legs flat
    )
    await fake._reconcile_partially_closed_multi_leg_legs()
    assert "SBIN" in fake._active_spreads  # left alone -- not the asymmetric-GTT case
    fake.order_manager.place_order.assert_not_called()


def _condor():
    return {
        "put_short_contract": "SBIN26SEP780PE", "put_long_contract": "SBIN26SEP760PE",
        "call_short_contract": "SBIN26SEP820CE", "call_long_contract": "SBIN26SEP840CE",
        "put_short_strike": 780, "put_long_strike": 760,
        "call_short_strike": 820, "call_long_strike": 840,
        "net_credit": 30.0, "lot_size": 750, "journal_id": 2,
        "strategy_name": "iron_condor_v1",
        "put_short_gtt_id": 111, "call_short_gtt_id": 222,
    }


@pytest.mark.asyncio
async def test_condor_one_wing_gtt_fired_flattens_the_entire_structure():
    fake = _FakeReconcileEngine(
        active_condors={"SBIN": _condor()},
        broker_positions=[
            # put_short absent (GTT fired), put_long survives, call side intact
            {"symbol": "SBIN26SEP760PE", "quantity": 750,  "avg_price": 3.0},
            {"symbol": "SBIN26SEP820CE", "quantity": -750, "avg_price": 15.0},
            {"symbol": "SBIN26SEP840CE", "quantity": 750,  "avg_price": 4.0},
        ],
    )
    await fake._reconcile_partially_closed_multi_leg_legs()
    assert "SBIN" not in fake._active_condors
    # All 3 surviving legs (put_long, call_short, call_long) get flattened.
    assert fake.order_manager.place_order.await_count == 3
    fake._log_trade_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_intact_condor_all_four_legs_open_is_left_alone():
    fake = _FakeReconcileEngine(
        active_condors={"SBIN": _condor()},
        broker_positions=[
            {"symbol": "SBIN26SEP780PE", "quantity": -750, "avg_price": 8.0},
            {"symbol": "SBIN26SEP760PE", "quantity": 750,  "avg_price": 3.0},
            {"symbol": "SBIN26SEP820CE", "quantity": -750, "avg_price": 15.0},
            {"symbol": "SBIN26SEP840CE", "quantity": 750,  "avg_price": 4.0},
        ],
    )
    await fake._reconcile_partially_closed_multi_leg_legs()
    assert "SBIN" in fake._active_condors
    fake.order_manager.place_order.assert_not_called()


def test_restore_state_calls_the_reconcile_after_the_orphan_reconcile():
    src = inspect.getsource(LiveTradingEngine._restore_state)
    idx_orphan   = src.index("await self._reconcile_broker_positions()")
    idx_partial  = src.index("await self._reconcile_partially_closed_multi_leg_legs()")
    assert idx_partial > idx_orphan


# ── Data collection (non-gating): daily ATR, credit/max-loss, wing-failure ──

def test_get_daily_atr_pct_computes_true_range_based_atr():
    import pandas as pd
    ranker = RSRanker(redis_client=None, symbols=["SBIN"])
    # 20 days of mildly noisy OHLC around 100 -- daily range ~2 points.
    highs  = [101 + (i % 3) for i in range(20)]
    lows   = [99  - (i % 2) for i in range(20)]
    closes = [100 + (i % 4) - 2 for i in range(20)]
    ranker._cache["SBIN"] = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    result = ranker.get_daily_atr_pct("SBIN")
    assert result is not None
    assert 0 < result < 10  # sane %, not zero or absurd


def test_get_daily_atr_pct_returns_none_for_insufficient_history():
    import pandas as pd
    ranker = RSRanker(redis_client=None, symbols=["SBIN"])
    ranker._cache["SBIN"] = pd.DataFrame({"high": [101, 102], "low": [99, 98], "close": [100, 101]})
    assert ranker.get_daily_atr_pct("SBIN") is None


def test_get_daily_atr_pct_returns_none_for_unknown_symbol():
    ranker = RSRanker(redis_client=None, symbols=["SBIN"])
    assert ranker.get_daily_atr_pct("UNKNOWN") is None


def test_credit_spread_entry_computes_credit_to_max_loss_and_daily_atr():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    assert "_credit_to_max_loss" in src
    assert "get_daily_atr_pct" in src
    assert "credit_to_max_loss_pct=_credit_to_max_loss" in src


def test_iron_condor_entry_computes_credit_to_max_loss_and_daily_atr():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert "_credit_to_max_loss" in src
    assert "get_daily_atr_pct" in src


def test_condor_exit_classifies_wing_failed_from_exit_reason():
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    assert "wing_failed" in src
    assert '"BOTH"' in src and '"PUT"' in src and '"CALL"' in src


def test_trade_journal_model_has_the_new_analytics_columns():
    from src.database.models.trade_journal import TradeJournal
    for col in ("daily_atr_pct", "credit_to_max_loss_pct", "wing_failed"):
        assert hasattr(TradeJournal, col), f"missing column: {col}"


def test_migration_b006_is_the_new_head_after_b005():
    import importlib
    b006 = importlib.import_module("migrations.versions.b006_add_premium_selling_analytics")
    assert b006.down_revision == "b005"
    assert b006.revision == "b006"
