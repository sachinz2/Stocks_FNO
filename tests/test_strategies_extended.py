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


def test_ema_crossover_breakeven_stop_fires_before_trailing_stop_would_have():
    # Mirrors the momentum_v1 fix -- same structural gap, same fix.
    # trailing_stop_pct=0.25 here, so the breakeven-crossing drawdown for a
    # peak of +30% is (100-76.9)/100 = 23.1%, still below the 25% trailing
    # threshold -- the old code would still be holding at exactly breakeven.
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    assert ema.breakeven_activation_pct == 0.15

    entry, peak = 50.0, 65.0  # +30% peak
    pos = {"avg_price": entry, "peak_premium": peak}

    drawdown_at_breakeven = (peak - entry) / peak
    assert drawdown_at_breakeven < ema.trailing_stop_pct, (
        "test invariant: breakeven must be reached before the trailing stop "
        "would have fired, or this test doesn't actually prove the fix"
    )

    assert ema.manage_position(pos, entry) == "EXIT"


def test_ema_crossover_breakeven_stop_not_active_below_activation_threshold():
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    # Peak only +10% above entry -- below the 15% activation floor.
    pos = {"avg_price": 100.0, "peak_premium": 110.0}
    assert ema.manage_position(pos, 100.0) == "HOLD"


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


# ── Breakeven stop (2026-08-13) ──────────────────────────────────────────────
#
# trailing_stop_pct alone is scoped to the PEAK premium, not to locked-in
# profit -- for a moderate peak gain, trailing_stop_pct off that peak can
# land BELOW the entry price, letting a genuinely profitable position
# round-trip all the way into a realized loss before the trailing stop ever
# fires. Confirmed live 2026-08-11: ULTRACEMCO26AUG11920PE (momentum_v1)
# entered at Rs54.04, peaked at Rs71.30 (+31.9%), and only exited once
# already down to Rs47.34 (-Rs335 loss) because 30% off that peak (~Rs49.91)
# was still below entry. These tests reproduce those exact numbers.

def test_momentum_breakeven_stop_fires_at_entry_before_trailing_stop_would_have():
    mom = MomentumStrategy("mom_test", {})
    mom.initialize()
    assert mom.breakeven_activation_pct == 0.15

    entry, peak = 54.04, 71.30  # the real ULTRACEMCO fill/peak
    pos = {"avg_price": entry, "peak_premium": peak}

    # At exactly breakeven, drawdown-from-peak is only ~24.2% -- BELOW the
    # 30% trailing threshold, so the OLD code would still be holding here.
    drawdown_at_breakeven = (peak - entry) / peak
    assert drawdown_at_breakeven < mom.trailing_stop_pct, (
        "test invariant: breakeven must be reached before the trailing stop "
        "would have fired, or this test doesn't actually prove the fix"
    )

    assert mom.manage_position(pos, entry) == "EXIT"


def test_momentum_breakeven_stop_not_active_below_activation_threshold():
    mom = MomentumStrategy("mom_test", {})
    mom.initialize()
    # Peak only +10% above entry -- below the 15% activation floor, so a
    # pullback to entry must NOT trigger the breakeven stop (falls through
    # to the ordinary trailing-stop/ADX rules instead).
    pos = {"avg_price": 100.0, "peak_premium": 110.0, "current_adx": 25.0}
    assert mom.manage_position(pos, 100.0) == "HOLD"


def test_momentum_breakeven_stop_does_not_preempt_large_trailing_stop():
    mom = MomentumStrategy("mom_test", {})
    mom.initialize()
    # A large peak (+50%) means the 30%-off-peak trailing floor (Rs105) sits
    # ABOVE entry (Rs100) -- the ordinary trailing stop protects this case
    # on its own, exiting well before the position could ever reach a loss.
    entry, peak = 100.0, 150.0
    trailing_floor = peak * (1 - mom.trailing_stop_pct)
    assert trailing_floor > entry, "test invariant: trailing floor must be above entry here"

    pos = {"avg_price": entry, "peak_premium": peak}
    assert mom.manage_position(pos, trailing_floor) == "EXIT"
