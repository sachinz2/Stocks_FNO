"""
ZerodhaTicker.seconds_since_last_tick() -- the core logic behind the
tick-staleness watchdog in src/api/main.py's _kite_self_heal job.
"""
import datetime as dt
import pytz
import pytest

from src.market_data.zerodha_ticker import ZerodhaTicker

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def frozen_now(monkeypatch):
    # zerodha_ticker.py does `from src.core.utils import now_ist` INSIDE each
    # method (not at module level), so there's no module-level attribute to
    # patch there -- patch the source in src.core.utils instead, which the
    # local import re-resolves at call time.
    state = {"t": IST.localize(dt.datetime(2026, 7, 31, 10, 0, 0))}

    def _now_ist():
        return state["t"]

    monkeypatch.setattr("src.core.utils.now_ist", _now_ist)
    return state


def _bare_ticker():
    """Skip __init__ (no real redis/kite connection needed for this logic)."""
    t = ZerodhaTicker.__new__(ZerodhaTicker)
    t._connected_at = None
    t._last_tick_at = None
    return t


def test_never_connected_returns_none():
    t = _bare_ticker()
    assert t.seconds_since_last_tick() is None


def test_connected_no_tick_yet_measures_from_connect_time(frozen_now):
    t = _bare_ticker()
    t._connected_at = frozen_now["t"]
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 2, 0))  # +120s
    assert abs(t.seconds_since_last_tick() - 120) < 0.01


def test_tick_arrival_switches_reference_to_last_tick(frozen_now):
    t = _bare_ticker()
    t._connected_at = frozen_now["t"]
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 2, 0))
    t._last_tick_at = frozen_now["t"]
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 2, 30))  # +30s since tick
    assert abs(t.seconds_since_last_tick() - 30) < 0.01


def test_reproduces_the_2026_07_31_dead_reactor_incident(frozen_now):
    # connected long ago, no tick ever, 2+ hours pass -> watchdog threshold trips
    t = _bare_ticker()
    t._connected_at = IST.localize(dt.datetime(2026, 7, 31, 8, 30, 22))
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 23, 33))  # ~1h53m later
    stale_for = t.seconds_since_last_tick()
    TICK_STALE_THRESHOLD_SECONDS = 180
    assert stale_for > TICK_STALE_THRESHOLD_SECONDS


def test_watchdog_exits_process_on_staleness_not_inplace_reconnect():
    """
    Fixed 2026-08-07: the watchdog in api/main.py's _kite_self_heal used to
    call ticker.stop(); ticker.start() on staleness -- an in-place reconnect
    that repeated the exact same silent-failure pattern for 27+ minutes on
    2026-08-07 (KiteTicker's Twisted reactor can only run once per process;
    stop()/start() leaves a dead reactor that never processes another
    on_connect/on_error). It must call os._exit(1) instead, so
    docker-compose's `restart: unless-stopped` brings up a genuinely fresh
    process. os._exit() itself can't be exercised in a test process (it
    would kill the test runner), so this checks the source directly.
    """
    from pathlib import Path

    main_py = Path(__file__).resolve().parent.parent / "src" / "api" / "main.py"
    main_src = main_py.read_text(encoding="utf-8")

    start = main_src.index("async def _kite_self_heal")
    end = main_src.index("scheduler.add_job(\n        _kite_self_heal")
    body = main_src[start:end]

    assert "os._exit(1)" in body, "watchdog must force a full process exit on staleness"
    assert "ticker.stop()\n                    ticker.start()" not in body, (
        "old in-place reconnect (proven insufficient) must not be present"
    )
