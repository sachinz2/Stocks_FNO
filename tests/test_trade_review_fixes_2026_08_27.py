"""
Fixes from the 2026-08-27 trade review of ema_crossover_v1's Aug 24-26
performance (28 trades, 17.9% win rate, -Rs43,692). Exit-log analysis of all
27 exits with a clear trigger showed:
  - EMA reversal exit: 12/27 (44%), 100% losses, several firing on an
    EMA20/50 gap of a few hundredths of a point (once literally equal) --
    noise, not a real trend reversal.
  - Underlying-based stop (1.0x ATR): 8/27 (30%), all losses -- tight
    enough that normal intraday noise plausibly tripped some of these.
  - Underlying-based target (2.0x ATR): 6/27 (22%), all wins.
  - The other 4 exit rules (hard 50% stop, 100% target, trailing stop,
    breakeven stop) never fired once -- the first two checks always get
    there first.

Four fixes, all in ema_crossover.py + its src/api/main.py wiring:
  1. ema_reversal_min_gap_pct (default 0.1%) -- the EMA20/50 gap must
     exceed this before a sign flip counts as "reversed" at all.
  2. ema_reversal_confirm_bars (default 2) -- the gap-qualified reversal
     must hold across this many genuinely distinct bars (same bar_key
     debounce as generate_signal()'s entry confirmation) before it exits.
  3. underlying_stop_atr_mult raised 1.0 -> 1.4.
  4. adx_entry_threshold raised 18 -> 22.

A follow-up entry-quality check (same review) found ADX/RVOL at entry don't
discriminate winners from losers in the same 28-trade sample, but the entry
side never checked the EMA20/50 gap itself the way the exit-side fix above
now does -- entry_min_gap_pct (default 0.1%) added, checked once at fire
time alongside the existing ADX gate.

A second follow-up (same review) diagnosed momentum_v1's zero trades over
the same 3 days: grepping all 3 archived daily logs found 1,449 "too
extended" rejections (median 2.69x ATR from EMA20 among candidates that had
already passed every other gate) against the old extension_atr_mult=1.5
floor, and zero -- not one -- "pullback started"/"breakout confirmed" log
lines in any of the 3 days, meaning the strategy's actual pullback+breakout
entry logic never once got a chance to run. extension_atr_mult raised
1.5 -> 2.5, vwap_extension_pct raised 1.5 -> 2.5 for the same reason.
"""
import inspect

from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.momentum import MomentumStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine


def _ema(**overrides):
    strat = EMACrossoverStrategy("ema_crossover_v1", overrides)
    strat.initialize()
    return strat


def _pos(fast, slow, is_call=True, contract="X26SEP100CE", bar_key="live:t0", **extra):
    p = {
        "avg_price": 40.0, "peak_premium": 40.0,
        "is_call": is_call, "current_ema_fast": fast, "current_ema_slow": slow,
        "contract": contract, "ohlc_bar_key": bar_key,
    }
    p.update(extra)
    return p


# ── Fix 1: minimum gap before a "reversal" counts at all ───────────────────

def test_a_literally_equal_ema_gap_does_not_exit():
    """Reproduces the exact real-world pattern from the Aug 26 trade log:
    fast=184.67 slow=184.67 (equal) -- must not exit."""
    strat = _ema()
    result = strat.manage_position(_pos(184.67, 184.67, is_call=True), 39.0)
    assert result == "HOLD"


def test_a_hundredth_point_gap_does_not_exit():
    """Reproduces fast=1801.19 slow=1801.20 (0.0006% apart) from the Aug 24
    log -- below the 0.1% default gap floor, must not exit."""
    strat = _ema()
    result = strat.manage_position(_pos(1801.19, 1801.20, is_call=True), 39.0)
    assert result == "HOLD"


