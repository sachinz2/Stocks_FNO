"""
Event Calendar Auto-Refresh

Fetches upcoming board meeting / quarterly results dates from NSE's public
event-calendar API and merges them with known RBI MPC dates. Writes the
result to both config/event_calendar.json and Redis (event:calendar).

Called from the engine every Monday at market open so the calendar stays
current without any manual work. Falls back gracefully if NSE API is down.

NSE API note: NSE occasionally changes its API paths or adds bot-detection.
If the fetch returns empty, the existing JSON / Redis calendar is kept intact.
"""
import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CALENDAR_JSON_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "event_calendar.json")
)
_CALENDAR_REDIS_KEY = "event:calendar"

# RBI MPC meeting announcement dates (decision day = last day of each meeting).
# Updated for FY 2026–27. Next update: March 2027 (when RBI announces FY27-28 calendar).
_RBI_MPC_DATES: List[str] = [
    # FY 2026–27 — source: RBI website (announced Feb 2026)
    "2026-08-07",   # Aug 5–7 2026
    "2026-10-08",   # Oct 6–8 2026
    "2026-12-05",   # Dec 3–5 2026
    "2027-02-05",   # Feb 3–5 2027
]

# Union Budget (announcement date)
_BUDGET_DATES: List[str] = [
    "2027-02-01",   # Union Budget FY 2027–28
]


async def refresh_calendar(redis=None) -> bool:
    """
    Fetch upcoming NSE results and merge with RBI/Budget dates.
    Writes to JSON + Redis. Returns True on success, False if NSE fetch failed
    (existing calendar untouched on failure).
    """
    loop = asyncio.get_event_loop()

    # Fetch NSE data in a thread (blocking HTTP)
    nse_data: Dict[str, List[str]] = await loop.run_in_executor(
        None, _fetch_nse_events
    )

    # Track whether NSE returned any stock-level data before we add metadata
    nse_had_stock_data = bool(nse_data)

    # Always include RBI MPC and Budget dates under the "*" market-wide key
    today = date.today()
    upcoming_global = [
        d for d in _RBI_MPC_DATES + _BUDGET_DATES
        if d >= today.isoformat()
    ]
    nse_data["*"] = sorted(set(upcoming_global))

    # Strip metadata keys and merge with any existing manual overrides in JSON
    existing = _load_existing_json()
    for sym, dates in existing.items():
        if sym.startswith("_"):
            continue
        if sym not in nse_data:
            nse_data[sym] = dates
        else:
            # Merge: keep dates from both sources, deduplicated
            merged = sorted(set(nse_data[sym]) | set(d for d in dates if d >= today.isoformat()))
            nse_data[sym] = merged

    # Add metadata for traceability
    nse_data["_refreshed"] = datetime.now().isoformat()
    nse_data["_source"]    = "NSE event-calendar API + hardcoded RBI/Budget"

    # Write to JSON
    try:
        with open(_CALENDAR_JSON_PATH, "w", encoding="utf-8") as fh:
            json.dump(nse_data, fh, indent=2)
        logger.info(
            f"[EventCalendar] JSON updated: {sum(1 for k in nse_data if not k.startswith('_'))} symbols"
        )
    except Exception as exc:
        logger.error(f"[EventCalendar] Failed to write JSON: {exc}")

    # Write to Redis (strip metadata keys to keep it lean)
    if redis:
        redis_payload = {k: v for k, v in nse_data.items() if not k.startswith("_")}
        try:
            await redis.set(
                _CALENDAR_REDIS_KEY,
                json.dumps(redis_payload),
                ex=8 * 86400,   # 8-day TTL — auto-expires if weekly refresh fails
            )
            logger.info("[EventCalendar] Redis updated")
        except Exception as exc:
            logger.error(f"[EventCalendar] Failed to write Redis: {exc}")

    # Return True only if NSE provided stock-level earnings data.
    # False means NSE was unreachable — existing calendar was left intact.
    return nse_had_stock_data


