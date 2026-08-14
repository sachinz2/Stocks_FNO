"""
fetch_and_cache_real_contracts() (2026-08-13) -- caches the real per-symbol
option chain (expiry -> strike -> {CE/PE: real tradingsymbol}) from
kite.instruments("NFO") into Redis, feeding get_real_contract()'s validation/
correction of our own computed strikes and hand-formatted symbol strings.
"""
import json
from datetime import date

from scripts.zerodha_auto_auth import fetch_and_cache_real_contracts
from src.core.constants import REDIS_CONTRACT_PREFIX


class _FakeSyncRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


def _mock_instrument(name, expiry, strike, itype, tradingsymbol):
    return {
        "name": name, "expiry": expiry, "strike": strike,
        "instrument_type": itype, "tradingsymbol": tradingsymbol,
    }


def _bajfinance_instruments():
    e1, e2, e3, e4 = date(2026, 8, 25), date(2026, 9, 29), date(2026, 10, 27), date(2026, 11, 24)
    rows = []
    for expiry, suffix in [(e1, "AUG"), (e2, "SEP"), (e3, "OCT"), (e4, "NOV")]:
        for strike in (1160, 1170):
            rows.append(_mock_instrument(
                "BAJFINANCE", expiry, strike, "CE", f"BAJFINANCE26{suffix}{strike}CE",
            ))
            rows.append(_mock_instrument(
                "BAJFINANCE", expiry, strike, "PE", f"BAJFINANCE26{suffix}{strike}PE",
            ))
    return rows


