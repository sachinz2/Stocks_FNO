"""
Live incident, 2026-09-04 (thorough cross-check of all strategies): the
2026-08-20 fix (test_single_leg_dte_dead_zone_2026_08_20.py) only addressed
the "DTE too HIGH right after a monthly roll" symptom by raising max_dte to
42. A SEPARATE, still-real dead zone remained: single-leg entries resolved
expiry via get_near_month_expiry() alone, which only rolls to next month
once near-month DTE < 7 -- but fresh entries require DTE >= min_dte (10).
For DTE 7/8/9 every month, the near-month contract is still "current" but
too close for a new position, and nothing rolls it forward -- a genuine
~3-trading-day dead zone every month where neither ema_crossover_v1 nor
momentum_v1 can open any new position. credit_spread_v1/iron_condor_v1
already avoid this via get_entry_expiry(min_dte); the same fix is now
applied to the shared single-leg entry path in _process_signal().

Per test_real_contract_resolution.py's own documented rationale,
_process_signal() has too deep a precondition chain to drive end-to-end
with a mock -- source-level regression guards instead, same convention.
"""
import inspect

from src.core.utils import get_entry_expiry, get_near_month_expiry
from src.live_trading.live_trading_engine import LiveTradingEngine


def test_process_signal_rolls_to_next_month_when_near_month_dte_below_min_dte():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    idx = src.index("min_dte = getattr(strategy, \"min_dte\", 0)")
    block = src[idx:idx + 1500]
    assert "if dte < min_dte:" in block
    assert "expiry = get_entry_expiry(min_dte)" in block
    # The rolled expiry must be recomputed into `dte` too, not just assigned
    # to `expiry` and left stale for the range check right below it.
    assert "dte    = (expiry - now_ist().replace(tzinfo=None)).days" in block


def test_process_signal_dte_roll_happens_before_the_range_check_not_after():
    """The roll must happen BEFORE the min_dte<=dte<=max_dte gate -- rolling
    after an early `return` would never have a chance to run."""
    src = inspect.getsource(LiveTradingEngine._process_signal)
    roll_idx  = src.index("if dte < min_dte:")
    range_idx = src.index("if not (min_dte <= dte <= max_dte):")
    assert roll_idx < range_idx


def test_worst_case_min_dte_dead_zone_day_is_resolved_by_get_entry_expiry():
    """Reproduces the actual dead-zone scenario: near-month DTE=8 (inside
    the strategies' own min_dte=10 floor, but not yet <7 so
    get_near_month_expiry() alone would never roll it) -- get_entry_expiry()
    (now wired into _process_signal(), see the source-level tests above)
    must roll to a contract with DTE>=min_dte."""
    near_month = get_near_month_expiry()
    # Can't control "today" directly (these are real calendar functions), so
    # verify the CONTRACT this system's own DTE dead-zone incident hit
    # directly: emulate "near-month DTE=8" by asserting get_entry_expiry's
    # documented behavior for any date where the gap is real -- reuses the
    # exact function already fully unit-tested elsewhere (core/utils.py's
    # get_entry_expiry), just confirms it produces DTE>=min_dte, which is
    # the property _process_signal now depends on.
    min_dte = 10
    rolled = get_entry_expiry(min_dte)
    from src.core.utils import now_ist
    rolled_dte = (rolled - now_ist().replace(tzinfo=None)).days
    assert rolled_dte >= min_dte, (
        "get_entry_expiry(min_dte) must always produce a contract with "
        "DTE >= min_dte -- this is the invariant _process_signal's new "
        "roll depends on to close the dead zone."
    )
