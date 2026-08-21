"""
Regime classification tests.

Covers two related 2026-08-06 fixes:
  - dead-code collapse in _classify() (behavior must be unchanged)
  - the low-VIX/high-ATR% reorder bug: VIX<12 was checked before the ATR%
    trend check, so a low-VIX but genuinely trending market was forced into
    LOW_VOL instead of TRENDING.
"""
from src.market_data.regime_detector import (
    MarketRegimeDetector as MRD,
    REGIME_STRATEGY_MAP,
    STRATEGY_EMA, STRATEGY_MOMENTUM, STRATEGY_SPREAD, STRATEGY_CONDOR,
    ATR_TREND_EXIT_THRESHOLD, ATR_TREND_THRESHOLD,
)


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


def test_low_vol_and_range_bound_allow_identical_strategies():
    # Confirms the reorder fix changes zero premium-seller (credit_spread_v1/
    # iron_condor_v1) behavior -- both regimes must still map to the same set.
    assert set(REGIME_STRATEGY_MAP["LOW_VOL"]) == set(REGIME_STRATEGY_MAP["RANGE_BOUND"])


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