def _fetch_nse_events(days_ahead: int = 90) -> Dict[str, List[str]]:
    """
    Blocking — runs in thread executor.
    Tries two NSE endpoints for upcoming results/board-meeting dates.
    Returns {SYMBOL: [iso_date, ...]} or {} on failure.
    """
    try:
        import requests
    except ImportError:
        logger.warning("[EventCalendar] 'requests' not installed — skipping NSE fetch")
        return {}

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com/",
    })

    # Warm up the session — NSE requires a cookie from the homepage
    try:
        session.get("https://www.nseindia.com/", timeout=10)
    except Exception as exc:
        logger.warning(f"[EventCalendar] NSE homepage warm-up failed: {exc}")
        return {}

    today  = date.today()
    cutoff = today + timedelta(days=days_ahead)

    results: Dict[str, List[str]] = {}

    # Endpoint 1: NSE event calendar (all upcoming corporate events)
    _try_event_calendar(session, results, today, cutoff)

    # Endpoint 2: NSE corporate actions (board meetings per index)
    if not results:
        _try_corporate_actions(session, results, today, cutoff)

    total_dates = sum(len(v) for v in results.values())
    logger.info(
        f"[EventCalendar] NSE fetch complete: {len(results)} symbols, "
        f"{total_dates} upcoming dates"
    )
    return results


def _try_event_calendar(session, results: dict, today: date, cutoff: date) -> None:
    """Try NSE /api/event-calendar endpoint."""
    try:
        resp = session.get(
            "https://www.nseindia.com/api/event-calendar",
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("results") or []
        _parse_nse_rows(rows, results, today, cutoff)
    except Exception as exc:
        logger.debug(f"[EventCalendar] event-calendar endpoint failed: {exc}")


def _try_corporate_actions(session, results: dict, today: date, cutoff: date) -> None:
    """Try NSE /api/corporates-corporateActions for the equity index."""
    try:
        resp = session.get(
            "https://www.nseindia.com/api/corporates-corporateActions?index=equities",
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("results") or []
        _parse_nse_rows(rows, results, today, cutoff)
    except Exception as exc:
        logger.debug(f"[EventCalendar] corporate-actions endpoint failed: {exc}")


def _parse_nse_rows(rows: list, results: dict, today: date, cutoff: date) -> None:
    """Parse an NSE API row list into {SYMBOL: [iso_date]} entries."""
    _RESULT_KEYWORDS = ("result", "board meeting", "quarterly", "annual", "financial")
    _DATE_FIELDS     = ("date", "bcastDate", "exDate", "recDate", "setDate", "ndDate")
    _DATE_FORMATS    = ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%Y-%m-%d", "%d-%B-%Y")

    for item in (rows or []):
        if not isinstance(item, dict):
            continue

        symbol  = (item.get("symbol") or item.get("Symbol") or "").upper().strip()
        purpose = (
            item.get("purpose") or item.get("description") or
            item.get("subject") or item.get("Purpose") or ""
        ).lower()

        if not symbol or not any(kw in purpose for kw in _RESULT_KEYWORDS):
            continue

        # Try to parse a date from any known field
        event_date: Optional[date] = None
        for field in _DATE_FIELDS:
            raw = (item.get(field) or "").strip().rstrip("*")
            if not raw or raw in ("-", "NA", "N.A."):
                continue
            for fmt in _DATE_FORMATS:
                try:
                    event_date = datetime.strptime(raw, fmt).date()
                    break
                except ValueError:
                    continue
            if event_date:
                break

        if event_date and today <= event_date <= cutoff:
            iso = event_date.isoformat()
            results.setdefault(symbol, [])
            if iso not in results[symbol]:
                results[symbol].append(iso)


def _load_existing_json() -> dict:
    try:
        if os.path.exists(_CALENDAR_JSON_PATH):
            with open(_CALENDAR_JSON_PATH, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return {}
