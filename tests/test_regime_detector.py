"""
Regime classification tests.

Covers two related 2026-08-06 fixes:
  - dead-code collapse in _classify() (behavior must be unchanged)
  - the low-VIX/high-ATR% reorder bug: VIX<12 was checked before the ATR%
    trend check, so a low-VIX but genuinely trending market was forced into
    LOW_VOL instead of TRENDING.
"""
import json

import pytest

from src.market_data.regime_detector import (
    MarketRegimeDetector as MRD,
    REGIME_STRATEGY_MAP,
    STRATEGY_EMA, STRATEGY_MOMENTUM, STRATEGY_SPREAD, STRATEGY_CONDOR,
    ATR_TREND_EXIT_THRESHOLD, ATR_TREND_THRESHOLD,
)
from src.strategies.base import StrategyRegistry, StrategyBase


def test_low_vix_high_atr_classifies_trending():
    # The actual bug: low VIX (<12) but genuinely elevated ATR% must be
    # TRENDING, not forced into LOW_VOL by a VIX-first check ordering.
    assert MRD._classify(vix=11.9, atr_pct=1.68, ema_spread_pct=0.32) == "TRENDING"


def test_low_vix_low_atr_still_low_vol():
    assert MRD._classify(vix=11.9, atr_pct=1.0, ema_spread_pct=0.1) == "LOW_VOL"


def test_normal_vix_high_atr_still_trending():
    assert MRD._classify(vix=15.0, atr_pct=2.0, ema_spread_pct=0.5) == "TRENDING"


def test_normal_vix_low_atr_still_range_bound():
    assert MRD._classify(vix=15.0, atr_pct=1.0, ema_spread_pct=0.1) == "RANGE_BOUND"


def test_high_vix_always_volatile_regardless_of_atr():
    assert MRD._classify(vix=25.0, atr_pct=3.0, ema_spread_pct=1.0) == "VOLATILE"
    assert MRD._classify(vix=25.0, atr_pct=0.5, ema_spread_pct=0.05) == "VOLATILE"


def test_low_vol_excludes_iron_condor_its_own_vix_gate_can_never_pass_there():
    # Fixed 2026-09-03 (live incident): LOW_VOL is defined as vix < 12.0, but
    # iron_condor_v1's own entry gate requires vix >= 12.0 -- mutually
    # exclusive by construction. Confirmed live: 8 days of logs with
    # substantial LOW_VOL time and zero RANGE_BOUND minutes, during which 96%
    # of iron_condor_v1's skip lines were exactly this VIX-too-low block.
    # credit_spread_v1 has the same VIX gate but also runs in TRENDING/
    # VOLATILE, where VIX tends to sit >=12 anyway, so it keeps LOW_VOL as a
    # (largely theoretical, but not self-contradicting) eligible regime.
    assert STRATEGY_CONDOR not in REGIME_STRATEGY_MAP["LOW_VOL"]
    assert STRATEGY_SPREAD in REGIME_STRATEGY_MAP["LOW_VOL"]


def test_range_bound_is_the_only_regime_iron_condor_can_actually_trade_in():
    # RANGE_BOUND (vix >= 12, ATR% low) is the one regime where both the
    # regime gate and iron_condor_v1's own vix_allows_selling() gate can
    # pass simultaneously.
    assert STRATEGY_CONDOR in REGIME_STRATEGY_MAP["RANGE_BOUND"]
    for regime, strategies in REGIME_STRATEGY_MAP.items():
        if regime == "RANGE_BOUND":
            continue
        assert STRATEGY_CONDOR not in strategies, (
            f"iron_condor_v1 should not be regime-eligible in {regime} -- "
            "either its own gates or its defined-risk-in-a-move thesis rules it out"
        )


