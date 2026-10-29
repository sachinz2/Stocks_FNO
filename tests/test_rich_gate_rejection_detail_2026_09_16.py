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
import datetime as _dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.enums import SignalType


@pytest.fixture(autouse=True)
def _pin_clock_outside_the_hour13_exclusion(monkeypatch):
    # EXPERIMENT (2026-10-15, data-driven): _process_signal() now blocks
    # ema_crossover_v1 entries during hour=13 IST. Every test in this file
    # drives the REAL _process_signal() with strategy.name="ema_crossover_v1"
    # -- without pinning the clock, these would spuriously pass/fail
    # depending on the real wall-clock hour the suite happens to run at,
    # which has nothing to do with what this file actually tests (gate
    # rejection detail, not time-of-day filtering).
    import src.live_trading.live_trading_engine as lte_module
    monkeypatch.setattr(lte_module, "now_ist", lambda: _dt.datetime(2026, 10, 15, 10, 0, 0))


class _FakeEngine:
    _audit_gate = LiveTradingEngine._audit_gate
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure
    _ELEVATED_VIX_ENTRY_CEILING = LiveTradingEngine._ELEVATED_VIX_ENTRY_CEILING

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


# ── hour=13 entry exclusion (ema_crossover_v1 2026-10-15, momentum_v1 ───────
# 2026-10-19) -- both data-driven, confirmed independently for each strategy
# via its own trade review (see matching comment in _process_signal()).

@pytest.mark.asyncio
async def test_hour13_blocks_ema_crossover_v1_entries(monkeypatch):
    import datetime as _dt
    import src.live_trading.live_trading_engine as lte_module
    monkeypatch.setattr(lte_module, "now_ist", lambda: _dt.datetime(2026, 10, 19, 13, 30, 0))
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 30.0, "adx_valid": True,
    })
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")

    # Blocked before the ADX gate even runs -- only signal_generated is audited.
    assert fake._signal_gate_stats.get("ema_crossover_v1") == {"signal_generated": 1}
    assert fake._last_gate_rejection is None


@pytest.mark.asyncio
async def test_hour13_blocks_momentum_v1_entries(monkeypatch):
    import datetime as _dt
    import src.live_trading.live_trading_engine as lte_module
    monkeypatch.setattr(lte_module, "now_ist", lambda: _dt.datetime(2026, 10, 19, 13, 45, 0))
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 30.0, "adx_valid": True,
    })
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, min_dte=0, max_dte=999,
        generate_signal=lambda market_data: SignalType.SELL,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")

    assert fake._signal_gate_stats.get("momentum_v1") == {"signal_generated": 1}
    assert fake._last_gate_rejection is None


@pytest.mark.asyncio
async def test_hour10_does_not_block_ema_crossover_v1_or_momentum_v1(monkeypatch):
    """Confirms the exclusion is hour-specific, not a blanket block on either
    strategy -- both must still reach the ADX gate normally outside hour=13."""
    import datetime as _dt
    import src.live_trading.live_trading_engine as lte_module
    monkeypatch.setattr(lte_module, "now_ist", lambda: _dt.datetime(2026, 10, 19, 10, 0, 0))

    for name in ("ema_crossover_v1", "momentum_v1"):
        fake = _FakeEngine({
            "close": 1200.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 18.5, "adx_valid": True,   # below the flat 25 threshold -> rejected there
        })
        strategy = SimpleNamespace(
            name=name, is_active=True, min_dte=0, max_dte=999,
            rvol_checked_internally=True,
            generate_signal=lambda market_data: SignalType.BUY,
        )

        await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")

        rej = fake._last_gate_rejection
        assert rej is not None, f"{name} was unexpectedly blocked before reaching the ADX gate"
        assert rej["gate"] == "adx_passed"
        assert rej["reason"] == "ADX_BELOW_THRESHOLD"


# ── Elevated-VIX entry ceiling (2026-10-29, data-driven) ────────────────────
# 15-day review (Oct 13-28) found the same break point independently in
# ema_crossover_v1 and iron_condor_v1: both strategies deteriorated sharply
# once VIX crossed ~16, concentrating nearly all of that window's losses.
# Scoped to ema_crossover_v1 only here (momentum_v1 showed 0% win in EVERY
# VIX bucket that same window, so a ceiling wouldn't have helped it).

@pytest.mark.asyncio
async def test_vix_ceiling_blocks_ema_crossover_v1_entries():
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 30.0, "adx_valid": True,
    })
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=16.0, regime="TRENDING")

    # Blocked before the ADX gate even runs -- only signal_generated is audited.
    assert fake._signal_gate_stats.get("ema_crossover_v1") == {"signal_generated": 1}
    assert fake._last_gate_rejection is None


@pytest.mark.asyncio
async def test_vix_just_under_the_ceiling_does_not_block_ema_crossover_v1():
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 18.5, "adx_valid": True,   # below the flat 25 threshold -> rejected there
    })
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.9, regime="TRENDING")

    rej = fake._last_gate_rejection
    assert rej is not None, "unexpectedly blocked by the VIX ceiling just under its threshold"
    assert rej["gate"] == "adx_passed"


