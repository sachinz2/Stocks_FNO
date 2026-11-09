"""
LTPPoller._prefetch_stale_histories() (2026-08-20).

At the current 41-symbol universe, poll()'s per-symbol loop calling
kite.historical_data() one at a time comfortably fit the 60s signal-cycle
budget (measured ~9s for a full cold-start burst). Scaling toward the real
208-symbol NSE F&O universe (see FNO_SECTORS' 2026-08-20 comment), a cold
start hits ~48s sequentially for the 5-min OHLC alone -- close enough to the
60s budget that one slow Zerodha response could push a scheduled cycle past
misfire_grace_time=30s and get it skipped (APScheduler default
max_instances=1 means a missed cycle is skipped, not queued).
_prefetch_stale_histories() fetches every stale symbol concurrently (bounded
by a semaphore, not full-parallel, to stay well under Zerodha's request-rate
limits) before poll()'s main per-symbol loop runs.
"""
import asyncio
import datetime as dt

import pandas as pd
import pytest

from src.market_data.ltp_poller import LTPPoller


def _df():
    dates = pd.date_range("2026-08-01", periods=30, freq="5min")
    return pd.DataFrame({
        "date": dates, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000,
    })


@pytest.mark.asyncio
async def test_fetches_only_stale_symbols_concurrently():
    poller = LTPPoller(redis_client=None, symbols=["A", "B", "C"], kite=object(),
                        instrument_tokens={"A": 1, "B": 2, "C": 3})
    # B already has a fresh cache -- must NOT be refetched.
    poller._history_loaded_at["B"] = dt.datetime.now()
    poller._history["B"] = _df()

    fetched = []

    def _fake_fetch(symbol):
        fetched.append(symbol)
        return _df()

    loop = asyncio.get_running_loop()
    await poller._prefetch_stale_histories(
        poller.symbols, loop, _fake_fetch, poller._history_loaded_at, poller._history, 300,
    )

    assert sorted(fetched) == ["A", "C"]
    assert "A" in poller._history and "C" in poller._history
    assert poller._history["B"] is not None  # untouched, still the pre-seeded df


@pytest.mark.asyncio
async def test_concurrency_is_bounded_not_full_parallel():
    poller = LTPPoller(redis_client=None, symbols=[f"S{i}" for i in range(20)], kite=object(),
                        instrument_tokens={f"S{i}": i for i in range(20)})
    poller._MAX_CONCURRENT_OHLC_FETCHES = 3

    # fetch_fn runs inside run_in_executor (a real thread), so a real
    # threading.Lock + time.sleep is needed to observe genuine overlap --
    # an asyncio.Lock wouldn't be touched by the executor threads.
    import threading
    import time
    lock = threading.Lock()
    counters = {"current": 0, "peak": 0}

    def _tracked_fetch(symbol):
        with lock:
            counters["current"] += 1
            counters["peak"] = max(counters["peak"], counters["current"])
        time.sleep(0.05)
        with lock:
            counters["current"] -= 1
        return _df()

    loop = asyncio.get_running_loop()
    await poller._prefetch_stale_histories(
        poller.symbols, loop, _tracked_fetch, poller._history_loaded_at, poller._history, 300,
    )

    assert counters["peak"] <= 3, f"expected at most 3 concurrent fetches, saw {counters['peak']}"
    assert len(poller._history) == 20


@pytest.mark.asyncio
async def test_one_symbol_failing_does_not_block_others():
    poller = LTPPoller(redis_client=None, symbols=["GOOD1", "BAD", "GOOD2"], kite=object(),
                        instrument_tokens={"GOOD1": 1, "BAD": 2, "GOOD2": 3})

    def _fake_fetch(symbol):
        if symbol == "BAD":
            raise RuntimeError("Zerodha timeout")
        return _df()

    loop = asyncio.get_running_loop()
    await poller._prefetch_stale_histories(
        poller.symbols, loop, _fake_fetch, poller._history_loaded_at, poller._history, 300,
    )

    assert "GOOD1" in poller._history
    assert "GOOD2" in poller._history
    assert "BAD" not in poller._history
    # BAD's loaded_at must still be stamped so it isn't retried every cycle
    # (matches _get_history()'s existing "on fetch failure, still update the
    # timestamp" contract).
    assert "BAD" in poller._history_loaded_at


