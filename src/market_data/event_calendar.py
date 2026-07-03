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


async def has_event_within_days(symbol: str, redis, days: int = 5) -> bool:
    """
    Return True if an event is scheduled for *symbol* within *days* calendar
    days from today.  '*' entries in the calendar apply to ALL symbols (RBI,
    Budget, etc.).  Returns False on any lookup failure so entries are never
    blocked by a calendar data issue.
    """
    try:
        calendar = await _load_calendar(redis)
        if not calendar:
            return False

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

    except Exception as exc:
        logger.debug(f"[EventCalendar] lookup failed for {symbol}: {exc}")

    return False


async def _load_calendar(redis) -> dict:
    """Load calendar from Redis; fall back to the JSON config file."""
    if redis:
        try:
            raw = await redis.get(_CALENDAR_REDIS_KEY)
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict) and data:
                    return data
        except Exception:
            pass

    try:
        if os.path.exists(_CALENDAR_JSON_PATH):
            with open(_CALENDAR_JSON_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            # Strip metadata keys (start with "_")
            return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as exc:
        logger.debug(f"[EventCalendar] could not load JSON calendar: {exc}")

    return {}