def test_hysteresis_low_vix_alone_does_not_exit_trending():
    # ATR% between the exit (lower) and enter (higher) thresholds, already
    # TRENDING: a VIX dip alone must NOT kick it back out while ATR% is still
    # above the exit threshold -- this was a real gap (VIX<12 checked before
    # the hysteresis-adjusted ATR% check).
    mid_atr = (ATR_TREND_EXIT_THRESHOLD + ATR_TREND_THRESHOLD) / 2
    assert MRD._classify(vix=11.5, atr_pct=mid_atr, ema_spread_pct=0.3, prev_regime="TRENDING") == "TRENDING"
    # Same ATR%, but from a non-TRENDING state (higher enter threshold applies,
    # no head start) -> not yet TRENDING; low VIX + non-trending -> LOW_VOL.
    assert MRD._classify(vix=11.5, atr_pct=mid_atr, ema_spread_pct=0.3, prev_regime=None) == "LOW_VOL"


def test_volatile_regime_includes_ema_crash_catching_excludes_momentum():
    assert STRATEGY_EMA in REGIME_STRATEGY_MAP["VOLATILE"]
    assert STRATEGY_SPREAD in REGIME_STRATEGY_MAP["VOLATILE"]
    # Iron condor wings still blow through in a panic -- deliberately excluded.
    assert STRATEGY_CONDOR not in REGIME_STRATEGY_MAP["VOLATILE"]
    # Fixed 2026-08-21 (external review, round 2): momentum_v1 was ALSO
    # enabled here until this fix -- removed because a trend-continuation
    # thesis is a specifically bad fit for a regime defined by imminent
    # violent reversal, regardless of the PE-only/tightened-exit guardrails
    # that still justify keeping EMA crossover active. See
    # regime_detector.py's REGIME_STRATEGY_MAP comment for the full reasoning.
    assert STRATEGY_MOMENTUM not in REGIME_STRATEGY_MAP["VOLATILE"]
    assert STRATEGY_MOMENTUM in REGIME_STRATEGY_MAP["TRENDING"]


# ── enforce_regime_switching() vs. StrategyMonitor auto-kill race ──────────
# Live incident, 2026-08-28: enforce_regime_switching()'s resume branch only
# ever checked is_active/should_be_active, with no regard for WHY a strategy
# was currently paused. A StrategyMonitor auto-kill (ema_crossover_v1,
# rolling PF 0.063 -- essentially all losing trades) got undone by this every
# single cycle the regime happened to still allow the strategy, since
# evaluate_all() (which re-pauses it) and enforce_regime_switching() (which
# then immediately resumed it) both run before the entry-signal loop each
# cycle -- confirmed live via logs: pause/resume fired every ~60s for 90+
# consecutive minutes, meaning the strategy was actually active by the time
# new entries were evaluated every single cycle, completely defeating the
# circuit breaker rather than just flapping cosmetically in the health API.

class _FakeStrategy(StrategyBase):
    def initialize(self):
        pass

    def generate_signal(self, data):
        return None

    def manage_position(self, p, c):
        return None

    def shutdown(self):
        pass


class _FakeRedisRegime:
    def __init__(self, regime: str):
        self._regime = regime

    async def get(self, key):
        return json.dumps({"regime": self._regime})


def _register(sid: str, is_active: bool, paused_by=None):
    inst = _FakeStrategy(sid, {})
    inst.is_active = is_active
    inst.paused_by = paused_by
    StrategyRegistry._active_instances[sid] = inst
    return inst


@pytest.mark.asyncio
async def test_regime_switching_resumes_a_strategy_it_paused_for_regime_reasons():
    sid = "ema_crossover_v1"
    _register(sid, is_active=False, paused_by="regime")
    try:
        detector = MRD(_FakeRedisRegime("TRENDING"))
        await detector.enforce_regime_switching()
        assert StrategyRegistry._active_instances[sid].is_active is True
    finally:
        del StrategyRegistry._active_instances[sid]


@pytest.mark.asyncio
async def test_regime_switching_does_not_resume_a_monitor_paused_strategy():
    """The exact live bug: a strategy paused by StrategyMonitor for real
    poor performance must NOT be resumed just because the regime allows it."""
    sid = "ema_crossover_v1"
    _register(sid, is_active=False, paused_by="monitor")
    try:
        detector = MRD(_FakeRedisRegime("TRENDING"))
        await detector.enforce_regime_switching()
        assert StrategyRegistry._active_instances[sid].is_active is False, (
            "a performance-based auto-kill must survive a regime that would "
            "otherwise allow the strategy -- it needs an explicit manual resume"
        )
    finally:
        del StrategyRegistry._active_instances[sid]


