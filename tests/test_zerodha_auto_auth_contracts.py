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
