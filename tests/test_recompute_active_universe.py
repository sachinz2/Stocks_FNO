"""
scripts/zerodha_auto_auth.py::recompute_active_universe() (2026-08-20).

The weekly job behind the self-correcting F&O active-universe list -- see
src/market_data/fno_universe.py's module docstring and
docs/LIVE_TRADING_CHECKLIST.md for why this exists (a one-time liquidity
snapshot would otherwise go stale the same way FNO_STRIKE_INTERVALS did).
"""
import json

import pytest

from scripts.zerodha_auto_auth import recompute_active_universe
from src.core.constants import REDIS_ACTIVE_FNO_SYMBOLS


class _FakeSyncRedis:
    def __init__(self, store=None):
        self.store = store or {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


class _FakeKiteConnect:
    """Stands in for kiteconnect.KiteConnect -- constructed fresh inside
    recompute_active_universe() via `from kiteconnect import KiteConnect`."""

    def __init__(self, api_key=None):
        pass

    def set_access_token(self, token):
        pass

    def instruments(self, exchange):
        if exchange == "NFO":
            return [
                {"name": "LIQUID", "instrument_type": "CE", "tradingsymbol": "LIQUID26SEP100CE"},
                {"name": "THIN", "instrument_type": "CE", "tradingsymbol": "THIN26SEP100CE"},
                {"name": "NIFTY", "instrument_type": "CE", "tradingsymbol": "NIFTY26SEP25000CE"},
            ]
        if exchange == "NSE":
            return [
                {"tradingsymbol": "LIQUID", "instrument_token": 1},
                {"tradingsymbol": "THIN", "instrument_token": 2},
            ]
        return []

    def historical_data(self, token, from_date, to_date, interval, continuous, oi):
        if token == 1:  # LIQUID
            return [{"close": 1000.0, "volume": 10_000_000}]  # Rs 1000 Cr/day -- well above the floor
        if token == 2:  # THIN
            return [{"close": 10.0, "volume": 1000}]  # Rs 10,000/day -- nowhere near the floor
        return []


@pytest.fixture(autouse=True)
def _patch_kiteconnect(monkeypatch):
    monkeypatch.setattr("kiteconnect.KiteConnect", _FakeKiteConnect)


def test_recompute_returns_only_qualifying_symbols(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    result = recompute_active_universe("fake-token")

    assert result["active"] == ["LIQUID"]
    assert "THIN" not in result["active"]
    assert "NIFTY" not in result["active"], "index options must never appear as a stock symbol"


def test_index_options_never_enter_the_candidate_universe(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    result = recompute_active_universe("fake-token")

    assert "NIFTY" not in result["tokens"]


def test_recompute_computes_added_and_removed_relative_to_current_redis_value(monkeypatch):
    fake_redis = _FakeSyncRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["THIN", "STALE_DELISTED"])})
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    result = recompute_active_universe("fake-token")

    assert result["added"] == ["LIQUID"]
    assert result["removed"] == ["STALE_DELISTED", "THIN"]


