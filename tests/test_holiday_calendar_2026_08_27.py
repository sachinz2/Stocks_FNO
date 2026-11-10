"""
Live incident (2026-08-27): zero trades all day despite a genuine trading
session. Root cause -- _init_nse_holiday_checker() requested calendar code
"XNSE" from exchange_calendars, which does not exist in the installed
version (4.13.2 only ships "BSE"/"XBSE", confirmed via
ecals.get_calendar_names()). Init silently failed every single day (not a
transient blip) and fell back to the hardcoded _NSE_HOLIDAYS_FALLBACK list,
which incorrectly flags 2026-08-27 as "Janmashtami" -- verified wrong
against the real dynamic XBSE calendar (is_session() returns True for that
date). is_market_open() returned False all day, so run_signal_cycle()
silently no-op'd every cycle -- zero regime detection, zero candidates,
zero trades, and zero error output (a holiday is meant to be a quiet
no-op path, which is exactly what made this hard to notice).

The mocked test below fakes BOTH exchange_calendars and pandas -- letting
the real pandas import through here hits an unrelated numpy C-extension
guard ("cannot load module more than once per process") when this function
is invoked more than once across the test session, which is a test-harness
artifact of re-importing numpy's compiled extension via sys.modules
patching, not a real production concern (confirmed directly against the
actual production container, which only ever imports these once at cold
start).
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


def test_holiday_checker_requests_xbse_and_defers_to_the_dynamic_calendar():
    """Regression guard for the actual incident: the calendar code passed to
    exchange_calendars.get_calendar() must be "XBSE" (the nonexistent
    "XNSE" silently broke init every day), and the resulting checker must
    defer to the dynamic calendar's is_session() rather than falling back
    to the hardcoded list for a date the hardcoded list gets wrong (exactly
    2026-08-27, real trading day, wrongly flagged as "Janmashtami")."""
    fake_cal = MagicMock()
    fake_cal.is_session.return_value = True  # real trading day per the dynamic calendar

    fake_ecals = MagicMock()
    fake_ecals.get_calendar.return_value = fake_cal

    fake_pd = MagicMock()
    fake_pd.Timestamp.side_effect = lambda d: d  # pass the date straight through

    with patch.dict("sys.modules", {"exchange_calendars": fake_ecals, "pandas": fake_pd}):
        from src.core import utils
        checker = utils._init_nse_holiday_checker()

    assert fake_ecals.get_calendar.call_args_list, "get_calendar() was never called"
    for c in fake_ecals.get_calendar.call_args_list:
        assert c.args == ("XBSE",), f"expected calendar code 'XBSE', got {c.args}"

    # Fixed 2026-11-10: the wrong "Janmashtami" entry that used to make this
    # case interesting (hardcoded list said holiday, real calendar said
    # trading day) has since been removed from _NSE_HOLIDAYS_FALLBACK
    # entirely -- see that incident's own comment there. This still
    # exercises the real regression (checker must defer to the dynamic
    # calendar), just without a hardcoded false-positive to override.
    assert checker(date(2026, 8, 27)) is False, (
        "a real trading session per the dynamic calendar must not be "
        "reported as a holiday"
    )


def test_real_installed_calendar_confirms_2026_08_27_is_a_trading_day():
    """Integration check against whatever exchange_calendars version is
    actually installed (production has 4.13.2) -- confirms the real,
    dynamic calendar (not the hardcoded list) agrees 2026-08-27 was a
    genuine trading day, and that surrounding weekend dates are still
    correctly flagged as non-sessions. Skipped where exchange_calendars
    itself isn't installed (e.g. local dev envs without it)."""
    pytest.importorskip("exchange_calendars", reason="exchange_calendars not installed in this environment")
    from src.core.utils import _init_nse_holiday_checker

    checker = _init_nse_holiday_checker()
    assert checker(date(2026, 8, 27)) is False
    assert checker(date(2026, 8, 29)) is True   # Saturday
    assert checker(date(2026, 8, 30)) is True   # Sunday


