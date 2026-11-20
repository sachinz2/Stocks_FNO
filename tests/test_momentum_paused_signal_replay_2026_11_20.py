"""
Paused-signal replay queue (2026-11-20, data-driven, part of the
pre-2027-01-02-go-live fine-tuning pass).

generate_signal() runs every cycle regardless of pause state (the 2026-09-03
fix, for state continuity) and can genuinely CONFIRM a fresh BUY/SELL while
the strategy is still regime-paused -- confirmed live 2026-11-11:
momentum_v1's pullback+breakout model (multi-bar confirmation, can take many
minutes) fully tracked and confirmed ~20 real setups during a single
sustained RANGE_BOUND stretch, every one discarded as a mere "paused signal"
observation. This is NOT a setup interrupted mid-confirmation by the pause
(on_pause() already wipes in-flight tracking at the pause transition) -- it's
a brand-new one fully built and confirmed entirely while already paused.

Fix: queue the most recent confirmed-while-regime-paused signal per
(strategy, symbol); consume it (replay) the next time that strategy is
active again for that symbol, if still within a short grace window and the
underlying hasn't drifted too far since confirmation.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import datetime as _dt

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.enums import SignalType


class _FakeRepo:
    created = []

    def __init__(self, model, session):
        pass

    async def create(self, obj_in):
        _FakeRepo.created.append(obj_in)
        return obj_in


@pytest.fixture(autouse=True)
def _wire_fake_repo(monkeypatch):
    import src.database.repositories.base as base_mod
    _FakeRepo.created = []
    monkeypatch.setattr(base_mod, "BaseRepository", _FakeRepo)
    yield


class _FakeEngine:
    _audit_gate = LiveTradingEngine._audit_gate
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure
    _maybe_record_shadow_candidate = LiveTradingEngine._maybe_record_shadow_candidate
    _SHADOW_ELIGIBLE_STRATEGIES = LiveTradingEngine._SHADOW_ELIGIBLE_STRATEGIES
    _SHADOW_MIN_STREAK_DAYS = LiveTradingEngine._SHADOW_MIN_STREAK_DAYS
    _maybe_record_paused_signal_outcome = LiveTradingEngine._maybe_record_paused_signal_outcome
    _record_rejected_outcome = LiveTradingEngine._record_rejected_outcome
    _compute_trade_quality_score = LiveTradingEngine._compute_trade_quality_score
    _PAUSED_SIGNAL_OUTCOME_STRATEGIES = LiveTradingEngine._PAUSED_SIGNAL_OUTCOME_STRATEGIES
    _ELEVATED_VIX_ENTRY_CEILING = LiveTradingEngine._ELEVATED_VIX_ENTRY_CEILING

    def __init__(self, market_data, now=None):
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {}
        self._exited_today = set()
        self._max_daily_orders = 0
        self._max_concurrent_intraday = 999
        self._last_signal_date = {}
        self._signal_gate_stats = {}
        self._last_gate_rejection = None
        self._last_signal_metrics = {}
        self._paused_confirmed_signals = {}
        self._close_option_positions = AsyncMock()
        self._has_open_option = AsyncMock(return_value=False)
        self._get_market_data = AsyncMock(return_value=market_data)
        self._redis = None
        self.rs_ranker = None


def _bar(close=100.0):
    return {
        "close": close, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 30.0, "adx_valid": True,
    }


@pytest.mark.asyncio
async def test_confirmed_signal_while_regime_paused_is_queued():
    fake = _FakeEngine(_bar())
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=False, paused_by="regime",
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "CANBK", vix=15.0, regime="RANGE_BOUND")

    queued = fake._paused_confirmed_signals.get(("momentum_v1", "CANBK"))
    assert queued is not None
    assert queued["signal"] == "BUY"
    assert queued["close"] == 100.0
    fake._close_option_positions.assert_not_awaited()  # never acted on while paused


@pytest.mark.asyncio
async def test_not_queued_for_a_non_regime_pause():
    # A StrategyMonitor circuit-breaker / manual pause is a deliberate
    # "stop trading" decision, not a timing race -- must not queue.
    fake = _FakeEngine(_bar())
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=False, paused_by="monitor",
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "CANBK", vix=15.0, regime="RANGE_BOUND")

    assert fake._paused_confirmed_signals == {}


@pytest.mark.asyncio
async def test_queued_signal_is_replayed_once_active_again_within_grace_window():
    now = _dt.datetime(2026, 11, 11, 11, 35, 0)
    fake = _FakeEngine(_bar(close=100.5), now=now)  # drifted slightly, within tolerance
    fake._paused_confirmed_signals[("momentum_v1", "CANBK")] = {
        "signal": "BUY", "timestamp": now - _dt.timedelta(minutes=5), "close": 100.0,
    }
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, paused_by=None,
        generate_signal=lambda market_data: SignalType.HOLD,
    )

    fake._get_lot_size = AsyncMock(return_value=None)  # stop gracefully right after the real pipeline is reached
    import src.live_trading.live_trading_engine as lte_module
    import unittest.mock as _mock
    with _mock.patch.object(lte_module, "now_ist", return_value=now):
        await LiveTradingEngine._process_signal(fake, strategy, "CANBK", vix=15.0, regime="TRENDING")

    assert ("momentum_v1", "CANBK") not in fake._paused_confirmed_signals  # consumed
    fake._close_option_positions.assert_awaited_once()  # acted on the replayed BUY


@pytest.mark.asyncio
async def test_queued_signal_dropped_when_too_stale():
    now = _dt.datetime(2026, 11, 11, 12, 0, 0)
    fake = _FakeEngine(_bar(close=100.0))
    fake._paused_confirmed_signals[("momentum_v1", "CANBK")] = {
        "signal": "BUY", "timestamp": now - _dt.timedelta(minutes=20), "close": 100.0,  # > 15 min grace
    }
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, paused_by=None,
        generate_signal=lambda market_data: SignalType.HOLD,
    )

    import src.live_trading.live_trading_engine as lte_module
    import unittest.mock as _mock
    with _mock.patch.object(lte_module, "now_ist", return_value=now):
        await LiveTradingEngine._process_signal(fake, strategy, "CANBK", vix=15.0, regime="TRENDING")

    assert ("momentum_v1", "CANBK") not in fake._paused_confirmed_signals  # consumed either way
    fake._close_option_positions.assert_not_awaited()  # too stale, not acted on


@pytest.mark.asyncio
async def test_queued_signal_dropped_when_price_drifted_too_far():
    now = _dt.datetime(2026, 11, 11, 11, 40, 0)
    fake = _FakeEngine(_bar(close=105.0))  # 5% away from the queued close -- beyond 2% tolerance
    fake._paused_confirmed_signals[("momentum_v1", "CANBK")] = {
        "signal": "BUY", "timestamp": now - _dt.timedelta(minutes=5), "close": 100.0,
    }
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, paused_by=None,
        generate_signal=lambda market_data: SignalType.HOLD,
    )

    import src.live_trading.live_trading_engine as lte_module
    import unittest.mock as _mock
    with _mock.patch.object(lte_module, "now_ist", return_value=now):
        await LiveTradingEngine._process_signal(fake, strategy, "CANBK", vix=15.0, regime="TRENDING")

    assert ("momentum_v1", "CANBK") not in fake._paused_confirmed_signals
    fake._close_option_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_fresh_signal_this_cycle_takes_priority_and_queue_is_discarded():
    now = _dt.datetime(2026, 11, 11, 11, 40, 0)
    fake = _FakeEngine(_bar(close=100.0))
    fake._paused_confirmed_signals[("momentum_v1", "CANBK")] = {
        "signal": "SELL", "timestamp": now - _dt.timedelta(minutes=5), "close": 100.0,
    }
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, paused_by=None,
        generate_signal=lambda market_data: SignalType.BUY,  # fresh signal this cycle
    )

    fake._get_lot_size = AsyncMock(return_value=None)  # stop gracefully right after the real pipeline is reached
    import src.live_trading.live_trading_engine as lte_module
    import unittest.mock as _mock
    with _mock.patch.object(lte_module, "now_ist", return_value=now):
        await LiveTradingEngine._process_signal(fake, strategy, "CANBK", vix=15.0, regime="TRENDING")

    assert ("momentum_v1", "CANBK") not in fake._paused_confirmed_signals
    fake._close_option_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_fake_engine_without_the_replay_queue_attribute_degrades_gracefully():
    # Guard against the exact AttributeError this feature's rollout hit
    # across ~8 pre-existing fake-engine test fixtures that don't construct
    # this dict.
    fake = _FakeEngine(_bar())
    del fake._paused_confirmed_signals
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=False, paused_by="regime",
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "CANBK", vix=15.0, regime="RANGE_BOUND")  # must not raise