@pytest.mark.asyncio
async def test_vix_ceiling_does_not_block_momentum_v1():
    """Confirms the ceiling is ema_crossover_v1-specific -- momentum_v1
    showed 0% win at every VIX bucket in the same window, so there's no
    data to justify blocking it here too."""
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 18.5, "adx_valid": True,
    })
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=20.0, regime="TRENDING")

    rej = fake._last_gate_rejection
    assert rej is not None, "momentum_v1 should not be affected by the ema_crossover_v1-only VIX ceiling"
    assert rej["gate"] == "adx_passed"


@pytest.mark.asyncio
async def test_vix_ceiling_fails_open_on_missing_vix():
    """A VIX-fetch outage must not silently halt every ema_crossover_v1
    entry -- unlike credit_spread_v1/iron_condor_v1's premium-selling
    thesis, this strategy has no fundamental dependency on VIX."""
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 18.5, "adx_valid": True,
    })
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=None, regime="TRENDING")

    rej = fake._last_gate_rejection
    assert rej is not None, "vix=None should fail open, not block the entry"
    assert rej["gate"] == "adx_passed"


# ── Same-day re-entry block (2026-10-22, data-driven) ───────────────────────
# _exited_today already existed ("adverse exits today -- blocks same-day
# re-entry") and was already checked by _process_credit_spread()/
# _process_iron_condor(), but was never wired up for the single-leg
# strategies sharing _process_signal(). Quick same-symbol re-entries were
# found to perform meaningfully worse than each strategy's own baseline
# (momentum_v1: 0% win rate on 6 instances; ema_crossover_v1: 25.0% win
# rate on 20, vs 34.3% overall).

@pytest.mark.asyncio
async def test_single_leg_entry_blocked_when_symbol_already_exited_today():
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 30.0, "adx_valid": True,
    })
    fake._exited_today = {"SBIN"}
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, min_dte=0, max_dte=999,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")

    # Blocked before even the cross-strategy collision-guard's next step
    # (_close_option_positions) runs.
    fake._close_option_positions.assert_not_awaited()
    assert fake._signal_gate_stats.get("momentum_v1") == {"signal_generated": 1}


@pytest.mark.asyncio
async def test_single_leg_entry_not_blocked_for_a_different_symbol():
    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 18.5, "adx_valid": True,   # below the flat 25 threshold -> rejected there, proving we got past the re-entry check
    })
    fake._exited_today = {"TCS"}
    strategy = SimpleNamespace(
        name="momentum_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")

    rej = fake._last_gate_rejection
    assert rej is not None, "SBIN was unexpectedly blocked by TCS's re-entry entry"
    assert rej["gate"] == "adx_passed"


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
async def test_rvol_rejection_detail_actually_reaches_the_persisted_trace_row():
    """Fixed 2026-10-01: this file previously only asserted
    fake._last_gate_rejection was set correctly, never that
    _record_signal_trace() actually persists it -- the one place a real
    end-to-end bug was hiding (the gate-name match in
    _record_signal_trace() could never succeed, since last_gate_reached is
    always the last-PASSED gate while _last_gate_rejection["gate"] is
    always the gate that just FAILED). Drives the real _process_signal()
    AND the real _record_signal_trace() together, same as live traffic
    does each cycle, to close that gap."""
    from src.database.models.signal_decision_trace import SignalDecisionTrace
    from src.database.repositories.base import BaseRepository

    class _FakeRepo:
        created = []

        def __init__(self, model, session):
            pass

        async def create(self, obj_in):
            _FakeRepo.created.append(obj_in)
            return obj_in

    fake = _FakeEngine({
        "close": 1200.0, "ltp_source": "live_tick",
        "rvol": 0.9, "rvol_valid": True,       # below threshold
        "adx14": 30.0, "adx_valid": True,
    })
    fake._record_signal_trace = LiveTradingEngine._record_signal_trace.__get__(fake)
    fake._classify_signal_error = staticmethod(LiveTradingEngine._classify_signal_error)
    fake._compute_trade_quality_score = LiveTradingEngine._compute_trade_quality_score.__get__(fake)
    fake._record_rejected_outcome = LiveTradingEngine._record_rejected_outcome.__get__(fake)
    fake._last_signal_metrics = {}
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_hard_gate=True, rvol_entry_threshold=1.3,
        generate_signal=lambda market_data: SignalType.SELL,
    )

    import unittest.mock as _mock
    import src.database.repositories.base as base_mod
    with _mock.patch.object(base_mod, "BaseRepository", _FakeRepo):
        _FakeRepo.created = []
        gates_before = dict(fake._signal_gate_stats.get("ema_crossover_v1", {}))
        await LiveTradingEngine._process_signal(fake, strategy, "SBIN", vix=15.0, regime="TRENDING")
        await fake._record_signal_trace("ema_crossover_v1", "SBIN", "TRENDING", gates_before)

    trace_rows = [r for r in _FakeRepo.created if "last_gate_reached" in r]
    assert len(trace_rows) == 1
    row = trace_rows[0]
    assert row["final_decision"] == "REJECTED"
    assert row["detail"] == "RVOL_BELOW_THRESHOLD value=0.9 threshold=1.3"


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
