"""
Live incident, 2026-09-04 (thorough cross-check of all strategies):
ema_crossover_v1's and momentum_v1's ADX-rising history builder appended
adx14 into _adx_history unconditionally, without checking adx_valid.
ltp_poller.py emits adx14=0.0 (not None) paired with adx_valid=False as an
"insufficient history" sentinel for roughly the first 14 bars of a symbol's
life (or after data gaps) -- same convention as RVOL's rvol_valid. Appending
that sentinel let a later genuinely-computed-but-still-low ADX reading pass
the "ADX rising" fallback (adx > hist_adx[-2]) purely because it was being
compared against an injected 0.0, not because ADX actually rose.
"""
from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.momentum import MomentumStrategy


def _ema(**overrides):
    strat = EMACrossoverStrategy("ema_test", overrides)
    strat.initialize()
    return strat


def _ema_bar(fast=101.0, slow=100.0, adx=20.0, adx_valid=True, bar_key="live:t0"):
    return {
        "symbol": "RELIANCE", "ema20": fast, "ema50": slow, "adx14": adx,
        "adx_valid": adx_valid, "ohlc_bar_key": bar_key,
    }


def test_ema_crossover_invalid_adx_sentinel_does_not_enter_history():
    strat = _ema()
    strat.generate_signal(_ema_bar(adx=0.0, adx_valid=False, bar_key="live:t0"))
    assert strat._adx_history.get("RELIANCE") in (None, [])


def test_ema_crossover_invalid_sentinel_cannot_fake_a_rising_adx():
    """The exact bug: two invalid 0.0 sentinel bars followed by a genuinely
    low (but valid) ADX reading must NOT look like "ADX rising" just because
    12.0 > 0.0 -- there must be no real history to compare against yet."""
    strat = _ema(signal_confirm_bars=1, adx_entry_threshold=22)
    strat.generate_signal(_ema_bar(fast=99.0, slow=100.0, adx=0.0, adx_valid=False, bar_key="live:t0"))
    strat.generate_signal(_ema_bar(fast=99.5, slow=100.0, adx=0.0, adx_valid=False, bar_key="live:t1"))
    # Cross confirms with a genuinely low, VALID ADX=12.0 (< threshold 22,
    # and no real prior history exists to compare against for "rising").
    signal = strat.generate_signal(_ema_bar(fast=101.0, slow=100.0, adx=12.0, adx_valid=True, bar_key="live:t2"))
    assert signal == "HOLD", (
        "a low, valid ADX with no real rising-history must not fire just "
        "because sentinel 0.0 values were sitting in history"
    )


def _mom(**overrides):
    strat = MomentumStrategy("mom_test", overrides)
    strat.initialize()
    return strat


def _mom_bar(ema20=105.0, ema50=100.0, adx=30.0, adx_valid=True, bar_key="live:t0"):
    return {
        "symbol": "RELIANCE", "ema20": ema20, "ema50": ema50, "adx14": adx,
        "adx_valid": adx_valid, "close": 2500.0, "atr14": 20.0, "vwap": 2500.0,
        "ohlc_bar_key": bar_key,
    }


def test_momentum_invalid_adx_sentinel_does_not_enter_history():
    strat = _mom(use_pullback_continuation_model=False)
    strat.generate_signal(_mom_bar(adx=0.0, adx_valid=False, bar_key="live:t0"))
    assert strat._adx_history.get("RELIANCE") in (None, [])


def test_momentum_ema_history_still_advances_even_when_adx_is_invalid():
    """Guard against over-fixing: _ema_history (unrelated to adx_valid) must
    keep accumulating even on a bar with an invalid ADX sentinel."""
    strat = _mom(use_pullback_continuation_model=False)
    strat.generate_signal(_mom_bar(ema20=101.0, adx=0.0, adx_valid=False, bar_key="live:t0"))
    assert strat._ema_history.get("RELIANCE") == [101.0]
