"""
_maybe_record_paused_signal_outcome() (2026-10-01).

Live incident + external review: logs from 09-28/09-29/09-30 confirmed
RANGE_BOUND (mostly BEARISH) persisted essentially the whole session each
day, and ema_crossover_v1/momentum_v1 generated 100+ real CONFIRMED
signals/day (per their own "... confirmed ... firing" log lines) that were
all silently discarded at the is_active gate. The existing RS-sustained-
streak shadow trial (_maybe_record_shadow_candidate) only ever captured a
handful of these (its narrow RS-streak filter is a specific hypothesis
test, not general coverage) -- leaving over 95% of real signals invisible
to any analysis. This records EVERY real paused signal from either
strategy into the existing RejectedSignalOutcome forward-price-tracking
table (rejected_at_gate="regime_paused"), reusing the already-built
_backfill_rejected_outcomes() job and /analytics/rejected-outcomes-summary
endpoint -- zero new tracking infrastructure needed.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine


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


class _FakeOutcomeEngine:
    _maybe_record_paused_signal_outcome = LiveTradingEngine._maybe_record_paused_signal_outcome
    _record_rejected_outcome = LiveTradingEngine._record_rejected_outcome
    _compute_trade_quality_score = LiveTradingEngine._compute_trade_quality_score
    _PAUSED_SIGNAL_OUTCOME_STRATEGIES = LiveTradingEngine._PAUSED_SIGNAL_OUTCOME_STRATEGIES

    def __init__(self, rs_ranks=None, market_direction="BEARISH"):
        self._last_signal_metrics = {}
        self.rs_ranker = SimpleNamespace(get_ranks=AsyncMock(return_value=rs_ranks or []))
        self.regime_detector = SimpleNamespace(
            get_cached_market_direction=AsyncMock(return_value=market_direction),
        )


@pytest.mark.asyncio
async def test_records_a_paused_ema_sell_signal():
    fake = _FakeOutcomeEngine(rs_ranks=[{"symbol": "LTF", "rank": 3}, {"symbol": "OTHER", "rank": 50}])
    strategy = SimpleNamespace(name="ema_crossover_v1")
    market_data = {"close": 1200.0, "rvol": 2.1, "rvol_valid": True, "adx14": 33.3, "adx_valid": True}

    await fake._maybe_record_paused_signal_outcome(strategy, "LTF", "SELL", market_data, "RANGE_BOUND")

    assert len(_FakeRepo.created) == 1
    row = _FakeRepo.created[0]
    assert row["strategy_name"] == "ema_crossover_v1"
    assert row["symbol"] == "LTF"
    assert row["signal"] == "SELL"
    assert row["rejected_at_gate"] == "regime_paused"
    assert row["close_at_rejection"] == 1200.0
    assert row["quality_score"] is not None


@pytest.mark.asyncio
async def test_records_regime_and_market_direction_for_the_range_exception_experiment():
    """2026-10-01: regime alone can't distinguish RANGE_BOUND+bullish from
    RANGE_BOUND+bearish -- market_direction (fetched from the regime
    detector's own cached payload) must be recorded alongside it."""
    fake = _FakeOutcomeEngine(market_direction="BEARISH")
    strategy = SimpleNamespace(name="ema_crossover_v1")
    market_data = {"close": 1200.0}

    await fake._maybe_record_paused_signal_outcome(strategy, "LTF", "SELL", market_data, "RANGE_BOUND")

    row = _FakeRepo.created[0]
    assert row["regime"] == "RANGE_BOUND"
    assert row["market_direction"] == "BEARISH"
    fake.regime_detector.get_cached_market_direction.assert_awaited_once()


@pytest.mark.asyncio
async def test_market_direction_is_none_when_regime_detector_is_unavailable():
    fake = _FakeOutcomeEngine()
    fake.regime_detector = None
    strategy = SimpleNamespace(name="ema_crossover_v1")
    market_data = {"close": 1200.0}

    await fake._maybe_record_paused_signal_outcome(strategy, "LTF", "SELL", market_data, "RANGE_BOUND")

    row = _FakeRepo.created[0]
    assert row["regime"] == "RANGE_BOUND"
    assert row["market_direction"] is None


@pytest.mark.asyncio
async def test_market_direction_fetch_failure_does_not_block_recording():
    fake = _FakeOutcomeEngine()
    fake.regime_detector = SimpleNamespace(
        get_cached_market_direction=AsyncMock(side_effect=ConnectionError("redis down")),
    )
    strategy = SimpleNamespace(name="ema_crossover_v1")
    market_data = {"close": 1200.0}

    await fake._maybe_record_paused_signal_outcome(strategy, "LTF", "SELL", market_data, "RANGE_BOUND")

    row = _FakeRepo.created[0]
    assert row["market_direction"] is None


@pytest.mark.asyncio
async def test_records_a_paused_momentum_signal_too():
    """Unlike the narrow RS-streak shadow trial (ema_crossover_v1 only),
    this covers momentum_v1 as well -- real log data confirmed it also
    produces real confirmed signals while paused."""
    fake = _FakeOutcomeEngine()
    strategy = SimpleNamespace(name="momentum_v1")
    market_data = {"close": 958.2, "rvol": 3.65, "rvol_valid": True, "adx14": 28.0, "adx_valid": True}

    await fake._maybe_record_paused_signal_outcome(strategy, "SBIN", "SELL", market_data, "RANGE_BOUND")

    assert len(_FakeRepo.created) == 1
    assert _FakeRepo.created[0]["strategy_name"] == "momentum_v1"


@pytest.mark.asyncio
async def test_ignores_strategies_outside_the_eligible_set():
    fake = _FakeOutcomeEngine()
    strategy = SimpleNamespace(name="credit_spread_v1")
    market_data = {"close": 100.0, "rvol_valid": False, "adx_valid": False}

    await fake._maybe_record_paused_signal_outcome(strategy, "RELIANCE", "SELL", market_data, "RANGE_BOUND")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_ignores_hold_signal():
    fake = _FakeOutcomeEngine()
    strategy = SimpleNamespace(name="ema_crossover_v1")
    market_data = {"close": 100.0}

    await fake._maybe_record_paused_signal_outcome(strategy, "RELIANCE", "HOLD", market_data, "RANGE_BOUND")
    await fake._maybe_record_paused_signal_outcome(strategy, "RELIANCE", None, market_data, "RANGE_BOUND")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_skips_when_close_is_missing():
    fake = _FakeOutcomeEngine()
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_paused_signal_outcome(strategy, "RELIANCE", "SELL", {}, "RANGE_BOUND")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_adx_and_rvol_fail_closed_to_none_when_invalid():
    """Matches the live engine's own RVOL/ADX validity convention -- an
    unconfirmed reading must score as 'unknown' (quality scorer's neutral
    midpoint), not silently pass through a zero/garbage value."""
    fake = _FakeOutcomeEngine()
    strategy = SimpleNamespace(name="ema_crossover_v1")
    market_data = {"close": 100.0, "rvol": 5.0, "rvol_valid": False, "adx14": 40.0, "adx_valid": False}

    await fake._maybe_record_paused_signal_outcome(strategy, "RELIANCE", "BUY", market_data, "LOW_VOL")

    assert fake._last_signal_metrics["rvol"] is None
    assert fake._last_signal_metrics["adx"] is None


@pytest.mark.asyncio
async def test_swallows_repo_errors_without_raising():
    class _BrokenRepo:
        def __init__(self, model, session):
            pass

        async def create(self, obj_in):
            raise ConnectionError("db down")

    import unittest.mock as _mock
    import src.database.repositories.base as base_mod
    with _mock.patch.object(base_mod, "BaseRepository", _BrokenRepo):
        fake = _FakeOutcomeEngine()
        strategy = SimpleNamespace(name="ema_crossover_v1")
        market_data = {"close": 100.0}
        await fake._maybe_record_paused_signal_outcome(strategy, "RELIANCE", "SELL", market_data, "RANGE_BOUND")


# ── Wiring into _process_signal() ────────────────────────────────────────

class _FakeProcessSignalEngine:
    _audit_gate = LiveTradingEngine._audit_gate
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure
    _maybe_record_shadow_candidate = LiveTradingEngine._maybe_record_shadow_candidate
    _maybe_record_paused_signal_outcome = LiveTradingEngine._maybe_record_paused_signal_outcome
    _record_rejected_outcome = LiveTradingEngine._record_rejected_outcome
    _compute_trade_quality_score = LiveTradingEngine._compute_trade_quality_score
    _SHADOW_ELIGIBLE_STRATEGIES = LiveTradingEngine._SHADOW_ELIGIBLE_STRATEGIES
    _SHADOW_MIN_STREAK_DAYS = LiveTradingEngine._SHADOW_MIN_STREAK_DAYS
    _PAUSED_SIGNAL_OUTCOME_STRATEGIES = LiveTradingEngine._PAUSED_SIGNAL_OUTCOME_STRATEGIES

    def __init__(self):
        self._last_gate_rejection = None
        self._last_signal_metrics = {}
        self._last_signal_date = {}
        self._get_market_data = AsyncMock(return_value={
            "close": 1200.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        })
        self._redis = None
        self.rs_ranker = SimpleNamespace(
            get_sustained_streak=AsyncMock(return_value={}),
            get_ranks=AsyncMock(return_value=[]),
        )
        # If shadow observation ever accidentally continued deeper into the
        # real pipeline, THIS would be called -- asserted never-called below.
        self._close_option_positions = AsyncMock(
            side_effect=AssertionError("paused-signal recording must never reach _close_option_positions()")
        )


@pytest.mark.asyncio
async def test_process_signal_records_paused_outcome_for_momentum_when_regime_paused():
    """momentum_v1 was NOT covered by the narrow RS-streak shadow trial --
    confirm the new mechanism picks it up end-to-end through _process_signal."""
    from src.core.enums import SignalType

    fake = _FakeProcessSignalEngine()
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=False,
        generate_signal=lambda market_data: SignalType.SELL,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="RANGE_BOUND")

    assert len(_FakeRepo.created) == 1
    row = _FakeRepo.created[0]
    assert row["strategy_name"] == "momentum_v1"
    assert row["symbol"] == "SBIN"
    assert row["rejected_at_gate"] == "regime_paused"
    fake._close_option_positions.assert_not_awaited()
