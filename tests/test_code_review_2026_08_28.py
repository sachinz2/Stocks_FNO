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

import pytest

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


# ── Round 3: metrics-calculation audit (VIX already fixed separately) ──────
# User asked for a full audit of VIX/RVOL/ATR/ADX/EMA/IV-Rank/sigma/delta/
# OI/PCR calculation logic, and to prefer real Zerodha data where available.
# Confirmed via 4 parallel deep-read agents + direct verification: two
# severe, high-confidence bugs fixed here; several more real findings
# flagged for the user's decision (bigger-scope changes, not blind fixes).

from src.core.utils import estimate_option_premium
from src.market_data.ltp_poller import LTPPoller


def test_estimate_option_premium_fallback_applies_daily_atr_scale():
    """Live-incident-adjacent finding, 2026-08-28: the ATR-based fallback
    (used whenever a live option quote is stale/unavailable and no
    strike/underlying_price was supplied) used raw 5-min-bar ATR directly
    in a formula written for daily-scale ATR -- understating every premium
    estimate by ~8.66x (FIVE_MIN_ATR_DAILY_SCALE). The BS branch (called
    WITH strike/underlying_price) already applied this scale; the fallback
    never did."""
    from src.core.constants import FIVE_MIN_ATR_DAILY_SCALE
    atr = 2.0
    dte = 20
    # No underlying_price/strike -> fallback branch.
    premium = estimate_option_premium(atr, dte)
    expected = round((atr * FIVE_MIN_ATR_DAILY_SCALE) * 4.0 * 1.0, 2)  # sqrt(20/20)=1.0
    assert premium == expected
    # Sanity: this must be meaningfully larger than the old (unscaled) bug
    # would have produced -- guards against a future accidental revert.
    old_buggy_value = round(atr * 4.0 * 1.0, 2)
    assert premium > old_buggy_value * 5  # ~8.66x, generous margin for rounding


def test_estimate_option_premium_fallback_otm_discount_still_applies():
    atr = 2.0
    dte = 20
    atm = estimate_option_premium(atr, dte, otm_intervals=0)
    one_otm = estimate_option_premium(atr, dte, otm_intervals=1)
    assert one_otm == round(atm * 0.75, 2)


def test_estimate_option_premium_bs_branch_unaffected_by_fallback_fix():
    """Guard against over-fixing -- the correctly-scaled BS branch (called
    WITH strike/underlying_price) must be untouched."""
    premium = estimate_option_premium(
        atr=2.0, dte=20, underlying_price=100.0, strike=100.0, option_type="CE",
    )
    assert premium > 0.05  # a real BS-priced ATM premium, not the floor


# ── RVOL day-boundary contamination fix ─────────────────────────────────────
# Covered in detail by tests/test_ltp_poller.py's new tests; this file just
# confirms the fix note is present (matches this session's established
# "verify the fix landed" convention for source-level checks).

def test_ltp_poller_rvol_uses_todays_bars_not_multiday_series():
    src = inspect.getsource(LTPPoller._enrich)
    idx = src.index("RVOL — current bar volume")
    block = src[idx:idx + 2000]
    assert "if has_live_data and len(live_rows) >= 20:" in block
    assert "_today_vol = pd.Series([r[" in block


# ── risk_manager.validate_trade(): VIX/IV-rank gate must fail closed on None ─
# Same bug class as vix_allows_selling()/iv_rank_allows_selling(), fixed
# earlier the same day -- `if x is not None and x < threshold` silently
# skips the whole gate when x is None, instead of blocking. Defense-in-depth:
# real live entry paths already gate upstream via the already-fixed
# option_chain functions, so this shouldn't change today's production
# behavior, but any other/future caller of validate_trade() is now covered.

from src.risk.risk_manager import RiskManager


def test_validate_trade_blocks_credit_spread_when_iv_rank_missing():
    rm = RiskManager(initial_capital=300_000.0)
    ok = rm.validate_trade(
        "TESTCE", "SELL", 25, 10.0,
        strategy_name="credit_spread_v1", capital_at_risk=500.0,
        iv_rank=None, vix=15.0,
    )
    assert ok is False


def test_validate_trade_blocks_iron_condor_when_vix_missing():
    rm = RiskManager(initial_capital=300_000.0)
    ok = rm.validate_trade(
        "TESTPE", "SELL", 25, 10.0,
        strategy_name="iron_condor_v1", capital_at_risk=500.0,
        iv_rank=0.5, vix=None,
    )
    assert ok is False


def test_validate_trade_allows_credit_spread_with_real_iv_rank_and_vix():
    """Guard against over-fixing -- real values in range must still pass."""
    rm = RiskManager(initial_capital=300_000.0)
    ok = rm.validate_trade(
        "TESTCE", "SELL", 25, 10.0,
        strategy_name="credit_spread_v1", capital_at_risk=500.0,
        iv_rank=0.5, vix=15.0,
    )
    assert ok is True