def test_recompute_writes_the_new_active_list_to_redis(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    recompute_active_universe("fake-token")

    stored = json.loads(fake_redis.store[REDIS_ACTIVE_FNO_SYMBOLS])
    assert stored == ["LIQUID"]


def test_recompute_returns_full_universe_tokens_not_just_the_active_subset(monkeypatch):
    # Callers (the weekly job in main.py) push these into LTPPoller/RSRanker
    # immediately -- must cover THIN too (even though it's not "active"),
    # since a symbol just above the floor next week still needs a token
    # ready without waiting for a kite.instruments("NSE") round-trip.
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    result = recompute_active_universe("fake-token")

    assert set(result["tokens"].keys()) == {"LIQUID", "THIN"}


# ── Data-coverage guard (2026-08-20, code-review fix) ────────────────────────
#
# compute_liquidity_turnover() catches each symbol's fetch failure
# individually and continues rather than aborting -- a systemic partial
# failure (Zerodha rate-limit, timeout) must not get published as if it were
# a genuine "the market got less liquid" result.

class _FakeKiteConnectManySymbolsPartialFailure:
    """10 real F&O stocks, but historical_data() only succeeds for 2 of
    them -- simulates a rate-limit/timeout hitting partway through."""

    def __init__(self, api_key=None):
        pass

    def set_access_token(self, token):
        pass

    def instruments(self, exchange):
        if exchange == "NFO":
            return [
                {"name": f"SYM{i}", "instrument_type": "CE", "tradingsymbol": f"SYM{i}26SEP100CE"}
                for i in range(10)
            ]
        if exchange == "NSE":
            return [{"tradingsymbol": f"SYM{i}", "instrument_token": i} for i in range(10)]
        return []

    def historical_data(self, token, from_date, to_date, interval, continuous, oi):
        if token < 2:  # only SYM0, SYM1 succeed -- 2/10 = 20% coverage, below the 70% floor
            return [{"close": 1000.0, "volume": 10_000_000}]
        raise RuntimeError("Zerodha rate-limited")


def test_partial_coverage_failure_does_not_overwrite_the_active_list(monkeypatch):
    fake_redis = _FakeSyncRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["PREVIOUSLY_ACTIVE_1", "PREVIOUSLY_ACTIVE_2"])})
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)
    monkeypatch.setattr("kiteconnect.KiteConnect", _FakeKiteConnectManySymbolsPartialFailure)

    result = recompute_active_universe("fake-token")

    assert result["skipped"] is True
    assert result["active"] == ["PREVIOUSLY_ACTIVE_1", "PREVIOUSLY_ACTIVE_2"]
    assert result["added"] == []
    assert result["removed"] == []
    # The Redis key itself must be untouched -- still whatever was live before.
    assert json.loads(fake_redis.store[REDIS_ACTIVE_FNO_SYMBOLS]) == ["PREVIOUSLY_ACTIVE_1", "PREVIOUSLY_ACTIVE_2"]


def test_full_coverage_result_is_not_marked_skipped(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)
    # Default autouse fixture's _FakeKiteConnect: both LIQUID and THIN succeed -- 2/2 = 100% coverage.

    result = recompute_active_universe("fake-token")

    assert result["skipped"] is False


def test_weekly_job_wired_into_the_scheduler():
    # main.py's lifespan is too heavy (real DB/broker/redis wiring) to
    # invoke directly in a unit test -- static-source regression guard,
    # matching this project's convention (see
    # test_zerodha_auto_auth_contracts.py's self-heal wiring test).
    import inspect
    from src.api import main as main_module
    src = inspect.getsource(main_module)

    assert "recompute_active_universe" in src
    idx = src.index("_weekly_universe_refresh")
    block = src[idx:idx + 4000]
    assert 'CronTrigger(day_of_week="sun"' in block
    assert "ltp_poller.set_kite(" in block
    assert "rs_ranker.set_kite(" in block
    assert "engine._notify(" in block
    # Fixed 2026-08-20 (code review): the weekly job used to leave
    # ZerodhaTicker/ZerodhaLTPPoller frozen at their startup-time symbol
    # list, and used to publish a partial-coverage recompute unconditionally.
    assert 'result.get("skipped")' in block
    assert "zt._instrument_tokens" in block
    assert "zlp.set_symbols(" in block


def test_provision_kite_resolves_tokens_for_the_full_universe_not_just_static_list():
    # Fixed 2026-08-20: used to filter kite.instruments("NSE") by membership
    # in the static FNO_SYMBOLS list -- a restart between weekly recompute
    # runs would then lose the token for anything the weekly job had
    # dynamically added beyond that static list.
    import inspect
    from src.api import main as main_module
    src = inspect.getsource(main_module)

    idx = src.index("async def _provision_kite")
    block = src[idx:idx + 2000]
    assert "extract_stock_underlyings" in block
    assert 'kite.instruments, "NFO"' in block
    assert "set(FNO_SYMBOLS)" not in block, "still filtering tokens to the static list instead of the full universe"