def test_a_gap_past_the_floor_is_eligible_to_exit():
    """A genuine, well-past-the-floor reversal (>0.1% gap) must still be
    able to exit once bar-confirmed (see confirm_bars tests below) --
    confirms fix 1 doesn't disable the check entirely."""
    strat = _ema(ema_reversal_confirm_bars=1)
    # 0.5% gap, well past the 0.1% default floor, confirmed on 1 bar.
    result = strat.manage_position(_pos(99.4, 100.0, is_call=True, bar_key="live:t0"), 39.0)
    assert result == "EXIT"


def test_min_gap_is_configurable():
    strat = _ema(ema_reversal_min_gap_pct=0.02)  # 2% floor, deliberately high
    # 0.5% gap -- below this strategy instance's (unusually high) 2% floor.
    result = strat.manage_position(_pos(99.4, 100.0, is_call=True), 39.0)
    assert result == "HOLD"


# ── Fix 2: bar-count confirmation before a qualified reversal fires ────────

def test_qualified_reversal_does_not_fire_on_the_first_bar_with_default_confirm_bars():
    strat = _ema()  # ema_reversal_confirm_bars defaults to 2
    result = strat.manage_position(_pos(99.0, 100.0, is_call=True, bar_key="live:t0"), 39.0)
    assert result == "HOLD"


def test_qualified_reversal_fires_once_confirmed_over_two_distinct_bars():
    strat = _ema()
    contract = "X26SEP100CE"
    r1 = strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t0"), 39.0)
    assert r1 == "HOLD"
    r2 = strat.manage_position(_pos(98.9, 100.0, is_call=True, contract=contract, bar_key="live:t1"), 39.0)
    assert r2 == "EXIT"


def test_same_bar_key_repeated_does_not_advance_the_confirmation_count():
    """Same convention as generate_signal()'s entry confirmation -- multiple
    engine cycles landing on the same 5-min candle must not double-count."""
    strat = _ema()
    contract = "X26SEP100CE"
    strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t0"), 39.0)
    # Same bar_key again -- must not advance toward firing.
    r = strat.manage_position(_pos(98.9, 100.0, is_call=True, contract=contract, bar_key="live:t0"), 39.0)
    assert r == "HOLD"
    # A genuinely new bar_key now advances and fires.
    r2 = strat.manage_position(_pos(98.9, 100.0, is_call=True, contract=contract, bar_key="live:t1"), 39.0)
    assert r2 == "EXIT"


def test_reversal_confirmation_resets_if_the_gap_closes_back_up():
    """If the EMA gap un-reverses (or closes back under the floor) before
    confirming, the pending count must reset rather than carrying stale
    progress into a later, unrelated reversal."""
    strat = _ema()
    contract = "X26SEP100CE"
    strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t0"), 39.0)
    # Thesis re-intact (fast back above slow) -- must clear pending progress.
    strat.manage_position(_pos(101.0, 100.0, is_call=True, contract=contract, bar_key="live:t1"), 39.0)
    # A fresh reversal on the very next bar must NOT fire immediately --
    # it needs its own 2 bars, not "1 leftover + 1 new."
    r = strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t2"), 39.0)
    assert r == "HOLD"


def test_confirm_bars_is_configurable_to_one_for_immediate_firing():
    strat = _ema(ema_reversal_confirm_bars=1)
    result = strat.manage_position(_pos(99.0, 100.0, is_call=True, bar_key="live:t0"), 39.0)
    assert result == "EXIT"


def test_multiple_concurrent_positions_track_reversal_state_independently():
    """ema_crossover_v1 can hold 2 concurrent positions -- confirmation
    state for one contract must not leak into another."""
    strat = _ema()
    strat.manage_position(_pos(99.0, 100.0, is_call=True, contract="A26SEPCE", bar_key="live:t0"), 39.0)
    # A different contract's first qualifying bar must also just seed, not
    # inherit A's count.
    r = strat.manage_position(_pos(99.0, 100.0, is_call=True, contract="B26SEPCE", bar_key="live:t0"), 39.0)
    assert r == "HOLD"
    assert strat._reversal_pending_count.get("A26SEPCE") == 1
    assert strat._reversal_pending_count.get("B26SEPCE") == 1