def test_validate_trade_ema_crossover_unaffected_by_iv_gate():
    """The IV/VIX gate only applies to premium-selling strategies -- a
    directional strategy passing neither must be unaffected."""
    rm = RiskManager(initial_capital=300_000.0)
    ok = rm.validate_trade("TESTCE", "BUY", 25, 10.0, strategy_name="ema_crossover_v1")
    assert ok is True


# ── bs_delta(): deep-ITM put edge case must return -1.0, not 0.0 ───────────
# Reachable pre-expiry whenever atr_to_annualised_vol() returns exactly 0.0
# (atr==0); some entry-path call sites don't guard atr>0 before calling
# bs_delta(). A wrongly-0.0 PE delta misreports a deep-ITM put as having no
# directional exposure at all.

from src.market_data.option_chain import bs_delta


def test_bs_delta_deep_itm_put_at_expiry_returns_minus_one():
    assert bs_delta(S=100.0, K=150.0, T=0.0, sigma=0.2, option_type="PE") == -1.0


def test_bs_delta_deep_otm_put_at_expiry_returns_zero():
    assert bs_delta(S=150.0, K=100.0, T=0.0, sigma=0.2, option_type="PE") == 0.0


def test_bs_delta_deep_itm_call_at_expiry_still_returns_one():
    """Guard against over-fixing -- the CE branch (already correct) must be
    unaffected."""
    assert bs_delta(S=150.0, K=100.0, T=0.0, sigma=0.2, option_type="CE") == 1.0


def test_bs_delta_zero_sigma_put_uses_same_itm_edge_case():
    assert bs_delta(S=100.0, K=150.0, T=1.0, sigma=0.0, option_type="PE") == -1.0


# ── is_strike_crowded(): now fails CLOSED on missing OI data ───────────────
# Flipped 2026-08-28 (same day, later in the audit) once Zerodha became the
# PRIMARY OI source with NSE as fallback -- a total outage now needs BOTH
# sources down at once, rare enough to match this codebase's normal
# fail-closed convention for entry-risk parameters.

from src.market_data.nse_oi import is_strike_crowded


def test_is_strike_crowded_fails_closed_when_oi_data_missing(caplog):
    with caplog.at_level("WARNING"):
        result = is_strike_crowded(25000, None, "CE")
    assert result is True
    assert any("OI data unavailable" in r.message for r in caplog.records)


def test_is_strike_crowded_still_works_normally_with_real_oi_data():
    oi_data = {"crowded_call_strikes": [25000], "crowded_put_strikes": []}
    assert is_strike_crowded(25000, oi_data, "CE") is True
    assert is_strike_crowded(24900, oi_data, "CE") is False


def test_total_oi_outage_makes_crowded_strike_search_skip_entry():
    """End-to-end: with is_strike_crowded now fail-closed, a total OI outage
    (oi_data=None reaching every candidate) must make
    _find_non_crowded_strike_within_delta_tolerance() return None -- the
    caller's existing convention for "skip this entry"."""
    from src.live_trading.live_trading_engine import LiveTradingEngine
    result = LiveTradingEngine._find_non_crowded_strike_within_delta_tolerance(
        oi_data=None, opt="PE", base_strike=100.0, interval=50.0,
        target_delta=-0.20, underlying_price=100.0, dte=20, sigma=0.2,
    )
    assert result is None


# ── OI/PCR: Zerodha kite.quote() as primary source, NSE scrape as fallback ──
# User-requested 2026-08-28: "take data from zerodha, keep NSE data for
# backup". Zerodha's kite.quote() already returns real, live `oi` for the
# same option contracts elsewhere in this codebase -- now the primary OI
# source via the daily-refreshed real-contract cache (REDIS_CONTRACT_PREFIX)
# that already backs get_real_contract()/get_real_strike_interval(). NSE
# scrape only runs when Zerodha is unavailable (no kite, cache miss, or
# kite.quote() itself fails).

import json as _json_oi
import src.market_data.nse_oi as nse_oi_module
from src.market_data.nse_oi import get_oi_data
from src.core.constants import REDIS_CONTRACT_PREFIX


class _FakeRedisOI:
    def __init__(self, values=None):
        self._values = values or {}

    async def get(self, key):
        return self._values.get(key)

    async def set(self, key, value, ex=None):
        self._values[key] = value


class _FakeKiteOI:
    def __init__(self, oi_by_key):
        self._oi_by_key = oi_by_key

    def quote(self, instruments):
        return {k: {"oi": self._oi_by_key[k]} for k in instruments if k in self._oi_by_key}


