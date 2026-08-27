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
"""
import inspect

from src.strategies.ema_crossover import EMACrossoverStrategy
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