# ── Live incident, 2026-11-10: the mirror-image failure mode ───────────────
# Nov 10, 2026 is a real NSE/BSE holiday (Diwali Balipratipada), verified via
# WebSearch against NSE's own published 2026 holiday list. The INSTALLED
# exchange_calendars (same production version, 4.13.2) got this one wrong in
# the OPPOSITE direction from the 2026-08-27 incident above: XBSE's
# is_session() returned True (a trading day) for a date the real exchange
# was actually closed on. run_signal_cycle() ran all day as if the market
# were open, closing 2 real paper credit_spread_v1 positions off whatever
# stale/estimated pricing was available. Fix: _check() now unions the
# dynamic calendar with the (corrected) hardcoded list -- a holiday if
# EITHER source says so -- instead of exclusively trusting exchange_calendars
# whenever it initializes successfully.

def test_holiday_checker_catches_a_date_the_dynamic_calendar_misses():
    """Regression guard for the actual Nov 10 incident: even when the
    dynamic calendar wrongly reports a real holiday as a trading session,
    the (now-corrected) hardcoded list must still catch it via the union."""
    fake_cal = MagicMock()
    fake_cal.is_session.return_value = True  # XBSE wrongly says "trading day"

    fake_ecals = MagicMock()
    fake_ecals.get_calendar.return_value = fake_cal

    fake_pd = MagicMock()
    fake_pd.Timestamp.side_effect = lambda d: d

    with patch.dict("sys.modules", {"exchange_calendars": fake_ecals, "pandas": fake_pd}):
        from src.core import utils
        checker = utils._init_nse_holiday_checker()

    assert checker(date(2026, 11, 10)) is True, (
        "Nov 10, 2026 (Diwali Balipratipada) is a real NSE holiday -- the "
        "hardcoded list must catch it even though the mocked dynamic "
        "calendar wrongly reports it as a trading session"
    )


def test_holiday_checker_still_defers_to_the_dynamic_calendar_when_both_agree():
    """The union must not turn every date into a holiday -- a date neither
    source flags stays a genuine trading day."""
    fake_cal = MagicMock()
    fake_cal.is_session.return_value = True

    fake_ecals = MagicMock()
    fake_ecals.get_calendar.return_value = fake_cal

    fake_pd = MagicMock()
    fake_pd.Timestamp.side_effect = lambda d: d

    with patch.dict("sys.modules", {"exchange_calendars": fake_ecals, "pandas": fake_pd}):
        from src.core import utils
        checker = utils._init_nse_holiday_checker()

    assert checker(date(2026, 11, 11)) is False, "an ordinary trading day must not be flagged a holiday"


def test_real_installed_calendar_and_fallback_union_catch_2026_11_10():
    """Integration check: with the ACTUAL installed exchange_calendars
    (whatever XBSE itself currently believes) unioned with the corrected
    hardcoded list, Nov 10 2026 must come back as a holiday either way."""
    pytest.importorskip("exchange_calendars", reason="exchange_calendars not installed in this environment")
    from src.core.utils import _init_nse_holiday_checker

    checker = _init_nse_holiday_checker()
    assert checker(date(2026, 11, 10)) is True


def test_2026_08_27_is_not_resurrected_as_a_false_positive_by_the_union():
    """Regression guard: removing the wrong "Janmashtami" entry from
    _NSE_HOLIDAYS_FALLBACK (rather than just leaving it and relying on
    exchange_calendars to override it) matters now that _check() unions
    both sources -- if that entry were still present, the union would
    resurrect the exact 2026-08-27 incident this file exists to prevent."""
    from src.core.utils import _NSE_HOLIDAYS_FALLBACK
    assert date(2026, 8, 27) not in _NSE_HOLIDAYS_FALLBACK