def _contract_cache_payload():
    return {
        "2026-09-25": {
            "100": {"CE": "SYM26SEP100CE", "PE": "SYM26SEP100PE"},
            "110": {"CE": "SYM26SEP110CE", "PE": "SYM26SEP110PE"},
        }
    }


@pytest.mark.asyncio
async def test_get_oi_data_uses_zerodha_when_available_and_never_touches_nse(monkeypatch):
    def _fail_if_called(symbol):
        raise AssertionError("NSE scrape must not run when Zerodha succeeds")
    monkeypatch.setattr(nse_oi_module, "_fetch_option_chain_blocking", _fail_if_called)

    redis = _FakeRedisOI({
        f"{REDIS_CONTRACT_PREFIX}SYM": _json_oi.dumps(_contract_cache_payload()),
    })
    kite = _FakeKiteOI({
        "NFO:SYM26SEP100CE": 5000, "NFO:SYM26SEP100PE": 8000,
        "NFO:SYM26SEP110CE": 3000, "NFO:SYM26SEP110PE": 2000,
    })

    result = await get_oi_data("SYM", redis, kite=kite)

    assert result is not None
    assert result["source"] == "zerodha"
    assert result["total_call_oi"] == 8000
    assert result["total_put_oi"] == 10000
    assert result["pcr"] == round(10000 / 8000, 3)
    assert result["expiry_date"] == "2026-09-25"
    assert set(result["crowded_call_strikes"]) == {100, 110}
    assert set(result["crowded_put_strikes"]) == {100, 110}


@pytest.mark.asyncio
async def test_get_oi_data_falls_back_to_nse_when_kite_unavailable(monkeypatch):
    calls = []

    def _fake_nse(symbol):
        calls.append(symbol)
        return {
            "data": [{
                "strikePrice": 100,
                "CE": {"expiryDate": "25-Sep-2026", "openInterest": 4000},
                "PE": {"expiryDate": "25-Sep-2026", "openInterest": 6000},
            }],
            "expiryDates": ["25-Sep-2026"],
        }
    monkeypatch.setattr(nse_oi_module, "_fetch_option_chain_blocking", _fake_nse)

    redis = _FakeRedisOI({})  # no contract cache either way
    result = await get_oi_data("SYM", redis, kite=None)

    assert calls == ["SYM"]
    assert result is not None
    assert result["source"] == "nse"
    assert result["total_call_oi"] == 4000
    assert result["total_put_oi"] == 6000


@pytest.mark.asyncio
async def test_get_oi_data_falls_back_to_nse_when_zerodha_contract_cache_missing(monkeypatch):
    """kite IS available, but the real-contract cache has no entry for this
    symbol (e.g. daily refresh hasn't run yet) -- must fall back, not
    return None outright."""
    calls = []

    def _fake_nse(symbol):
        calls.append(symbol)
        return {
            "data": [{
                "strikePrice": 100,
                "CE": {"expiryDate": "25-Sep-2026", "openInterest": 1111},
                "PE": {"expiryDate": "25-Sep-2026", "openInterest": 2222},
            }],
            "expiryDates": ["25-Sep-2026"],
        }
    monkeypatch.setattr(nse_oi_module, "_fetch_option_chain_blocking", _fake_nse)

    redis = _FakeRedisOI({})  # cache miss for REDIS_CONTRACT_PREFIX too
    kite = _FakeKiteOI({})
    result = await get_oi_data("SYM", redis, kite=kite)

    assert calls == ["SYM"]
    assert result is not None
    assert result["source"] == "nse"


@pytest.mark.asyncio
async def test_get_oi_data_reads_cache_before_hitting_either_source(monkeypatch):
    def _fail_zerodha(*a, **kw):
        raise AssertionError("must not fetch -- cache should have served this")
    monkeypatch.setattr(nse_oi_module, "_fetch_zerodha_option_chain", _fail_zerodha)
    def _fail_nse(symbol):
        raise AssertionError("must not fetch -- cache should have served this")
    monkeypatch.setattr(nse_oi_module, "_fetch_option_chain_blocking", _fail_nse)

    cached_payload = {"pcr": 1.1, "source": "zerodha", "total_call_oi": 1, "total_put_oi": 1}
    redis = _FakeRedisOI({"nse_oi:SYM": _json_oi.dumps(cached_payload)})
    result = await get_oi_data("SYM", redis, kite=_FakeKiteOI({}))

    assert result == cached_payload


# ── Exit-side delta checks now prefer live market IV over the ATR proxy ────
# User-requested 2026-08-28: "I believe live data would be right choice" --
# matches the entry side (_get_live_sigma, upgraded 2026-08-21) instead of
# staying on the ATR-derived historical-vol proxy only.