def test_no_contract_identifier_degrades_to_gap_filter_only_no_crash():
    """A caller that doesn't pass a contract (e.g. an older test harness)
    must not crash -- degrades to immediate gap-filtered firing rather than
    silently never exiting."""
    strat = _ema()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "is_call": True,
        "current_ema_fast": 99.0, "current_ema_slow": 100.0,
    }
    result = strat.manage_position(pos, 39.0)
    assert result == "EXIT"


# ── Fix 3: underlying-based stop widened 1.0x -> 1.4x ATR ──────────────────

def test_underlying_stop_default_is_1_4x_atr():
    strat = _ema()
    assert strat.underlying_stop_atr_mult == 1.4


def test_underlying_stop_at_old_1x_level_no_longer_fires():
    """A move that would have tripped the old 1.0x ATR stop must NOT trip
    the new 1.4x ATR stop."""
    strat = _ema()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "is_call": True,
        "current_ema_fast": 105.0, "current_ema_slow": 100.0,  # thesis intact -- no reversal exit
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 97.9,  # old stop level (100 - 1.0x2.0 = 98) -- breached
                                 # new stop level (100 - 1.4x2.0 = 97.2) -- NOT breached
    }
    assert strat.manage_position(pos, 39.5) == "HOLD"


# ── Fix 4: ADX entry threshold raised 18 -> 22 ──────────────────────────────

def test_adx_entry_threshold_default_is_22():
    strat = _ema()
    assert strat.adx_entry_threshold == 22


def test_crossover_at_adx_20_no_longer_fires_without_rising():
    """ADX=20 passed the old 18 threshold; must not pass the new 22
    threshold on its own (flat, not rising)."""
    strat = _ema(signal_confirm_bars=1)
    strat.generate_signal({"symbol": "X", "ema20": 99.0, "ema50": 100.0, "adx14": 20.0, "ohlc_bar_key": "live:t0"})
    signal = strat.generate_signal({"symbol": "X", "ema20": 101.0, "ema50": 100.0, "adx14": 20.0, "ohlc_bar_key": "live:t1"})
    assert signal == "HOLD"


# ── Entry-side follow-up: minimum EMA gap required to fire at all ──────────
# (2026-08-27, after user feedback that the exit-side fix alone doesn't
# address why entries themselves were frequently poor -- ADX/RVOL at entry
# were checked against the real 28-trade sample and neither discriminates
# winners from losers, but the EMA gap itself was never checked on the entry
# side even though the exit side now requires one. Mirrors
# ema_reversal_min_gap_pct, checked once at fire time alongside adx_ok.)

def test_entry_min_gap_default_is_0_001():
    strat = _ema()
    assert strat.entry_min_gap_pct == 0.001