# ── HISTORY_RETRY_SECONDS short-circuit for symbols with no real data yet ──
# Fixed 2026-11-09 (live incident, recurring): kite.historical_data() can
# come back empty WITHOUT raising (plausible for a throttled response right
# after a rate-limit burst), and both call sites stamp *_loaded_at=now
# regardless -- a symbol with zero real data cached then had to wait the
# FULL refresh_seconds (300s/900s) before the next attempt, repeating
# indefinitely if later attempts also came back empty. Confirmed live twice
# in one day on force-tracked open-position underlyings (TORNTPHARM/ITC/
# JIOFIN), both times right after a container restart's cold-start burst.

@pytest.mark.asyncio
async def test_symbol_with_no_data_yet_retries_sooner_than_the_full_refresh_window():
    from src.market_data.ltp_poller import HISTORY_RETRY_SECONDS
    poller = LTPPoller(redis_client=None, symbols=["NODATA"], kite=object(),
                        instrument_tokens={"NODATA": 1})
    # Simulate a prior failed/empty attempt: loaded_at stamped, no real data cached.
    poller._history_loaded_at["NODATA"] = dt.datetime.now() - dt.timedelta(seconds=HISTORY_RETRY_SECONDS + 1)

    fetched = []

    def _fake_fetch(symbol):
        fetched.append(symbol)
        return _df()

    loop = asyncio.get_running_loop()
    await poller._prefetch_stale_histories(
        poller.symbols, loop, _fake_fetch, poller._history_loaded_at, poller._history, 300,
    )

    assert fetched == ["NODATA"], (
        "a symbol with no real data yet must retry after HISTORY_RETRY_SECONDS, "
        "not wait the full 300s refresh_seconds"
    )


@pytest.mark.asyncio
async def test_symbol_with_real_data_waits_the_full_refresh_window_not_the_short_retry():
    from src.market_data.ltp_poller import HISTORY_RETRY_SECONDS
    poller = LTPPoller(redis_client=None, symbols=["HASDATA"], kite=object(),
                        instrument_tokens={"HASDATA": 1})
    poller._history["HASDATA"] = _df()
    # Past the short retry window but well within the full 300s refresh window.
    poller._history_loaded_at["HASDATA"] = dt.datetime.now() - dt.timedelta(seconds=HISTORY_RETRY_SECONDS + 1)

    fetched = []

    def _fake_fetch(symbol):
        fetched.append(symbol)
        return _df()

    loop = asyncio.get_running_loop()
    await poller._prefetch_stale_histories(
        poller.symbols, loop, _fake_fetch, poller._history_loaded_at, poller._history, 300,
    )

    assert fetched == [], (
        "a symbol that already has real cached data must wait the full "
        "300s refresh_seconds, not the short HISTORY_RETRY_SECONDS"
    )


@pytest.mark.asyncio
async def test_get_history_retries_sooner_when_no_data_cached_yet():
    """Same fix, direct call site: _get_history() (not the concurrent
    prefetch helper) is what poll()'s main per-symbol loop actually calls."""
    from src.market_data.ltp_poller import HISTORY_RETRY_SECONDS
    poller = LTPPoller(redis_client=None, symbols=["NODATA"], kite=object(),
                        instrument_tokens={"NODATA": 1})
    poller._history_loaded_at["NODATA"] = dt.datetime.now() - dt.timedelta(seconds=HISTORY_RETRY_SECONDS + 1)
    poller._fetch_kite_ohlc = lambda symbol: _df()

    loop = asyncio.get_running_loop()
    result = await poller._get_history("NODATA", loop)

    assert result is not None and not result.empty