@pytest.mark.asyncio
async def test_regime_switching_does_not_resume_a_manually_paused_strategy():
    sid = "ema_crossover_v1"
    _register(sid, is_active=False, paused_by="manual")
    try:
        detector = MRD(_FakeRedisRegime("TRENDING"))
        await detector.enforce_regime_switching()
        assert StrategyRegistry._active_instances[sid].is_active is False
    finally:
        del StrategyRegistry._active_instances[sid]


@pytest.mark.asyncio
async def test_regime_switching_still_pauses_a_regime_ineligible_strategy():
    """Guard against over-fixing -- the PAUSE side (unaffected by this fix)
    must still work regardless of paused_by."""
    sid = "iron_condor_v1"
    _register(sid, is_active=True, paused_by=None)
    try:
        detector = MRD(_FakeRedisRegime("TRENDING"))  # excludes iron_condor_v1
        await detector.enforce_regime_switching()
        inst = StrategyRegistry._active_instances[sid]
        assert inst.is_active is False
        assert inst.paused_by == "regime"
    finally:
        del StrategyRegistry._active_instances[sid]


def test_pause_strategy_records_source_and_defaults_to_manual():
    sid = "credit_spread_v1"
    _register(sid, is_active=True)
    try:
        StrategyRegistry.pause_strategy(sid, reason="test")
        assert StrategyRegistry._active_instances[sid].paused_by == "manual"
    finally:
        del StrategyRegistry._active_instances[sid]


def test_resume_strategy_clears_paused_by():
    sid = "momentum_v1"
    _register(sid, is_active=False, paused_by="monitor")
    try:
        StrategyRegistry.resume_strategy(sid)
        inst = StrategyRegistry._active_instances[sid]
        assert inst.is_active is True
        assert inst.paused_by is None
    finally:
        del StrategyRegistry._active_instances[sid]


# ── _get_vix(): no more ATR%-as-VIX fallback ────────────────────────────────
# Live incident, 2026-08-28: confirmed via production logs that real VIX
# (~11 for most of the session) went stale in Redis for an extended stretch,
# and _get_vix() silently substituted the day's ATR% (~1.5-2.0) as if it
# WERE the VIX, based on a "roughly 1:1 comparable" assumption the same
# day's real data disproves outright. VIX is an annualized volatility
# measure; a raw daily ATR% is not comparable on any simple 1:1 basis.

class _FakeRedisGetOnly:
    def __init__(self, values: dict):
        self._values = values

    async def get(self, key):
        return self._values.get(key)


@pytest.mark.asyncio
async def test_get_vix_returns_real_cached_value_when_present():
    detector = MRD(_FakeRedisGetOnly({"market:india_vix": "11.2"}))
    assert await detector._get_vix() == 11.2


@pytest.mark.asyncio
async def test_get_vix_falls_back_to_15_not_atr_pct_when_cache_empty():
    """The exact live scenario: VIX cache empty, but market:trend_stats has
    a real, very different ATR% (1.8) sitting right there -- must NOT be
    used as a VIX substitute."""
    detector = MRD(_FakeRedisGetOnly({
        "market:trend_stats": json.dumps({"n_symbols": 132, "avg_atr_pct_daily": 1.8}),
    }))
    assert await detector._get_vix() == 15.0


@pytest.mark.asyncio
async def test_get_vix_falls_back_to_15_when_nothing_available():
    detector = MRD(_FakeRedisGetOnly({}))
    assert await detector._get_vix() == 15.0


@pytest.mark.asyncio
async def test_get_vix_fallback_logs_a_warning(caplog):
    detector = MRD(_FakeRedisGetOnly({}))
    with caplog.at_level("WARNING"):
        await detector._get_vix()
    assert any("India VIX unavailable" in r.message for r in caplog.records)
