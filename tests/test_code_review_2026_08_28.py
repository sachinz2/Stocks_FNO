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

Round 2 (same day, user asked to fix the remaining findings too): four more
confirmed and fixed, all deliberately scoped to avoid touching the
established "OPEN status is trusted" convention used everywhere else in
this codebase:
  - credit_spread_v1/iron_condor_v1: vix_allows_selling()/
    iv_rank_allows_selling() now fail CLOSED on None instead of open,
    matching every other entry-blocking data gap in this codebase. Fixed
    the log-message formatting at all 4 call sites that would otherwise
    crash on f"{None:.1f}" now that the None branch is actually reachable.
  - credit_spread_v1/iron_condor_v1: the repeated-stop-loss circuit
    breaker now uses net_pnl < 0 (the exact basis already adopted a few
    lines below for profit_closes/adverse_closes bucketing) instead of
    string-matching exit_reason, which missed manage_position()'s own
    stop-loss exit text entirely.
  - credit_spread_v1: removed the dead `spread_width` config parameter
    (real width has always been delta-driven).
  - iron_condor_v1: the entry-failure unwind path now treats
    PENDING_VERIFICATION (a genuinely unconfirmed fill, not a success) the
    same as a failed unwind -- alerts instead of silently assuming closed.
    Deliberately did NOT touch plain "OPEN" handling here, which stays
    consistent with every other order-placement site in this file.
  - iron_condor_v1: partial-leg exit failures during the normal exit cycle
    (not just the expiry-day path, which already alerted) now send one
    notification per structure (guarded to avoid spamming on every 10s/60s
    retry cycle a stuck leg persists across).
"""
import inspect

from src.strategies.momentum import MomentumStrategy
from src.strategies.ema_crossover import EMACrossoverStrategy
from src.market_data.option_chain import vix_allows_selling, iv_rank_allows_selling
from src.strategies.credit_spread import CreditSpreadStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine


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


# ── Round 2: VIX/IV-rank gates now fail closed ──────────────────────────────

def test_vix_allows_selling_fails_closed_on_none():
    assert vix_allows_selling(None) is False


def test_vix_allows_selling_still_correct_for_real_values():
    assert vix_allows_selling(11.9) is False
    assert vix_allows_selling(12.0) is True
    assert vix_allows_selling(15.0) is True


def test_iv_rank_allows_selling_fails_closed_on_none():
    assert iv_rank_allows_selling(None) is False


def test_iv_rank_allows_selling_still_correct_for_real_values():
    assert iv_rank_allows_selling(0.29) is False
    assert iv_rank_allows_selling(0.30) is True


def test_credit_spread_vix_none_log_message_does_not_crash_on_formatting():
    """The old f"{vix:.1f}" would raise TypeError on vix=None -- now
    reachable since vix_allows_selling(None) returns False. Source-level
    check that the None case is branched around the numeric format string,
    not a functional call (that needs a full engine + broker mock)."""
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("if not vix_allows_selling(vix):")
    block = src[idx:idx + 500]
    assert "if vix is None:" in block
    assert "VIX unavailable" in block


def test_credit_spread_iv_rank_none_log_message_does_not_crash_on_formatting():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("if not iv_rank_allows_selling(iv_rank):")
    block = src[idx:idx + 500]
    assert "if iv_rank is None:" in block
    assert "IV Rank unavailable" in block


def test_iron_condor_vix_none_log_message_does_not_crash_on_formatting():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("if not vix_allows_selling(vix):")
    block = src[idx:idx + 500]
    assert "if vix is None:" in block
    assert "VIX unavailable" in block


def test_iron_condor_iv_rank_none_log_message_does_not_crash_on_formatting():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("if not iv_rank_allows_selling(iv_rank):")
    block = src[idx:idx + 500]
    assert "if iv_rank is None:" in block
    assert "IV Rank unavailable" in block


# ── Round 2: SL-frequency circuit breaker uses real P&L, not string match ──

def test_credit_spread_circuit_breaker_uses_net_pnl_not_string_match():
    src = inspect.getsource(LiveTradingEngine._check_spread_exits)
    idx = src.index("increment SL frequency counter for adverse exits")
    block = src[idx:idx + 1300]
    assert "_is_adverse = net_pnl < 0" in block
    assert "kw in exit_reason" not in block


def test_iron_condor_circuit_breaker_uses_net_pnl_not_string_match():
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    idx = src.index("increment SL frequency counter for adverse exits")
    block = src[idx:idx + 900]
    assert "_is_adverse_c = net_pnl < 0" in block
    assert "kw in exit_reason" not in block


# ── Round 2: dead spread_width parameter removed ────────────────────────────

def test_spread_width_no_longer_a_credit_spread_attribute():
    strat = CreditSpreadStrategy("credit_spread_v1", {})
    strat.initialize()
    assert not hasattr(strat, "spread_width")


def test_main_py_no_longer_configures_spread_width():
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    idx = src.index('StrategyRegistry.load_strategy("CREDIT_SPREAD"')
    block = src[idx:idx + 500]
    # Checking for the dict-key form specifically, not the bare substring --
    # the removal comment right above it legitimately mentions "spread_width"
    # in prose while explaining what was taken out.
    assert '"spread_width"' not in block


def test_spread_width_override_is_silently_ignored_not_an_error():
    """Guard against over-fixing -- a caller passing spread_width in
    parameters (e.g. a stale config) must not crash initialize(), just be
    ignored like any other unread parameter."""
    strat = CreditSpreadStrategy("credit_spread_v1", {"spread_width": 5})
    strat.initialize()  # must not raise
    assert not hasattr(strat, "spread_width")


# ── Round 2: iron_condor unwind PENDING_VERIFICATION handling ──────────────

def test_iron_condor_unwind_treats_pending_verification_as_uncertain():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("_bad_uw = {")
    block = src[idx:idx + 2000]
    assert '_uncertain_uw = {"PENDING_VERIFICATION"}' in block
    assert "_uw_status in _uncertain_uw" in block
    # PENDING_VERIFICATION must NOT be silently folded into the plain
    # "OPEN = trust it" success path -- it needs its own branch.
    assert '"PENDING_VERIFICATION"' not in block.split('_uncertain_uw = {')[0]


# ── Round 2: iron_condor partial-leg exit failure now alerts (throttled) ───

def test_iron_condor_partial_exit_failure_now_notifies():
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    idx = src.index("Exit orders for {underlying} partially rejected")
    block = src[idx:idx + 1400]
    assert "await self._notify(" in block


def test_iron_condor_partial_exit_alert_is_throttled_per_structure():
    """Must not notify every cycle a stuck leg persists -- this exit-check
    runs on both the 60s signal cycle and the 10s fast-exit job."""
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    idx = src.index("Exit orders for {underlying} partially rejected")
    block = src[idx:idx + 1400]
    assert '_partial_exit_alerted' in block
    assert 'if not c.get("_partial_exit_alerted")' in block
