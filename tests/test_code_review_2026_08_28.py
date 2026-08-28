"""
User-requested full review of all four strategies (2026-08-28), after the
prior day's live-incident fixes. Ran 4 parallel deep-read agents (one per
strategy), verified each finding against the actual code before fixing
anything -- not trusted from agent reports alone.

Confirmed and fixed here:
  - momentum_v1: a third silent state-reset path (direction flip failing
    the extension gate) of the same class already fixed for the two
    expiry paths on 2026-08-27 -- wiped tracked state with zero log output.
  - ema_crossover_v1: _reversal_pending_count/_reversal_pending_bar_key
    (the exit-side reversal-confirmation state added 2026-08-27) were never
    cleared when a position closed via any exit OTHER than the reversal
    check itself (stop loss, target, trailing, breakeven, underlying
    stop/target), and never cleared in on_pause() either -- unlike every
    other per-contract/per-symbol dict this strategy and momentum_v1 own.
    Stale partial confirmation progress could carry into a future position
    re-entering the same exact contract string, silently weakening the
    2-bar confirmation the 2026-08-27 fix was built to enforce.

Findings from the same review NOT fixed here, deliberately -- flagged for
the user's own judgment, not clear-cut bugs:
  - credit_spread_v1: VIX/IV-rank gates fail open on None (contradicts this
    codebase's fail-closed convention elsewhere) -- real, but changing a
    premium-selling entry gate's fail-safe direction is a risk-policy
    decision, not a pure bug fix.
  - credit_spread_v1: the repeated-stop-loss circuit breaker's adverse-exit
    string match doesn't catch manage_position()'s own stop-loss exit
    reason text.
  - credit_spread_v1: `spread_width` config parameter is dead (actual width
    is fully delta-driven) -- a documentation/config-surface issue, not a
    money-losing bug.
"""
import inspect

from src.strategies.momentum import MomentumStrategy
from src.strategies.ema_crossover import EMACrossoverStrategy


def _mom(**overrides):
    strat = MomentumStrategy("momentum_v1", overrides)
    strat.initialize()
    return strat


def _mbar(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx=30.0, close=108.0,
          atr=3.0, vwap=None, rvol=1.6, rvol_valid=True, bar_key="live:t0"):
    return {
        "symbol": symbol, "ema20": ema20, "ema50": ema50, "adx14": adx,
        "close": close, "atr14": atr, "vwap": vwap if vwap is not None else close,
        "rvol": rvol, "rvol_valid": rvol_valid, "ohlc_bar_key": bar_key,
    }


# ── momentum_v1: third silent-reset path (direction flip + extension) ──────

def test_direction_flip_failing_extension_is_logged_when_prior_state_existed(caplog):
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=2.0, vwap_extension_pct=0)
    # Bar0: BUY qualifies (ema20=105 > ema50=100), close=108 -> 1.0x ATR, within cap -> ESTABLISHED.
    strat.generate_signal(_mbar(ema20=105.0, ema50=100.0, close=108.0, atr=3.0, bar_key="live:t0"))
    assert strat._trend_state["RELIANCE"] == "ESTABLISHED"
    # Bar1: flips to SELL (ema20=95 < ema50=100), close=70 -> (95-70)/3=8.33x ATR, over the 2.0x cap.
    with caplog.at_level("INFO"):
        signal = strat.generate_signal(_mbar(ema20=95.0, ema50=100.0, close=70.0, atr=3.0, bar_key="live:t1"))
    assert signal == "HOLD"
    assert "RELIANCE" not in strat._trend_state
    assert any(
        "pullback setup cleared" in r.message and "direction flipped" in r.message
        for r in caplog.records
    )


