"""
Granular rejection detail for credit_spread_v1/iron_condor_v1's OWN gates
(2026-10-01, external review -- "I wouldn't accept the funnel-completeness
claim without seeing the actual output granularity").

Confirmed two real problems while investigating that challenge:

1. _last_gate_rejection was only ever set by the single-leg pipeline's
   RVOL/ADX/RS/MTF checks (added 2026-09-16) -- credit_spread_v1/
   iron_condor_v1's own gates (VIX, IV Rank, VWAP/PCR direction, ADX,
   event calendar, net credit, margin) never set it at all, so their
   rejections genuinely only had last_gate_reached (which gate), not a
   specific reason -- exactly the gap the review suspected.

2. Worse, and NOT suspected by the review: _record_signal_trace()'s
   consumption of _last_gate_rejection required
   `_rej.get("gate") == last_gate` -- but last_gate_reached is ALWAYS the
   last-PASSED gate (e.g. "dte_passed" when RVOL fails), while
   _last_gate_rejection["gate"] is ALWAYS the gate that just FAILED (e.g.
   "rvol_passed"). Those names can never be equal by construction, so
   `detail` was silently None for EVERY rejected candidate, on EVERY
   strategy (including the single-leg gates that were believed to already
   work), since 2026-09-16. See test_signal_decision_trace_2026_09_15.py
   and test_rich_gate_rejection_detail_2026_09_16.py for the fix to that
   matching bug (live_trading_engine.py's _record_signal_trace()).

This file covers the first gap: credit_spread_v1/iron_condor_v1 now set
_last_gate_rejection at every one of their own rejection points, same
pattern as the single-leg gates. Source-level checks, same convention
already established for these two methods (see
test_spread_condor_otm_interval_fix_2026_09_04.py's docstring: too deep a
precondition chain to drive end-to-end with a mock).
"""
import inspect

from src.live_trading.live_trading_engine import LiveTradingEngine


def _rejection_sites(src: str) -> list:
    """Every `self._last_gate_rejection = {...}` assignment's gate/reason pair."""
    import re
    return re.findall(r'"gate":\s*"([a-z_]+)",.*?"reason":\s*"([A-Z0-9_]+)"', src, re.DOTALL)


def test_credit_spread_sets_rejection_detail_at_every_gate():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    sites = _rejection_sites(src)
    reasons = {reason for _, reason in sites}
    expected = {
        "VIX_UNAVAILABLE", "VIX_TOO_LOW",
        "IV_RANK_UNAVAILABLE", "IV_RANK_TOO_LOW",
        "BULL_PUT_BELOW_VWAP", "BEAR_CALL_ABOVE_VWAP",
        "ADX_UNAVAILABLE", "ADX_TOO_LOW_CONDOR_TERRITORY", "ADX_TOO_HIGH_BLOWTHROUGH_RISK",
        "EVENT_WITHIN_5_DAYS",
        "NET_CREDIT_BELOW_FEE_FLOOR", "RISK_REWARD_TOO_POOR",
        "INSUFFICIENT_MARGIN",
    }
    missing = expected - reasons
    assert not missing, f"credit_spread_v1 is missing rejection detail for: {missing}"


def test_iron_condor_sets_rejection_detail_at_every_gate():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    sites = _rejection_sites(src)
    reasons = {reason for _, reason in sites}
    expected = {
        "VIX_UNAVAILABLE", "VIX_TOO_LOW",
        "IV_RANK_UNAVAILABLE", "IV_RANK_TOO_LOW",
        "PCR_TOO_DIRECTIONAL",
        "ADX_UNAVAILABLE", "ADX_TOO_HIGH_TRENDING",
        "EVENT_WITHIN_5_DAYS",
        "NET_CREDIT_BELOW_FEE_FLOOR", "PUT_WING_RISK_REWARD_TOO_POOR", "CALL_WING_RISK_REWARD_TOO_POOR",
        "INSUFFICIENT_MARGIN",
    }
    missing = expected - reasons
    assert not missing, f"iron_condor_v1 is missing rejection detail for: {missing}"


def test_every_rejection_site_is_followed_by_a_return_within_a_few_lines():
    """Each _last_gate_rejection assignment must lead to this candidate's
    processing stopping shortly after -- a rejection reason attached to the
    WRONG candidate (because execution kept going) would be worse than none.
    A few VIX/IV-Rank sites sit in an if/else branch sharing one `return`
    right after both arms, not literally the next line -- so this checks
    "within a short window", not "the immediate next line"."""
    for method in (LiveTradingEngine._process_credit_spread, LiveTradingEngine._process_iron_condor):
        src = inspect.getsource(method)
        idx = 0
        count = 0
        while True:
            idx = src.find("self._last_gate_rejection = {", idx)
            if idx == -1:
                break
            count += 1
            close_idx = src.index("}", idx)
            after = src[close_idx + 1: close_idx + 450]
            assert "return" in after, (
                f"{method.__name__}: a _last_gate_rejection assignment at offset {idx} "
                "has no return within the next 450 chars"
            )
            idx = close_idx
        assert count >= 11, f"{method.__name__}: expected at least 11 rejection sites, found {count}"
