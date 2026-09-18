"""
_maybe_record_shadow_candidate() / ShadowSignalObservation (2026-09-18,
user-authorized: "safest place to loosen the gates" -> observe-only live
trial before any real capital risk).

Pure observability: records a regime-paused, shadow-eligible strategy's
real BUY/SELL signal on a symbol holding a sustained RS-rank streak --
"would have been a real candidate if the regime gate weren't excluding this
strategy right now." No order is ever placed. Critically, this hook sits
BEFORE _close_option_positions() (a real, order-placing reversal-exit call
reached later in _process_signal's pipeline) -- these tests confirm the
hook fires and returns without ever reaching further into the pipeline.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.enums import SignalType


# ── _maybe_record_shadow_candidate() -- direct unit tests ───────────────────

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


class _FakeShadowEngine:
    _maybe_record_shadow_candidate = LiveTradingEngine._maybe_record_shadow_candidate
    _SHADOW_ELIGIBLE_STRATEGIES = LiveTradingEngine._SHADOW_ELIGIBLE_STRATEGIES
    _SHADOW_MIN_STREAK_DAYS = LiveTradingEngine._SHADOW_MIN_STREAK_DAYS

    def __init__(self, streaks):
        self.rs_ranker = SimpleNamespace(get_sustained_streak=AsyncMock(return_value=streaks))


@pytest.mark.asyncio
async def test_records_a_buy_candidate_on_a_sustained_top_streak():
    fake = _FakeShadowEngine({"PAYTM": {"side": "top", "count": 7}})
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_shadow_candidate(strategy, "PAYTM", SignalType.BUY, "RANGE_BOUND")

    assert len(_FakeRepo.created) == 1
    row = _FakeRepo.created[0]
    assert row["strategy_name"] == "ema_crossover_v1"
    assert row["symbol"] == "PAYTM"
    assert row["signal"] == "BUY"
    assert row["regime"] == "RANGE_BOUND"
    assert row["rs_streak_days"] == 7
    assert row["rs_streak_side"] == "top"


@pytest.mark.asyncio
async def test_records_a_sell_candidate_on_a_sustained_bottom_streak():
    fake = _FakeShadowEngine({"IDEA": {"side": "bottom", "count": 5}})
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_shadow_candidate(strategy, "IDEA", SignalType.SELL, "LOW_VOL")

    assert len(_FakeRepo.created) == 1
    assert _FakeRepo.created[0]["signal"] == "SELL"


@pytest.mark.asyncio
async def test_ignores_strategies_outside_the_shadow_eligible_set():
    fake = _FakeShadowEngine({"PAYTM": {"side": "top", "count": 7}})
    strategy = SimpleNamespace(name="momentum_v1")  # not in _SHADOW_ELIGIBLE_STRATEGIES

    await fake._maybe_record_shadow_candidate(strategy, "PAYTM", SignalType.BUY, "TRENDING")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_ignores_hold_signals():
    fake = _FakeShadowEngine({"PAYTM": {"side": "top", "count": 7}})
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_shadow_candidate(strategy, "PAYTM", SignalType.HOLD, "RANGE_BOUND")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_ignores_a_symbol_with_no_streak_at_all():
    fake = _FakeShadowEngine({})
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_shadow_candidate(strategy, "RANDOMSTOCK", SignalType.BUY, "LOW_VOL")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_ignores_a_streak_below_the_minimum_days():
    fake = _FakeShadowEngine({"PAYTM": {"side": "top", "count": 2}})  # below default floor of 3
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_shadow_candidate(strategy, "PAYTM", SignalType.BUY, "RANGE_BOUND")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_ignores_a_buy_signal_on_a_bottom_streak_wrong_side():
    # A BUY needs sustained RELATIVE STRENGTH (top), not weakness -- a
    # stock streaking bottom isn't the PAYTM-shaped case this exists for.
    fake = _FakeShadowEngine({"IDEA": {"side": "bottom", "count": 7}})
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_shadow_candidate(strategy, "IDEA", SignalType.BUY, "RANGE_BOUND")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_ignores_a_sell_signal_on_a_top_streak_wrong_side():
    fake = _FakeShadowEngine({"PAYTM": {"side": "top", "count": 7}})
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_shadow_candidate(strategy, "PAYTM", SignalType.SELL, "RANGE_BOUND")

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_no_rs_ranker_attached_is_a_safe_no_op():
    fake = SimpleNamespace(rs_ranker=None)
    fake._maybe_record_shadow_candidate = LiveTradingEngine._maybe_record_shadow_candidate.__get__(fake)
    fake._SHADOW_ELIGIBLE_STRATEGIES = LiveTradingEngine._SHADOW_ELIGIBLE_STRATEGIES
    fake._SHADOW_MIN_STREAK_DAYS = LiveTradingEngine._SHADOW_MIN_STREAK_DAYS
    strategy = SimpleNamespace(name="ema_crossover_v1")

    await fake._maybe_record_shadow_candidate(strategy, "PAYTM", SignalType.BUY, "RANGE_BOUND")  # must not raise

    assert _FakeRepo.created == []


@pytest.mark.asyncio
async def test_swallows_repo_errors_without_raising():
    class _BrokenRepo:
        def __init__(self, model, session):
            pass

        async def create(self, obj_in):
            raise ConnectionError("db down")

    import src.database.repositories.base as base_mod
    import unittest.mock as _mock
    with _mock.patch.object(base_mod, "BaseRepository", _BrokenRepo):
        fake = _FakeShadowEngine({"PAYTM": {"side": "top", "count": 7}})
        strategy = SimpleNamespace(name="ema_crossover_v1")
        await fake._maybe_record_shadow_candidate(strategy, "PAYTM", SignalType.BUY, "RANGE_BOUND")  # must not raise


# ── Wiring into _process_signal(): fires exactly when paused, never touches
# real trading state ─────────────────────────────────────────────────────────

class _FakeProcessSignalEngine:
    _audit_gate = LiveTradingEngine._audit_gate
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure
    _maybe_record_shadow_candidate = LiveTradingEngine._maybe_record_shadow_candidate
    _SHADOW_ELIGIBLE_STRATEGIES = LiveTradingEngine._SHADOW_ELIGIBLE_STRATEGIES
    _SHADOW_MIN_STREAK_DAYS = LiveTradingEngine._SHADOW_MIN_STREAK_DAYS

    def __init__(self, streaks):
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
        # If shadow observation ever accidentally continued deeper into the
        # real pipeline, THIS would be called -- asserted never-called below.
        self._close_option_positions = AsyncMock(
            side_effect=AssertionError("shadow observation must never reach _close_option_positions()")
        )
        self._has_open_option = AsyncMock(return_value=False)
        self._get_market_data = AsyncMock(return_value={
            "close": 1200.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        })
        self._redis = None
        self.rs_ranker = SimpleNamespace(get_sustained_streak=AsyncMock(return_value=streaks))


@pytest.mark.asyncio
async def test_process_signal_records_shadow_candidate_when_regime_paused():
    fake = _FakeProcessSignalEngine({"PAYTM": {"side": "top", "count": 7}})
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=False,  # regime-paused
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "PAYTM", vix=15.0, regime="RANGE_BOUND")

    assert len(_FakeRepo.created) == 1
    assert _FakeRepo.created[0]["symbol"] == "PAYTM"
    fake._close_option_positions.assert_not_awaited()  # never reached the real pipeline
    assert "signal_generated" not in fake._signal_gate_stats.get("ema_crossover_v1", {})  # no real gate progress


@pytest.mark.asyncio
async def test_process_signal_does_not_shadow_record_when_strategy_is_genuinely_active():
    # An active strategy proceeds through the REAL pipeline instead --
    # shadow recording is specifically for the paused case. Unlike the
    # shadow test above, reaching _close_option_positions() here is the
    # CORRECT, expected real-pipeline behavior, not a violation -- use a
    # normal permissive mock, not the assert-never-called one.
    fake = _FakeProcessSignalEngine({"PAYTM": {"side": "top", "count": 7}})
    fake._close_option_positions = AsyncMock()
    fake._get_lot_size = AsyncMock(return_value=None)  # stop gracefully, not under test here
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True, adx_checked_internally=True,
        require_rs=False, mtf_strict=False,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "PAYTM", vix=15.0, regime="TRENDING")

    assert _FakeRepo.created == []  # this is a real candidate, not a shadow one
