"""
Strategy-level behavioral tests not covered by tests/test_strategy.py:
  - MomentumStrategy.on_pause() clears pending-confirmation state
  - VOLATILE crash-catching: EMACrossoverStrategy's EMA-reversal exit and
    MomentumStrategy's tightened ADX-exit threshold, both scoped ONLY to
    positions entered while regime==VOLATILE
"""
from src.strategies.momentum import MomentumStrategy
from src.strategies.ema_crossover import EMACrossoverStrategy


def test_momentum_on_pause_clears_pending_state():
    mom = MomentumStrategy("momentum_v1_test", {})
    mom.initialize()
    mom._pending_signal["SBIN"] = "BUY"
    mom._pending_count["SBIN"] = 1
    mom._pending_bar_key["SBIN"] = "live:2026-07-30T09:15:00"

    mom.on_pause()

    assert mom._pending_signal == {}
    assert mom._pending_count == {}
    assert mom._pending_bar_key == {}


def test_ema_crossover_ce_exits_on_bearish_flip_when_volatile_entered():
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    pos = {
        "avg_price": 50.0, "peak_premium": 55.0,
        "current_ema_fast": 99.0, "current_ema_slow": 100.0,  # fast <= slow: reversed for a CE
        "is_call": True, "entry_regime": "VOLATILE",
    }
    assert ema.manage_position(pos, 52.0) == "EXIT"


def test_ema_crossover_pe_exits_on_bullish_flip_when_volatile_entered():
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    pos = {
        "avg_price": 50.0, "peak_premium": 55.0,
        "current_ema_fast": 101.0, "current_ema_slow": 100.0,  # fast >= slow: reversed for a PE
        "is_call": False, "entry_regime": "VOLATILE",
    }
    assert ema.manage_position(pos, 52.0) == "EXIT"


def test_ema_crossover_holds_while_relationship_intact():
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    pos = {
        "avg_price": 50.0, "peak_premium": 52.0,
        "current_ema_fast": 95.0, "current_ema_slow": 100.0,  # still bearish, matches PE direction
        "is_call": False, "entry_regime": "VOLATILE",
    }
    assert ema.manage_position(pos, 51.0) == "HOLD"


def test_ema_crossover_reversal_exit_scoped_only_to_volatile_entries():
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    # Same "reversed" EMA data, entry_regime is NOT VOLATILE -> must not exit.
    pos = {
        "avg_price": 50.0, "peak_premium": 52.0,
        "current_ema_fast": 99.0, "current_ema_slow": 100.0,
        "is_call": True, "entry_regime": "TRENDING",
    }
    assert ema.manage_position(pos, 51.0) == "HOLD"


def test_ema_crossover_missing_entry_regime_is_safe_noop():
    # Legacy/older journal entries without the entry_regime field must not crash.
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    pos = {
        "avg_price": 50.0, "peak_premium": 52.0,
        "current_ema_fast": 99.0, "current_ema_slow": 100.0,
        "is_call": True,
    }
    assert ema.manage_position(pos, 51.0) == "HOLD"


def test_momentum_uses_tighter_adx_exit_threshold_only_when_volatile_entered():
    mom = MomentumStrategy("mom_test", {})
    mom.initialize()
    assert mom.adx_exit_threshold == 22
    assert mom.adx_exit_threshold_volatile == 30

    # ADX=25: below the VOLATILE threshold (30) but above the normal one (22).
    pos_volatile = {"avg_price": 50.0, "peak_premium": 52.0, "current_adx": 25.0, "entry_regime": "VOLATILE"}
    assert mom.manage_position(pos_volatile, 51.0) == "EXIT"

    pos_normal = {"avg_price": 50.0, "peak_premium": 52.0, "current_adx": 25.0, "entry_regime": "TRENDING"}
    assert mom.manage_position(pos_normal, 51.0) == "HOLD"
