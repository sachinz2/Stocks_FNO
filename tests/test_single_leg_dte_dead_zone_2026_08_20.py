"""
Live incident, 2026-08-20: ema_crossover_v1 and momentum_v1 placed ZERO
orders from 2026-08-17 through 2026-08-20 (and, traced back, the same gap
happened 2026-07-25..08-05). Root cause: _process_signal() resolves the
entry expiry via get_near_month_expiry(), which only rolls to next month
once DTE<7 (unlike credit_spread/iron_condor's get_entry_expiry(), which
rolls early specifically to avoid this). Right after that roll, the fresh
contract's DTE can be as high as 41 (6 days left on the old contract + up
to a 35-day gap to the next monthly expiry) -- which sat above the old
max_dte=25, blocking every single-leg entry until DTE decayed back down.

Fix: raise max_dte to 42, comfortably covering the worst case. These tests
guard the underlying invariant (not just the literal "42") so a future
change to the NSE expiry calendar or the roll trigger would be caught here
instead of silently reintroducing the same monthly outage.
"""
import calendar
from datetime import datetime, timedelta

from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.momentum import MomentumStrategy
from src.core.utils import _last_expiry_weekday


def _worst_case_fresh_dte_after_roll() -> int:
    """
    Mirrors get_near_month_expiry()'s roll trigger (DTE<7, i.e. the latest
    possible pre-roll DTE is 6) plus the largest real gap between two
    consecutive NSE monthly expiries, walked across two full years to catch
    every month length / weekday-alignment combination.
    """
    worst = 0
    for year in (2026, 2027):
        for month in range(1, 13):
            e1 = _last_expiry_weekday(year, month)
            next_month, next_year = (month + 1, year) if month < 12 else (1, year + 1)
            e2 = _last_expiry_weekday(next_year, next_month)
            fresh_dte = 6 + (e2 - e1).days
            worst = max(worst, fresh_dte)
    return worst


def test_worst_case_fresh_dte_fits_within_ema_crossover_max_dte():
    strategy = EMACrossoverStrategy("ema_test", {})
    strategy.initialize()
    assert _worst_case_fresh_dte_after_roll() <= strategy.max_dte, (
        "max_dte no longer covers the worst-case DTE right after a monthly "
        "roll -- this would reintroduce the 2026-08-17..08-20 dead zone."
    )


def test_worst_case_fresh_dte_fits_within_momentum_max_dte():
    strategy = MomentumStrategy("mom_test", {})
    strategy.initialize()
    assert _worst_case_fresh_dte_after_roll() <= strategy.max_dte, (
        "max_dte no longer covers the worst-case DTE right after a monthly "
        "roll -- this would reintroduce the 2026-08-17..08-20 dead zone."
    )


def test_ema_crossover_dte_window_defaults():
    strategy = EMACrossoverStrategy("ema_test", {})
    strategy.initialize()
    assert strategy.min_dte == 10
    assert strategy.max_dte == 42


def test_momentum_dte_window_defaults():
    strategy = MomentumStrategy("mom_test", {})
    strategy.initialize()
    assert strategy.min_dte == 10
    assert strategy.max_dte == 42


def test_reproduces_the_actual_live_incident_dte_40():
    # The exact real-world number seen live on 2026-08-19 ("[momentum_v1]
    # DTE=40 outside [10,25] -- skipping entry for BPCL") must now fall
    # inside the window for both strategies.
    for strategy in (EMACrossoverStrategy("ema_test", {}), MomentumStrategy("mom_test", {})):
        strategy.initialize()
        assert strategy.min_dte <= 40 <= strategy.max_dte
