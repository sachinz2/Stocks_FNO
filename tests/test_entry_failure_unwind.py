"""
Multi-leg entry-failure unwind: if a later leg of a credit spread or iron
condor fails to place (rejected/cancelled/failed), the already-placed
leg(s) must be immediately closed out rather than left as a naked,
unintended position -- a naked short leg without its hedge is a
fundamentally different, much riskier trade than the spread/condor that
was actually intended.

This logic is embedded inline inside _process_credit_spread() and
_process_iron_condor(), both of which have a long precondition chain
(breadth/ADX/VWAP/DTE/IV-rank/PCR/strike-selection checks) before reaching
the entry itself -- driving the full method end-to-end would need an
extremely large, fragile mock of all of that. These are source-level
regression guards instead: they fail loudly if a future edit removes the
unwind path, drops is_exit_order=True on an unwind order (silently
reintroducing the kill-switch-bypass gap for THIS specific path), or
removes the operator-facing critical alert for the worst case (the unwind
itself failing, leaving a genuinely naked position).
"""
import inspect
from src.live_trading.live_trading_engine import LiveTradingEngine


def test_credit_spread_unwinds_short_leg_when_long_leg_fails():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)

    assert "Unwinding short" in src
    # The unwind order (buying back the short leg) must bypass the kill
    # switch/circuit breaker -- same reasoning as any other exit.
    unwind_block = src[src.index("Unwinding short"):src.index("Unwinding short") + 600]
    assert "is_exit_order=True" in unwind_block

    # Worst case: the unwind itself fails -- a naked short must trigger a
    # loud, actionable operator alert, not a silent log line.
    assert "UNWIND FAILED" in src
    assert "MANUAL INTERVENTION REQUIRED" in src
    assert "await self._notify(" in src


def test_iron_condor_unwinds_already_placed_legs_when_a_later_leg_fails():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)

    assert "Unwinding" in src
    unwind_block = src[src.index("for (c, s, p, _) in placed"):]
    assert "is_exit_order=True" in unwind_block

    assert "UNWIND FAILED" in src
    assert "MANUAL INTERVENTION REQUIRED" in src
    assert "await self._notify(" in src


def test_credit_spread_short_leg_rejected_by_risk_stops_retrying_today():
    # If the risk manager itself blocks the FIRST (short) leg, there's
    # nothing to unwind -- but the symbol must be marked so the engine
    # doesn't hammer the same rejected entry every cycle for the rest of
    # the day.
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("Short leg rejected")
    nearby = src[idx:idx + 300]
    assert '_exited_today.add(symbol)' in nearby