def test_caches_only_the_three_nearest_expiries(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    count = fetch_and_cache_real_contracts(_bajfinance_instruments())

    assert count == 1
    raw = fake_redis.store[f"{REDIS_CONTRACT_PREFIX}BAJFINANCE"]
    data = json.loads(raw)
    # 4 expiries fed in (Aug/Sep/Oct/Nov) -- only the nearest 3 kept.
    assert len(data) == 3
    assert "2026-08-25" in data
    assert "2026-09-29" in data
    assert "2026-10-27" in data
    assert "2026-11-24" not in data, "must not cache further-out expiries than needed"


def test_real_tradingsymbol_stored_correctly_per_strike_and_type(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    fetch_and_cache_real_contracts(_bajfinance_instruments())

    data = json.loads(fake_redis.store[f"{REDIS_CONTRACT_PREFIX}BAJFINANCE"])
    assert data["2026-09-29"]["1160"]["CE"] == "BAJFINANCE26SEP1160CE"
    assert data["2026-09-29"]["1160"]["PE"] == "BAJFINANCE26SEP1160PE"
    assert data["2026-09-29"]["1170"]["CE"] == "BAJFINANCE26SEP1170CE"


def test_non_fno_symbols_are_not_cached(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    instruments = _bajfinance_instruments() + [
        _mock_instrument("SOME_RANDOM_STOCK_NOT_IN_FNO", date(2026, 9, 29), 100, "CE", "JUNK"),
    ]
    fetch_and_cache_real_contracts(instruments)

    assert f"{REDIS_CONTRACT_PREFIX}SOME_RANDOM_STOCK_NOT_IN_FNO" not in fake_redis.store


def test_futures_and_non_option_rows_are_ignored(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    instruments = _bajfinance_instruments() + [
        {"name": "BAJFINANCE", "expiry": date(2026, 9, 29), "strike": 0,
         "instrument_type": "FUT", "tradingsymbol": "BAJFINANCE26SEPFUT"},
    ]
    fetch_and_cache_real_contracts(instruments)

    data = json.loads(fake_redis.store[f"{REDIS_CONTRACT_PREFIX}BAJFINANCE"])
    for expiry_strikes in data.values():
        for strikes in expiry_strikes.values():
            assert set(strikes.keys()) <= {"CE", "PE"}


def test_fractional_strike_key_formatted_correctly(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    instruments = [
        _mock_instrument("BAJFINANCE", date(2026, 9, 29), 167.5, "CE", "BAJFINANCE26SEP167.5CE"),
    ]
    fetch_and_cache_real_contracts(instruments)

    data = json.loads(fake_redis.store[f"{REDIS_CONTRACT_PREFIX}BAJFINANCE"])
    assert data["2026-09-29"]["167.5"]["CE"] == "BAJFINANCE26SEP167.5CE"


# ── instrument_caches_healthy() / refresh_instrument_caches() (2026-08-14) ──
#
# Found in a follow-up review: run_daily_auth() stores the access token
# BEFORE attempting the separate lot-size/real-contract cache refresh, so a
# successful login followed by a failed instrument fetch (network blip,
# Zerodha rate-limit, timeout on the ~5-10MB instrument dump) used to be
# logged as "non-fatal, using hardcoded/computed fallback" -- a fallback
# that no longer exists now that the live trading engine fails closed on a
# cache miss. This is the self-heal counterpart: api/main.py's kite
# self-heal calls instrument_caches_healthy() every 3 minutes and actively
# retries refresh_instrument_caches() if the caches are empty despite a
# valid token, the same active-retry pattern already used for a missing
# auth token itself.

from scripts.zerodha_auto_auth import instrument_caches_healthy, refresh_instrument_caches
from src.core.constants import REDIS_LOT_SIZE_PREFIX


def test_instrument_caches_healthy_true_when_canonical_symbol_cached(monkeypatch):
    fake_redis = _FakeSyncRedis()
    fake_redis.store[f"{REDIS_LOT_SIZE_PREFIX}RELIANCE"] = "500"
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    assert instrument_caches_healthy() is True


def test_instrument_caches_healthy_false_when_empty(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)

    assert instrument_caches_healthy() is False


def test_instrument_caches_healthy_false_on_redis_error(monkeypatch):
    class _BrokenRedis:
        def get(self, key):
            raise ConnectionError("redis down")
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: _BrokenRedis())

    assert instrument_caches_healthy() is False


def test_refresh_instrument_caches_populates_both_caches(monkeypatch):
    fake_redis = _FakeSyncRedis()
    monkeypatch.setattr("scripts.zerodha_auto_auth.get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        "scripts.zerodha_auto_auth.fetch_nfo_instruments",
        lambda token: _bajfinance_instruments(),
    )

    lot_count, contract_count = refresh_instrument_caches("fake-token")

    # _bajfinance_instruments()'s mock rows have no lot_size field (0,
    # filtered out by fetch_and_cache_lot_sizes) -- this test is about
    # refresh_instrument_caches() correctly calling and combining both
    # underlying functions' real return values, not about lot-size data.
    assert lot_count == 0
    assert contract_count == 1
    assert f"{REDIS_LOT_SIZE_PREFIX}BAJFINANCE" not in fake_redis.store
    assert f"{REDIS_CONTRACT_PREFIX}BAJFINANCE" in fake_redis.store


def test_run_daily_auth_survives_instrument_refresh_failure(monkeypatch):
    # The login itself must still succeed and return a token even if the
    # instrument-cache refresh fails -- that's exactly the scenario
    # instrument_caches_healthy()'s self-heal exists to detect afterward.
    from scripts import zerodha_auto_auth as mod

    monkeypatch.setattr(mod, "zerodha_auto_login", lambda: "fake-access-token")
    monkeypatch.setattr(mod, "store_token", lambda token: None)

    def _boom(token):
        raise RuntimeError("instrument dump timed out")
    monkeypatch.setattr(mod, "refresh_instrument_caches", _boom)

    result = mod.run_daily_auth()
    assert result == "fake-access-token"


def test_kite_self_heal_wired_to_instrument_cache_retry():
    # main.py's lifespan is too heavy (real DB/broker/redis wiring) to
    # invoke directly in a unit test -- static-source regression guard,
    # matching this project's convention for other lifespan-embedded jobs
    # (see test_zerodha_sync.py's daily-sync live-mode-gate test).
    import inspect
    from src.api import main as main_module
    src = inspect.getsource(main_module)

    assert "instrument_caches_healthy" in src
    assert "refresh_instrument_caches" in src
    idx = src.index("Instrument-cache self-heal")
    block = src[idx:idx + 2000]
    assert "if not instrument_caches_healthy():" in block
    assert "is_auth_retry_window(now)" in block
    assert "should_retry_auth(" in block
    assert "app.state.last_instrument_cache_retry_attempt" in block