def test_credit_spread_exit_delta_check_uses_live_sigma():
    src = inspect.getsource(LiveTradingEngine._check_spread_exits)
    idx = src.index("Delta-based exit — short leg delta")
    block = src[idx:idx + 2000]
    assert "await self._get_live_sigma(" in block


def test_iron_condor_exit_delta_check_uses_live_sigma():
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    idx = src.index("Delta-based exit — if either short leg")
    block = src[idx:idx + 1200]
    assert "await self._get_live_sigma(" in block


# ── implied_vol(): no more clamped-but-not-converged sigma escaping as if
# it were a real solve ──────────────────────────────────────────────────────

import src.market_data.option_chain as option_chain_module
from src.market_data.option_chain import implied_vol, bs_price


def test_implied_vol_returns_none_when_vega_collapses(monkeypatch):
    monkeypatch.setattr(option_chain_module, "_norm_pdf", lambda x: 0.0)
    assert option_chain_module.implied_vol(10.0, 100.0, 100.0, 0.5, "CE") is None


def test_implied_vol_returns_none_when_iterations_exhausted(monkeypatch):
    monkeypatch.setattr(option_chain_module, "bs_price", lambda S, K, T, sigma, option_type: 999.0)
    assert option_chain_module.implied_vol(10.0, 100.0, 100.0, 0.5, "CE") is None


def test_implied_vol_still_converges_for_normal_inputs():
    """Guard against over-fixing -- a genuinely solvable price must still
    return a real answer close to the vol it was priced from."""
    price = bs_price(100.0, 100.0, 0.5, 0.20, "CE")
    result = implied_vol(price, 100.0, 100.0, 0.5, "CE")
    assert result is not None
    assert abs(result - 0.20) < 0.01


# ── Entry-time Greeks/IV computation now has a try/except ──────────────────
# Runs AFTER both legs are already filled at the broker -- an unguarded
# exception here used to skip _log_trade_open() entirely, leaving a real,
# live position with no journal entry, no deployed-capital tracking, and no
# GTT backstop.

def test_credit_spread_entry_greeks_has_try_except():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("actual entry")
    block = src[idx:idx + 2800]
    assert "try:" in block
    assert "except Exception as _greeks_exc:" in block


def test_iron_condor_entry_greeks_has_try_except():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("actual entry")
    block = src[idx:idx + 2000]
    assert "try:" in block
    assert "except Exception as _greeks_exc_c:" in block


# ── credit_spread_v1/iron_condor_v1 now trade 2 lots ────────────────────────
# User-requested 2026-08-28: consistently the best-performing strategies to
# date, still PAPER-only. Applied by multiplying the real exchange lot size
# once, right after _get_lot_size() -- every downstream quantity (order
# size, margin check, capital_at_risk, journal, GTT backstop) derives from
# that same variable.

from src.core.constants import STRATEGY_LOT_MULTIPLIER


def test_lot_multiplier_is_2_for_premium_selling_strategies_only():
    assert STRATEGY_LOT_MULTIPLIER["credit_spread_v1"] == 2
    assert STRATEGY_LOT_MULTIPLIER["iron_condor_v1"] == 2
    assert STRATEGY_LOT_MULTIPLIER.get("ema_crossover_v1", 1) == 1
    assert STRATEGY_LOT_MULTIPLIER.get("momentum_v1", 1) == 1


def test_credit_spread_entry_applies_lot_multiplier_after_fail_closed_check():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("no verified real lot size available")
    block = src[idx:idx + 700]
    assert "lot_size  *= STRATEGY_LOT_MULTIPLIER.get(strategy.name, 1)" in block


def test_iron_condor_entry_applies_lot_multiplier_after_fail_closed_check():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("no verified real lot size available")
    block = src[idx:idx + 700]
    assert "lot_size  *= STRATEGY_LOT_MULTIPLIER.get(strategy.name, 1)" in block


def test_single_leg_entry_does_not_apply_lot_multiplier():
    """Guard against over-fixing -- ema_crossover_v1/momentum_v1's shared
    single-leg entry path must be untouched."""
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert "STRATEGY_LOT_MULTIPLIER" not in src


# ── Dead code removal: IndicatorEngine, SignalGenerator, oi_price_signal() ──
# Confirmed via grep (no callers anywhere in src/ or tests/, no __init__.py
# re-exports) before deleting -- not guessed.

import os


def test_indicator_engine_file_removed():
    assert not os.path.exists(os.path.join("src", "indicators", "indicator_engine.py"))


def test_signal_generator_file_removed():
    assert not os.path.exists(os.path.join("src", "strategies", "signal_generator.py"))


def test_oi_price_signal_function_removed():
    assert not hasattr(nse_oi_module, "oi_price_signal")