def test_direction_flip_failing_extension_is_silent_with_no_prior_state():
    """A flip on a symbol with nothing tracked yet is a no-op -- must not log."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=2.0, vwap_extension_pct=0)
    signal = strat.generate_signal(_mbar(ema20=95.0, ema50=100.0, close=70.0, atr=3.0, bar_key="live:t0"))
    assert signal == "HOLD"
    assert "RELIANCE" not in strat._trend_state


def test_direction_flip_passing_extension_still_establishes_the_new_direction():
    """Guard against over-fixing -- a flip that DOES pass the extension gate
    must still start tracking the new direction normally."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=2.0, vwap_extension_pct=0)
    strat.generate_signal(_mbar(ema20=105.0, ema50=100.0, close=108.0, atr=3.0, bar_key="live:t0"))
    # Flip to SELL, close=93 -> (95-93)/3=0.67x ATR, within the 2.0x cap.
    signal = strat.generate_signal(_mbar(ema20=95.0, ema50=100.0, close=93.0, atr=3.0, bar_key="live:t1"))
    assert signal == "HOLD"  # ESTABLISHED, not fired yet
    assert strat._trend_state["RELIANCE"] == "ESTABLISHED"
    assert strat._trend_direction["RELIANCE"] == "SELL"


# ── ema_crossover_v1: exit-side reversal-confirmation state never cleared ──

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


def test_reversal_pending_state_is_cleared_when_position_exits_via_hard_stop():
    """Confirmed bug: a partial reversal-confirmation count (e.g. 1 of 2
    bars) that never completes because the position instead exits via the
    hard stop loss used to survive in memory forever (no engine call site
    ever touches these two dicts, unlike _peak_premiums). A future position
    re-entering the exact same contract string only needed ONE more
    qualifying bar instead of two, silently halving the 2026-08-27 fix's
    confirmation window."""
    strat = _ema()
    contract = "X26SEP100CE"
    # Bar0: qualifying reversal gap, but confirm_bars=2 by default -- only 1 of 2, HOLD.
    r1 = strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t0"), 39.0)
    assert r1 == "HOLD"
    assert strat._reversal_pending_count.get(contract) == 1
    # Position instead exits via the hard 50% stop loss (premium fell hard).
    r2 = strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t1"), 19.0)
    assert r2 == "EXIT"
    # The stale partial reversal count must not survive this unrelated exit.
    assert contract not in strat._reversal_pending_count
    assert contract not in strat._reversal_pending_bar_key


def test_reversal_pending_state_is_cleared_when_position_exits_via_target():
    strat = _ema()
    contract = "X26SEP100CE"
    strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t0"), 39.0)
    assert strat._reversal_pending_count.get(contract) == 1
    r2 = strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t1"), 90.0)
    assert r2 == "EXIT"
    assert contract not in strat._reversal_pending_count
    assert contract not in strat._reversal_pending_bar_key


def test_reversal_pending_state_is_cleared_on_pause():
    """on_pause() already clears _pending_*/_adx_history for the entry side
    -- the exit-side reversal dicts must get the same treatment, since a
    regime- or performance-triggered pause shouldn't let confirmation
    progress from before the pause count toward a reversal detected after
    it resumes."""
    strat = _ema()
    contract = "X26SEP100CE"
    strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t0"), 39.0)
    assert strat._reversal_pending_count.get(contract) == 1
    strat.on_pause()
    assert contract not in strat._reversal_pending_count
    assert contract not in strat._reversal_pending_bar_key


def test_reversal_confirmation_still_completes_normally_across_two_bars():
    """Guard against over-fixing -- the normal 2-bar reversal exit path
    (unrelated to any other exit firing first) must still work."""
    strat = _ema()
    contract = "X26SEP100CE"
    r1 = strat.manage_position(_pos(99.0, 100.0, is_call=True, contract=contract, bar_key="live:t0"), 39.0)
    assert r1 == "HOLD"
    r2 = strat.manage_position(_pos(98.9, 100.0, is_call=True, contract=contract, bar_key="live:t1"), 39.0)
    assert r2 == "EXIT"


def test_main_py_momentum_config_documents_its_own_gaps_not_asserted_here():
    """Informational finding only (main.py's momentum_v1 dict omits several
    class-default-matching params despite the file's own "list everything
    explicitly" convention) -- no functional impact, not asserted as a bug."""
    pass
