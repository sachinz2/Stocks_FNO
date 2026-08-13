"""
ZerodhaLTPPoller's active-option-contract refresh (2026-08-13 fix).

Was kite.ltp() (last-traded-price only) -- switched to kite.quote() (which
also returns last_trade_time + order book depth) so a stale, no-longer-
trading contract's frozen last_price doesn't get written into the same
Redis cache (optltp:{contract}) that both the dashboard's displayed PnL and
credit_spread_v1/iron_condor_v1's own exit-rule checks read. See
resolve_reliable_option_price() in option_chain.py for the actual
staleness-detection logic -- these tests confirm the poller is wired to it
correctly (kite.quote() called, not kite.ltp(), and the resolved price
lands in Redis with the right key/TTL).
"""
from datetime import datetime, timedelta
import pytest

from src.market_data.zerodha_ltp_poller import ZerodhaLTPPoller


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.sets = []  # (key, value, ex) call log

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.sets.append((key, value, ex))


class _FakeKite:
    def __init__(self, quote_response):
        self.quote_response = quote_response
        self.quote_calls = []
        self.ltp_calls = []

    def quote(self, instruments):
        self.quote_calls.append(list(instruments))
        return self.quote_response

    def ltp(self, instruments):
        self.ltp_calls.append(list(instruments))
        # Underlying-stock polling still uses ltp() -- return empty so that
        # part of refresh_ltp() is a no-op in these option-focused tests.
        return {}


@pytest.mark.asyncio
async def test_active_option_contracts_use_quote_not_ltp(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    monkeypatch.setattr(
        "src.core.utils.now_ist", lambda: datetime(2026, 8, 13, 12, 0, 0),
    )

    fake_kite = _FakeKite({
        "NFO:TITAN26SEP4650PE": {
            "last_price": 82.0,
            "last_trade_time": datetime(2026, 7, 29, 12, 51, 10),  # stale
            "depth": {"buy": [], "sell": [{"price": 34.80, "quantity": 1750, "orders": 2}]},
        },
    })
    fake_redis = _FakeRedis()

    poller = ZerodhaLTPPoller(fake_kite, fake_redis, symbols=[])
    poller.register_option_contracts(["TITAN26SEP4650PE"])

    await poller.refresh_ltp()

    assert fake_kite.quote_calls == [["NFO:TITAN26SEP4650PE"]], (
        "active option contracts must be refreshed via kite.quote(), not kite.ltp()"
    )
    assert fake_redis.store["optltp:TITAN26SEP4650PE"] == "34.8", (
        "must store the resolved (order-book-derived) price, not the stale last_price"
    )
    # 15-second TTL preserved from the original implementation.
    key, value, ex = fake_redis.sets[-1]
    assert ex == 15


@pytest.mark.asyncio
async def test_active_option_contracts_use_last_price_when_traded_today(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    now = datetime(2026, 8, 13, 12, 0, 0)
    monkeypatch.setattr("src.core.utils.now_ist", lambda: now)

    fake_kite = _FakeKite({
        "NFO:BAJFINANCE26SEP1160CE": {
            "last_price": 12.75,
            "last_trade_time": now,  # traded moments ago
            "depth": {"buy": [{"price": 12.5, "quantity": 1, "orders": 1}],
                      "sell": [{"price": 13.0, "quantity": 1, "orders": 1}]},
        },
    })
    fake_redis = _FakeRedis()

    poller = ZerodhaLTPPoller(fake_kite, fake_redis, symbols=[])
    poller.register_option_contracts(["BAJFINANCE26SEP1160CE"])

    await poller.refresh_ltp()

    assert fake_redis.store["optltp:BAJFINANCE26SEP1160CE"] == "12.75"


@pytest.mark.asyncio
async def test_no_reliable_price_skips_the_redis_write(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    monkeypatch.setattr(
        "src.core.utils.now_ist", lambda: datetime(2026, 8, 13, 12, 0, 0),
    )

    fake_kite = _FakeKite({
        "NFO:DEAD26SEP100CE": {
            "last_price": 50.0,
            "last_trade_time": datetime(2026, 7, 1, 9, 0, 0),  # long stale
            "depth": {"buy": [], "sell": []},  # no order book at all
        },
    })
    fake_redis = _FakeRedis()

    poller = ZerodhaLTPPoller(fake_kite, fake_redis, symbols=[])
    poller.register_option_contracts(["DEAD26SEP100CE"])

    await poller.refresh_ltp()

    assert "optltp:DEAD26SEP100CE" not in fake_redis.store, (
        "must not write an unreliable price -- caller's ATR/Black-Scholes "
        "fallback should take over instead of a stale/fabricated number"
    )
