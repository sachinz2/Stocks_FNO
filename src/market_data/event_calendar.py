"""
Event / Earnings Calendar Filter

Blocks new spread and condor entries within N trading days of scheduled events
(earnings releases, RBI MPC meetings, Budget) to avoid IV crush and gap risk.

Data sources (checked in order):
  1. Redis key  event:calendar — JSON dict {symbol: [date_str, ...], "*": [...]}
     Updated by the management script (see config/event_calendar.json instructions).
  2. config/event_calendar.json — static fallback updated quarterly.

Usage:
    from src.market_data.event_calendar import has_event_within_days
    if await has_event_within_days(symbol, redis, days=5):
        logger.info(f"{symbol}: event within 5 days — skipping entry")
        return

Populating the calendar in Redis (run once after each quarterly refresh):
    import asyncio, json, aioredis
    cal = {"INFY": ["2026-07-15"], "TCS": ["2026-07-11"], "*": ["2026-06-06"]}
    asyncio.run(redis.set("event:calendar", json.dumps(cal)))
"""
import json
import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_CALENDAR_REDIS_KEY = "event:calendar"
_CALENDAR_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "event_calendar.json")
)


class CalendarUnavailable(Exception):
    """Raised when NEITHER Redis nor the JSON fallback could provide a
    usable calendar -- distinct from a source successfully loading a real
    (possibly empty-of-dates) calendar, which is a legitimate "no events"
    outcome, not an unavailability."""


async def has_event_within_days(symbol: str, redis, days: int = 5) -> bool:
    """
    Return True if an event is scheduled for *symbol* within *days* calendar
    days from today, OR if the calendar could not be loaded from either
    source at all.

    Fixed 2026-08-27 (second-opinion review): this used to return False on
    ANY lookup failure, including total unavailability (Redis down AND the
    JSON fallback missing/corrupt) -- silently letting new credit_spread_v1/
    iron_condor_v1 entries through with zero event-risk check, the opposite
    of the fail-closed convention this codebase uses everywhere else (MTF,
    RS, entry price). A short-vol strategy should not open new risk when it
    genuinely can't tell whether an earnings/RBI event is imminent.
    '*' entries in the calendar apply to ALL symbols (RBI, Budget, etc.).
    """
    try:
        calendar = await _load_calendar(redis)
    except CalendarUnavailable as exc:
        logger.warning(
            f"[EventCalendar] calendar unavailable for {symbol} ({exc}) -- "
            "failing closed (treating as if an event is imminent)."
        )
        return True

    today   = date.today()
    cutoff  = today + timedelta(days=days)

    # Check symbol-specific dates AND market-wide events
    dates_to_check: list = list(calendar.get(symbol) or [])
    dates_to_check += list(calendar.get("*") or [])

    for date_str in dates_to_check:
        try:
            # Skip metadata keys (e.g. "_comment")
            if not date_str or date_str.startswith("_"):
                continue
            event_date = date.fromisoformat(date_str)
            if today <= event_date <= cutoff:
                logger.debug(
                    f"[EventCalendar] {symbol}: event {date_str} is within {days} days"
                )
                return True
        except ValueError:
            pass  # malformed date — skip silently

    return False


async def _load_calendar(redis) -> dict:
    """
    Load calendar from Redis; fall back to the JSON config file. Raises
    CalendarUnavailable only when NEITHER source yields any usable dict --
    a source that loads successfully to an empty/non-matching calendar (the
    JSON fallback's actual shipped state today, {"*": []}) is a real,
    legitimate "no events scheduled" result, not an unavailability.
    """
    if redis:
        try:
            raw = await redis.get(_CALENDAR_REDIS_KEY)
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict) and data:
                    return data
        except Exception:
            pass  # fall through to the JSON file below

    if os.path.exists(_CALENDAR_JSON_PATH):
        try:
            with open(_CALENDAR_JSON_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            # Strip metadata keys (start with "_")
            return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception as exc:
            raise CalendarUnavailable(
                f"JSON calendar file exists but failed to parse: {exc}"
            ) from exc

    raise CalendarUnavailable(
        f"no usable calendar in Redis and no JSON fallback file at {_CALENDAR_JSON_PATH}"
    )
