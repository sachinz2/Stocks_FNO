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

    assert checker(date(2026, 8, 27)) is False, (
        "a real trading session per the dynamic calendar must not be "
        "reported as a holiday, even though the hardcoded fallback list "
        "incorrectly flags this specific date"
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