@pytest.mark.asyncio
async def test_get_history_does_not_retry_early_once_real_data_is_cached():
    from src.market_data.ltp_poller import HISTORY_RETRY_SECONDS
    poller = LTPPoller(redis_client=None, symbols=["HASDATA"], kite=object(),
                        instrument_tokens={"HASDATA": 1})
    poller._history["HASDATA"] = _df()
    poller._history_loaded_at["HASDATA"] = dt.datetime.now() - dt.timedelta(seconds=HISTORY_RETRY_SECONDS + 1)

    calls = []
    poller._fetch_kite_ohlc = lambda symbol: calls.append(symbol) or _df()

    loop = asyncio.get_running_loop()
    await poller._get_history("HASDATA", loop)

    assert calls == [], "must not re-fetch within the full 300s window once real data is cached"


@pytest.mark.asyncio
async def test_skips_symbols_without_kite_or_token():
    poller = LTPPoller(redis_client=None, symbols=["HASTOKEN", "NOTOKEN"], kite=object(),
                        instrument_tokens={"HASTOKEN": 1})  # NOTOKEN has no token entry

    fetched = []

    def _fake_fetch(symbol):
        fetched.append(symbol)
        return _df()

    loop = asyncio.get_running_loop()
    await poller._prefetch_stale_histories(
        poller.symbols, loop, _fake_fetch, poller._history_loaded_at, poller._history, 300,
    )

    assert fetched == ["HASTOKEN"]
    assert "NOTOKEN" not in poller._history_loaded_at  # left for _get_history()'s own no-token branch


@pytest.mark.asyncio
async def test_no_op_when_nothing_is_stale():
    poller = LTPPoller(redis_client=None, symbols=["A"], kite=object(), instrument_tokens={"A": 1})
    poller._history_loaded_at["A"] = dt.datetime.now()
    poller._history["A"] = _df()

    called = []

    def _fake_fetch(symbol):
        called.append(symbol)
        return _df()

    loop = asyncio.get_running_loop()
    await poller._prefetch_stale_histories(
        poller.symbols, loop, _fake_fetch, poller._history_loaded_at, poller._history, 300,
    )

    assert called == []


def test_poll_calls_prefetch_for_both_5min_and_15min_before_main_loop():
    # poll() has too many live dependencies (market-hours gate, real kite
    # calls, Redis writes) to drive behaviorally in a unit test -- static-
    # source regression guard, matching this project's convention.
    import inspect
    src = inspect.getsource(LTPPoller.poll)
    prefetch_calls = src.count("await self._prefetch_stale_histories(")
    assert prefetch_calls == 2, "expected exactly one prefetch call for 5-min and one for 15-min OHLC"
    first_prefetch_idx = src.index("await self._prefetch_stale_histories(")
    main_loop_idx = src.index("for symbol in self.symbols:")
    assert first_prefetch_idx < main_loop_idx, "prefetch must run before the main per-symbol loop"


# ── _fetch_kite_ohlc()/_fetch_kite_ohlc_15m() now log an empty (non-raising)
# Kite response instead of failing completely silently (2026-11-09, same
# live incident as above) ─────────────────────────────────────────────────

class _FakeEmptyKite:
    def historical_data(self, *a, **kw):
        return []  # Zerodha returned a well-formed but empty response, no exception


def test_fetch_kite_ohlc_logs_on_empty_response(caplog):
    poller = LTPPoller(redis_client=None, symbols=["SYM"], kite=_FakeEmptyKite(),
                        instrument_tokens={"SYM": 1})
    with caplog.at_level("WARNING"):
        result = poller._fetch_kite_ohlc("SYM")
    assert result is None
    assert any("returned empty for SYM" in r.message for r in caplog.records)


def test_fetch_kite_ohlc_15m_logs_on_empty_response(caplog):
    poller = LTPPoller(redis_client=None, symbols=["SYM"], kite=_FakeEmptyKite(),
                        instrument_tokens={"SYM": 1})
    with caplog.at_level("WARNING"):
        result = poller._fetch_kite_ohlc_15m("SYM")
    assert result is None
    assert any("returned empty for SYM" in r.message for r in caplog.records)