def test_crossover_with_tiny_gap_does_not_fire_even_with_adx_and_bars_confirmed():
    strat = _ema(signal_confirm_bars=1)
    strat.generate_signal({"symbol": "X", "ema20": 99.0, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t0"})
    # 0.05% gap -- ADX and bar-confirmation both pass, but the gap doesn't.
    signal = strat.generate_signal({"symbol": "X", "ema20": 100.05, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t1"})
    assert signal == "HOLD"


def test_crossover_with_gap_past_the_floor_fires():
    strat = _ema(signal_confirm_bars=1)
    strat.generate_signal({"symbol": "X", "ema20": 99.0, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t0"})
    # 0.5% gap, well past the 0.1% default floor.
    signal = strat.generate_signal({"symbol": "X", "ema20": 100.5, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t1"})
    assert signal == "BUY"


def test_entry_min_gap_is_configurable():
    strat = _ema(signal_confirm_bars=1, entry_min_gap_pct=0.02)  # 2% floor, deliberately high
    strat.generate_signal({"symbol": "X", "ema20": 99.0, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t0"})
    # 0.5% gap -- below this strategy instance's (unusually high) 2% floor.
    signal = strat.generate_signal({"symbol": "X", "ema20": 100.5, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t1"})
    assert signal == "HOLD"


def test_thin_gap_holds_pending_state_for_a_later_bar_once_it_widens():
    """A crossover that starts thin but widens on the very next confirming
    bar must still fire without needing a brand-new crossover to restart --
    same "hold, don't clear" behavior already established for adx_ok."""
    strat = _ema(signal_confirm_bars=1)
    strat.generate_signal({"symbol": "X", "ema20": 99.0, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t0"})
    r1 = strat.generate_signal({"symbol": "X", "ema20": 100.05, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t1"})
    assert r1 == "HOLD"
    r2 = strat.generate_signal({"symbol": "X", "ema20": 100.5, "ema50": 100.0, "adx14": 25.0, "ohlc_bar_key": "live:t2"})
    assert r2 == "BUY"


def test_main_py_wires_entry_min_gap_pct():
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    idx = src.index('StrategyRegistry.load_strategy("EMA_CROSSOVER"')
    block = src[idx:idx + 3000]
    assert '"entry_min_gap_pct": 0.001' in block


# ── momentum_v1: extension/VWAP filters widened, zero trades Aug 24-26 ──────

def _mom(**overrides):
    strat = MomentumStrategy("momentum_v1", overrides)
    strat.initialize()
    return strat


def test_momentum_extension_atr_mult_default_is_2_5():
    strat = _mom()
    assert strat.extension_atr_mult == 2.5


def test_momentum_vwap_extension_pct_default_is_2_5():
    strat = _mom()
    assert strat.vwap_extension_pct == 2.5


def test_momentum_extension_at_old_1_5x_no_longer_blocks_the_entry():
    """A candidate that would have tripped the old 1.5x ATR extension floor
    (real median observed in production was 2.69x among otherwise-qualifying
    candidates) must now pass."""
    strat = _mom(signal_confirm_bars=1, use_pullback_continuation_model=False,
                 adx_rising_required=False, ema_slope_required=False, vwap_extension_pct=0)
    strat.generate_signal({
        "symbol": "X", "ema20": 105.0, "ema50": 100.0, "adx14": 30.0,
        "close": 105.0, "atr14": 20.0, "ohlc_bar_key": "live:t0",
    })
    # close is 40 away from ema20=105 -> 2.0x ATR: over the old 1.5x cap,
    # under the new 2.5x cap.
    signal = strat.generate_signal({
        "symbol": "X", "ema20": 105.0, "ema50": 100.0, "adx14": 30.0,
        "close": 145.0, "atr14": 20.0, "ohlc_bar_key": "live:t1",
    })
    assert signal == "BUY"


def test_momentum_extension_still_blocks_a_genuinely_blown_out_move():
    """The widened floor isn't a no-op -- a move well past even the new 2.5x
    cap must still be rejected."""
    strat = _mom(signal_confirm_bars=1, use_pullback_continuation_model=False,
                 adx_rising_required=False, ema_slope_required=False, vwap_extension_pct=0)
    strat.generate_signal({
        "symbol": "X", "ema20": 105.0, "ema50": 100.0, "adx14": 30.0,
        "close": 105.0, "atr14": 20.0, "ohlc_bar_key": "live:t0",
    })
    # close is 80 away from ema20=105 -> 4.0x ATR: over the new 2.5x cap too.
    signal = strat.generate_signal({
        "symbol": "X", "ema20": 105.0, "ema50": 100.0, "adx14": 30.0,
        "close": 185.0, "atr14": 20.0, "ohlc_bar_key": "live:t1",
    })
    assert signal == "HOLD"


def test_main_py_wires_momentum_extension_and_vwap_fixes():
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    idx = src.index('StrategyRegistry.load_strategy("MOMENTUM"')
    block = src[idx:idx + 2000]
    assert '"extension_atr_mult": 2.5' in block
    assert '"vwap_extension_pct": 2.5' in block


# ── momentum_v1: extension no longer wipes an already-tracked pullback ─────
# Live incident, same day: even after the 1.5x->2.5x widening above, a
# genuinely strong trending day (2026-08-27) produced 105 extension
# rejections, ALL above 2.5x (observed 2.66x-5.11x) -- the pullback+breakout
# model's tracked state was being wiped every cycle the moment a maturing
# trend crossed the extension line, even though the model's own reference-
# level mechanism already protects against chasing an exhausted move.
# Fixed: extension/VWAP now only gate the FRESH-qualification moment
# (starting to track a NEW setup), not every re-evaluation of an
# already-tracked one.

def _mbar(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx=30.0, close=108.0,
          atr=3.0, vwap=None, rvol=1.6, rvol_valid=True, bar_key="live:t0"):
    return {
        "symbol": symbol, "ema20": ema20, "ema50": ema50, "adx14": adx,
        "close": close, "atr14": atr, "vwap": vwap if vwap is not None else close,
        "rvol": rvol, "rvol_valid": rvol_valid, "ohlc_bar_key": bar_key,
    }


def test_extension_blocks_a_brand_new_setup_from_ever_starting_to_track():
    """First qualifying bar is already too extended -- must NOT start
    tracking (unchanged from the old per-cycle behavior for a fresh
    setup)."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=2.0, vwap_extension_pct=0)
    # ema20=105, close=120, atr=3.0 -> 5.0x ATR, over the 2.0x cap from bar 0.
    signal = strat.generate_signal(_mbar(close=120.0, bar_key="live:t0"))
    assert signal == "HOLD"
    assert "RELIANCE" not in strat._trend_state


def test_extension_crossing_the_cap_mid_setup_no_longer_wipes_tracked_progress():
    """Reproduces the live incident: a setup starts within the extension
    cap, then a later bar crosses it (as a maturing trend naturally pulls
    price further from EMA20) -- progress must survive, not reset."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=2.0, vwap_extension_pct=0)
    # Bar 0: ema20=105, close=108, atr=3.0 -> 1.0x ATR, within the 2.0x cap.
    strat.generate_signal(_mbar(close=108.0, bar_key="live:t0"))
    assert strat._trend_state["RELIANCE"] == "ESTABLISHED"
    # Bar 1: close=113 -> (113-105)/3 = 2.67x ATR, OVER the 2.0x cap. Under
    # the old behavior this would wipe tracking entirely; now it must not,
    # since RELIANCE is already tracked and this isn't a fresh qualification.
    signal = strat.generate_signal(_mbar(close=113.0, bar_key="live:t1"))
    assert signal == "HOLD"
    assert strat._trend_state.get("RELIANCE") == "ESTABLISHED", (
        "an already-tracked setup must not be reset just because a "
        "maturing trend crossed the extension line on a later bar"
    )
    assert strat._pullback_ref["RELIANCE"] == 113.0  # kept extending, ref raised


def test_full_pullback_and_breakout_still_fires_despite_extension_staying_over_cap():
    """End-to-end: extension crosses the cap at bar 1 and STAYS crossed for
    the rest of the setup (2.33x-3.0x, all over the 2.0x cap) -- the
    pullback+breakout sequence must still complete and fire, matching what
    the live incident showed was structurally broken before this fix."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=2.0, vwap_extension_pct=0)
    strat.generate_signal(_mbar(close=108.0, bar_key="live:t0"))          # ESTABLISHED, ref=108, 1.0x
    strat.generate_signal(_mbar(close=113.0, bar_key="live:t1"))          # extends, ref=113, 2.67x (over cap)
    signal = strat.generate_signal(_mbar(close=112.0, rvol=1.0, bar_key="live:t2"))  # pulls back, 2.33x (over cap)
    assert signal == "HOLD"
    assert strat._trend_state["RELIANCE"] == "PULLBACK"
    # Breaks back above ref=113, flat RVOL >= rvol_entry_threshold (1.5) -- fires.
    signal = strat.generate_signal(_mbar(close=114.0, rvol=1.6, bar_key="live:t3"))  # 3.0x, still over cap
    assert signal == "BUY"
    assert "RELIANCE" not in strat._trend_state


def test_vwap_extension_crossing_the_cap_mid_setup_also_does_not_wipe_progress():
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=1.0)
    # Bar 0: close == vwap -> 0% away, within the 1.0% cap.
    strat.generate_signal(_mbar(close=108.0, vwap=108.0, bar_key="live:t0"))
    assert strat._trend_state["RELIANCE"] == "ESTABLISHED"
    # Bar 1: close=113, vwap=108 -> 4.6% away, over the 1.0% cap.
    signal = strat.generate_signal(_mbar(close=113.0, vwap=108.0, bar_key="live:t1"))
    assert signal == "HOLD"
    assert strat._trend_state.get("RELIANCE") == "ESTABLISHED"


def test_extension_check_is_skipped_entirely_once_raw_already_none_from_an_earlier_gate(caplog):
    """Live bug caught via production logs right after deploying the fix
    above: 'SOLARINDS None candidate too extended' -- the extension/VWAP
    checks ran even after `raw` was already None from
    adx_rising_required/ema_slope_required failing earlier in the same
    function, producing a nonsensical log line (and wasted computation).
    Restored the `raw is not None and` guard both blocks had before the
    restructure -- must not evaluate or log at all once raw is already
    None."""
    strat = _mom(adx_rising_required=True, ema_slope_required=False,
                 extension_atr_mult=1.0, vwap_extension_pct=0)
    strat.generate_signal(_mbar(adx=35.0, close=108.0, bar_key="live:t0"))
    with caplog.at_level("INFO"):
        # ADX declining (35 -> 30) -- adx_rising_required fails, raw becomes
        # None BEFORE the extension check runs. Price is genuinely far from
        # EMA20 (close=200, ema20=105, atr=3 -> 31.7x ATR) but that must
        # never be evaluated or logged since raw is already None.
        strat.generate_signal(_mbar(adx=30.0, close=200.0, atr=3.0, bar_key="live:t1"))
    assert not any("too extended" in r.message for r in caplog.records)


def test_pullback_expiry_via_repeated_rvol_rejected_breakouts_is_logged_and_resets(caplog):
    """Live incident, round 2: a setup that keeps genuinely breaking out
    (price crosses ref every attempt) but never clears the RVOL bar hits
    max_pullback_bars via a DIFFERENT code path than the 'never broke out
    at all' expiry already covered above -- this one reset with NO log line
    at all, confirmed live (BDL disappeared and reappeared as a brand-new
    'trend established' one cycle later with nothing explaining the gap).
    Must now log and actually clear state after exactly max_pullback_bars
    failed attempts."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0, max_pullback_bars=3)
    strat.generate_signal(_mbar(close=108.0, bar_key="live:t0"))            # ESTABLISHED, ref=108
    strat.generate_signal(_mbar(close=107.0, rvol=1.0, bar_key="live:t1"))  # PULLBACK, ref=108, bars=1
    strat.generate_signal(_mbar(close=109.0, rvol=0.1, bar_key="live:t2"))  # breaks ref, RVOL fails, bars=2
    assert strat._trend_state.get("RELIANCE") == "PULLBACK"
    with caplog.at_level("INFO"):
        signal = strat.generate_signal(_mbar(close=109.0, rvol=0.1, bar_key="live:t3"))  # bars=3 -> expires
    assert signal == "HOLD"
    assert "RELIANCE" not in strat._trend_state
    assert any(
        "pullback setup expired" in r.message and "breakout attempts" in r.message
        for r in caplog.records
    )


# ── main.py wiring ───────────────────────────────────────────────────────

def test_main_py_wires_all_four_fixes_for_ema_crossover():
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    idx = src.index('StrategyRegistry.load_strategy("EMA_CROSSOVER"')
    block = src[idx:idx + 2400]
    assert '"adx_entry_threshold": 22' in block
    assert '"underlying_stop_atr_mult": 1.4' in block
    assert '"ema_reversal_min_gap_pct": 0.001' in block
    assert '"ema_reversal_confirm_bars": 2' in block


def test_engine_passes_contract_and_bar_key_into_manage_position():
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    idx = src.index("result = owner_strategy.manage_position(")
    block = src[idx:idx + 2300]
    assert '"contract":     contract' in block
    assert '"ohlc_bar_key": market_data.get("ohlc_bar_key")' in block
