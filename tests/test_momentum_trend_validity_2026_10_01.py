"""
momentum_v1's pullback/breakout state machine (2026-10-01, external review,
confirmed against the actual code).

_pullback_continuation_signal() used to fully reset ESTABLISHED/PULLBACK
tracking (_trend_state, _trend_direction, _pullback_ref) the instant `raw`
went None -- and `raw` requires ADX to be higher than the immediately
preceding bar on EVERY bar it's checked (adx_rising_required), not just the
first. A perfectly good, still-strong established trend (e.g. ADX
50->49->48, still far above any reasonable "trend is over" bar) got its
tracked state wiped the moment ADX merely stopped increasing -- destroying
_pullback_ref, the exact reference level a later breakout needs to break
out from. This is the same class of bug already fixed for
extension_atr_mult/vwap_extension_pct on 2026-08-27 (see
_pullback_continuation_signal()'s docstring), just via a different pair of
gates.

Fix: trend_still_valid (direction + the looser adx_exit_threshold, not
requiring ADX to keep rising) now decides whether to MAINTAIN
already-tracked state; `raw` (the stricter check) stays reserved for
starting a NEW setup.
"""
from src.strategies.momentum import MomentumStrategy


def _mom(**overrides):
    strat = MomentumStrategy("momentum_v1", overrides)
    strat.initialize()
    return strat


def _bar(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx=30.0, close=110.0,
         atr=2.0, vwap=110.0, rvol=1.5, rvol_valid=True, bar_key="live:t0"):
    return {
        "symbol": symbol, "ema20": ema20, "ema50": ema50, "adx14": adx,
        "adx_valid": True,  # this test suite exercises adx_rising_required=True,
                             # which reads real history -- needs adx_valid set,
                             # unlike other test files that disable that check entirely.
        "close": close, "atr14": atr, "vwap": vwap, "rvol": rvol,
        "rvol_valid": rvol_valid,
        "rvol_closed_bar": rvol, "rvol_closed_bar_valid": rvol_valid,
        "ohlc_bar_key": bar_key,
    }


def _established_strategy():
    """Get a BUY trend ESTABLISHED via genuinely rising ADX (adx_rising_required
    stays at its real default, True -- unlike other test files, this one
    deliberately does NOT disable it, since that's the exact behavior under test)."""
    strat = _mom(ema_slope_required=False, extension_atr_mult=0, vwap_extension_pct=0)
    symbol = "RELIANCE"
    # Bar 0: below adx_entry_threshold -- pure history warmup, raw stays None
    # regardless (adx_rising_required needs >=2 bars of history to even evaluate).
    strat.generate_signal(_bar(symbol, adx=20.0, close=108.0, bar_key="live:t0"))
    # Bar 1: qualifies (adx rising vs bar 0) -- seeds ESTABLISHED.
    strat.generate_signal(_bar(symbol, adx=28.0, close=109.0, bar_key="live:t1"))
    assert strat._trend_state.get(symbol) == "ESTABLISHED"
    # Bar 2: still rising, extends further -- _pullback_ref advances.
    strat.generate_signal(_bar(symbol, adx=35.0, close=112.0, bar_key="live:t2"))
    assert strat._trend_state.get(symbol) == "ESTABLISHED"
    assert strat._pullback_ref.get(symbol) == 112.0
    return strat, symbol


def test_adx_dip_above_exit_threshold_does_not_wipe_established_state():
    strat, symbol = _established_strategy()
    ref_before = strat._pullback_ref[symbol]

    # Bar 3: ADX dips from 35 -> 34 (fails adx_rising_required: 34 < 35) but
    # is still well above adx_exit_threshold (22 default) -- the trend
    # itself hasn't ended. Also pulls back slightly (doesn't extend past ref).
    signal = strat.generate_signal(_bar(symbol, adx=34.0, close=111.0, bar_key="live:t3"))

    assert signal == "HOLD"
    assert symbol in strat._trend_state, "ESTABLISHED/PULLBACK tracking must survive a mere ADX dip"
    assert strat._trend_state[symbol] == "PULLBACK"  # didn't extend past ref -> pullback, not wiped
    assert strat._pullback_ref[symbol] == ref_before, "reference level must be preserved, not reseeded"
    assert strat._trend_direction[symbol] == "BUY"


def test_adx_dropping_below_exit_threshold_does_genuinely_reset():
    """The fix must not simply NEVER reset -- a real structural invalidation
    (ADX falls below the exit floor) still correctly clears tracked state."""
    strat, symbol = _established_strategy()

    # Bar 3: ADX collapses well below adx_exit_threshold (22) -- the trend
    # itself is genuinely over, not just "not accelerating this bar."
    signal = strat.generate_signal(_bar(symbol, adx=15.0, close=111.0, bar_key="live:t3"))

    assert signal == "HOLD"
    assert symbol not in strat._trend_state, "a genuine trend-exhaustion ADX reading must still reset tracking"


def test_direction_flip_still_resets_despite_trend_still_valid_check():
    """A reversal (fast_ema now below slow_ema) must still clear the old
    BUY-direction tracking, even though ADX itself stayed high."""
    strat, symbol = _established_strategy()

    # Bar 3: EMA direction flips to bearish (ema20 < ema50) while ADX stays
    # strong -- trend_still_valid becomes "SELL", which disagrees with the
    # tracked "BUY" direction, so this must still reset (not silently keep
    # tracking a BUY setup that no longer matches the data).
    signal = strat.generate_signal(_bar(symbol, ema20=95.0, ema50=100.0, adx=34.0, close=94.0, bar_key="live:t3"))

    assert signal == "HOLD"
    assert symbol not in strat._trend_state


def test_established_trend_can_still_fire_after_surviving_an_adx_dip():
    """End-to-end: a dip-then-resume sequence that would have been
    permanently broken by the old reset-on-any-raw=None behavior (the
    eventual breakout would restart tracking from scratch instead of firing
    on the real continuation)."""
    strat, symbol = _established_strategy()

    # Bar 3: ADX dip, pulls back slightly -> PULLBACK (not wiped, per the fix).
    strat.generate_signal(_bar(symbol, adx=34.0, close=111.0, rvol=0.5, bar_key="live:t3"))
    assert strat._trend_state.get(symbol) == "PULLBACK"

    # Bar 4: ADX resumes rising, price breaks back out above the preserved
    # reference (112.0) with strong RVOL confirming the contraction seen above.
    signal = strat.generate_signal(_bar(symbol, adx=38.0, close=113.5, rvol=1.6, bar_key="live:t4"))

    assert signal == "BUY", "must fire on the real continuation, not require restarting ESTABLISHED from scratch"
