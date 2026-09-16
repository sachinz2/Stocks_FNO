"""
Rich per-gate rejection detail (2026-09-16, external review round 2, "make
SignalDecisionTrace more granular"). SignalDecisionTrace's diff-based design
tells you WHICH gate a candidate died at (last_gate_reached) but not the
actual numeric value/threshold -- e.g. "died at mtf_passed", not "MTF spread
0.42% >= 0.30% threshold". self._last_gate_rejection is set by the RVOL/
ADX/RS/MTF threshold checks themselves, immediately before their own
`return` (additive only, no condition or control flow changed), and
consumed by _record_signal_trace() to enrich the persisted `detail` field.

Drives the REAL _process_signal() end to end (not a mocked stand-in) for
each gate, confirming the actual value/threshold captured matches what the
check itself computed -- this is the one part of the pipeline where getting
the real numbers right actually matters, so it's tested against the real
code path, not just _record_signal_trace() in isolation (see
test_signal_decision_trace_2026_09_15.py for that).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.enums import SignalType


class _FakeEngine:
    _audit_gate = LiveTradingEngine._audit_gate
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure

    def __init__(self, market_data, redis=None, rs_ranker=None):
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {}
        self._exited_today = set()
        self._max_daily_orders = 0
        self._max_concurrent_intraday = 999
        self._last_signal_date = {}
        self._signal_gate_stats = {}
        self._last_gate_rejection = None
        self._close_option_positions = AsyncMock()
        self._has_open_option = AsyncMock(return_value=False)
        self._get_market_data = AsyncMock(return_value=market_data)
        self._redis = redis
        self.rs_ranker = rs_ranker


@pytest.mark.asyncio
async def test_rvol_rejection_captures_real_value_and_threshold():
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 0.9, "rvol_valid": True,       # below threshold
        "adx14": 30.0, "adx_valid": True,
    })
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_hard_gate=True, rvol_entry_threshold=1.3,
        generate_signal=lambda market_data: SignalType.SELL,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")

    rej = fake._last_gate_rejection
    assert rej is not None
    assert rej["gate"] == "rvol_passed"
    assert rej["value"] == 0.9
    assert rej["threshold"] == 1.3
    assert rej["reason"] == "RVOL_BELOW_THRESHOLD"


@pytest.mark.asyncio
async def test_adx_rejection_captures_real_value_and_threshold():
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 18.5, "adx_valid": True,   # below the flat 25 threshold
    })
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")

    rej = fake._last_gate_rejection
    assert rej is not None
    assert rej["gate"] == "adx_passed"
    assert rej["value"] == 18.5
    assert rej["threshold"] == 25
    assert rej["reason"] == "ADX_BELOW_THRESHOLD"


class _FakeRedisMtf:
    def __init__(self, tick15_value):
        self._value = tick15_value

    async def get(self, key):
        return self._value


@pytest.mark.asyncio
async def test_mtf_strong_opposition_captures_real_spread_and_threshold():
    # The PDF's own worked example (external review round 2, section 7).
    import json
    tick15 = json.dumps({"ema20": 99.58, "ema50": 100.0})  # bearish 15m, spread=0.42%
    fake = _FakeEngine(
        {
            "close": 1200.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        },
        redis=_FakeRedisMtf(tick15),
    )
    fake._get_lot_size = AsyncMock(return_value=None)  # stop gracefully right after mtf_passed
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True, adx_checked_internally=True,
        require_rs=False, mtf_strict=False, mtf_strong_opposition_pct=0.30,
        generate_signal=lambda market_data: SignalType.BUY,  # 5m bullish, 15m bearish
    )

    await LiveTradingEngine._process_signal(fake, strategy, "PAYTM", vix=15.0, regime="TRENDING")

    rej = fake._last_gate_rejection
    assert rej is not None
    assert rej["gate"] == "mtf_passed"
    assert rej["value"] == pytest.approx(0.42, abs=0.01)
    assert rej["threshold"] == 0.30
    assert rej["reason"] == "MTF_STRONG_OPPOSITION"


@pytest.mark.asyncio
async def test_mtf_strict_disagreement_captures_direction_detail():
    import json
    tick15 = json.dumps({"ema20": 95.0, "ema50": 100.0})  # bearish 15m
    fake = _FakeEngine(
        {
            "close": 1200.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        },
        redis=_FakeRedisMtf(tick15),
    )
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True, adx_checked_internally=True,
        require_rs=False, mtf_strict=True,  # binary
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")

    rej = fake._last_gate_rejection
    assert rej is not None
    assert rej["gate"] == "mtf_passed"
    assert rej["value"] == "bearish"
    assert rej["threshold"] == "BUY"
    assert rej["reason"] == "MTF_STRICT_DISAGREEMENT"


@pytest.mark.asyncio
async def test_rejection_detail_is_cleared_between_calls_on_the_same_engine():
    # A rejection recorded for one symbol must never leak into a later
    # candidate that doesn't hit any gate-failure branch itself.
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 0.9, "rvol_valid": True,
        "adx14": 30.0, "adx_valid": True,
    })
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_hard_gate=True, rvol_entry_threshold=1.3,
        generate_signal=lambda market_data: SignalType.SELL,
    )
    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")
    assert fake._last_gate_rejection is not None  # sanity check the first call did reject

    # Second call: strategy now returns HOLD -- should clear the stale detail.
    strategy2 = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        generate_signal=lambda market_data: SignalType.HOLD,
    )
    await LiveTradingEngine._process_signal(fake, strategy2, "TCS", vix=15.0, regime="TRENDING")

    assert fake._last_gate_rejection is None
