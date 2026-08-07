"""
EMACrossoverStrategy.generate_signal() regression tests.

Fixed 2026-08-07: found live that ema_crossover_v1 had fired ZERO confirmed
trades in its entire history, despite genuine EMA20/50 crossovers happening
roughly once/day/symbol (verified via a kite.historical_data() replay
cross-referenced against the regime timeline -- dozens of real, well-held
crossovers occurred while the strategy was active, none ever confirmed).

Root cause: prev_fast_ema/prev_slow_ema were overwritten on EVERY call, so
the crossover test ("prev_fast <= prev_slow and fast_ema > slow_ema") could
only ever be True on the single cycle the sign actually flipped -- the very
next cycle it evaluated False (prev now reflected the post-cross state),
which cleared the pending-confirmation counter before a second confirming
bar could ever accumulate. With signal_confirm_bars=2, confirmation was
mathematically impossible.
"""
from src.strategies.ema_crossover import EMACrossoverStrategy


def _bar(symbol, fast, slow, bar_key):
    return {"symbol": symbol, "ema20": fast, "ema50": slow, "ohlc_bar_key": bar_key}


def test_no_signal_on_first_ever_observation():
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    signal = ema.generate_signal(_bar("SBIN", 101.0, 100.0, "bar1"))
    assert signal == "HOLD"
    # No pending count started -- there's no prior state to compare against.
    assert "SBIN" not in ema._pending_signal


def test_no_signal_when_symbol_never_actually_crosses():
    # Fast stays above slow across many cycles -- never a fresh cross, so
    # no pending count should ever start (this was already correct pre-fix).
    ema = EMACrossoverStrategy("ema_test", {})
    ema.initialize()
    for i in range(5):
        signal = ema.generate_signal(_bar("SBIN", 101.0 + i, 100.0, f"bar{i}"))
        assert signal == "HOLD"
    assert "SBIN" not in ema._pending_signal


def test_confirms_buy_after_genuine_crossover_holds_two_distinct_bars():
    """
    The core regression: a real crossover (fast flips from <= slow to >
    slow) that then HOLDS for a second distinct 5-min bar must confirm --
    this is exactly the scenario that was live-broken (91 such cases found
    in production data, zero ever fired).
    """
    ema = EMACrossoverStrategy("ema_test", {"signal_confirm_bars": 2})
    ema.initialize()

    # Bar 1: fast below slow (pre-cross baseline).
    assert ema.generate_signal(_bar("SBIN", 99.0, 100.0, "bar1")) == "HOLD"

    # Bar 2: fresh cross -- fast now above slow. Starts pending count=1.
    assert ema.generate_signal(_bar("SBIN", 100.5, 100.0, "bar2")) == "HOLD"
    assert ema._pending_signal.get("SBIN") == "BUY"
    assert ema._pending_count.get("SBIN") == 1

    # Bar 3 (new distinct bar_key): relationship still holds -- must confirm.
    signal = ema.generate_signal(_bar("SBIN", 101.0, 100.0, "bar3"))
    assert signal == "BUY", (
        "a crossover that holds for 2 distinct bars must confirm -- this is "
        "the exact bug that produced zero live trades ever"
    )
    assert "SBIN" not in ema._pending_signal  # cleared after firing


def test_confirms_sell_after_genuine_crossover_holds_two_distinct_bars():
    ema = EMACrossoverStrategy("ema_test", {"signal_confirm_bars": 2})
    ema.initialize()

    assert ema.generate_signal(_bar("SBIN", 101.0, 100.0, "bar1")) == "HOLD"
    assert ema.generate_signal(_bar("SBIN", 99.5, 100.0, "bar2")) == "HOLD"
    assert ema._pending_signal.get("SBIN") == "SELL"

    signal = ema.generate_signal(_bar("SBIN", 99.0, 100.0, "bar3"))
    assert signal == "SELL"


def test_same_bar_key_does_not_double_count():
    ema = EMACrossoverStrategy("ema_test", {"signal_confirm_bars": 2})
    ema.initialize()

    ema.generate_signal(_bar("SBIN", 99.0, 100.0, "bar1"))
    ema.generate_signal(_bar("SBIN", 100.5, 100.0, "bar2"))
    assert ema._pending_count.get("SBIN") == 1

    # Same bar_key repeated (e.g. two 1-min engine cycles within one 5-min
    # candle) must not bump the confirmation count.
    signal = ema.generate_signal(_bar("SBIN", 100.6, 100.0, "bar2"))
    assert signal == "HOLD"
    assert ema._pending_count.get("SBIN") == 1


def test_reversal_before_confirmation_restarts_pending_in_new_direction():
    ema = EMACrossoverStrategy("ema_test", {"signal_confirm_bars": 2})
    ema.initialize()

    ema.generate_signal(_bar("SBIN", 99.0, 100.0, "bar1"))
    ema.generate_signal(_bar("SBIN", 100.5, 100.0, "bar2"))  # BUY pending, count=1
    assert ema._pending_signal.get("SBIN") == "BUY"

    # Reverses back below slow before confirming -- must restart pending as
    # SELL (fresh cross in the other direction), not silently stay BUY.
    ema.generate_signal(_bar("SBIN", 99.0, 100.0, "bar3"))
    assert ema._pending_signal.get("SBIN") == "SELL"
    assert ema._pending_count.get("SBIN") == 1

    # Confirms SELL after a second distinct bar.
    signal = ema.generate_signal(_bar("SBIN", 98.5, 100.0, "bar4"))
    assert signal == "SELL"


def test_confirm_bars_of_one_fires_on_first_cross_bar():
    ema = EMACrossoverStrategy("ema_test", {"signal_confirm_bars": 1})
    ema.initialize()

    ema.generate_signal(_bar("SBIN", 99.0, 100.0, "bar1"))
    signal = ema.generate_signal(_bar("SBIN", 100.5, 100.0, "bar2"))
    assert signal == "BUY"


def test_per_symbol_state_does_not_leak_across_symbols():
    ema = EMACrossoverStrategy("ema_test", {"signal_confirm_bars": 2})
    ema.initialize()

    ema.generate_signal(_bar("SBIN", 99.0, 100.0, "bar1"))
    ema.generate_signal(_bar("SBIN", 100.5, 100.0, "bar2"))  # SBIN: BUY pending

    # A different symbol starting fresh must not be affected by SBIN's state.
    signal = ema.generate_signal(_bar("TCS", 200.0, 201.0, "bar1"))
    assert signal == "HOLD"
    assert "TCS" not in ema._pending_signal
