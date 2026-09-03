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
import json
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


# ── set_symbols() (2026-08-20, code-review fix) ──────────────────────────────
#
# This class's own docstring used to say the F&O underlying-stock set is a
# "fixed list, set at startup" -- true until the weekly active-universe
# recompute job needed to update it without a restart, so a newly-promoted
# symbol still gets real-time WebSocket-fallback tick coverage.

def test_set_symbols_replaces_the_tracked_underlyings():
    poller = ZerodhaLTPPoller(kite=None, redis_client=None, symbols=["OLD1", "OLD2"])
    assert poller._instruments == ["NSE:OLD1", "NSE:OLD2"]

    poller.set_symbols(["NEW1", "NEW2", "NEW3"])

    assert poller._instruments == ["NSE:NEW1", "NSE:NEW2", "NSE:NEW3"]
    assert poller._symbol_map == {"NSE:NEW1": "NEW1", "NSE:NEW2": "NEW2", "NSE:NEW3": "NEW3"}
    assert "NSE:OLD1" not in poller._symbol_map


def test_set_symbols_does_not_touch_registered_option_contracts():
    poller = ZerodhaLTPPoller(kite=None, redis_client=None, symbols=["OLD1"])
    poller.register_option_contracts(["OLD126SEP100CE"])

    poller.set_symbols(["NEW1"])

    assert "NFO:OLD126SEP100CE" in poller._option_instruments


# ── Underlying-stock refresh: skip the write when WebSocket is fresh ───────
# Live incident, 2026-09-02: momentum_v1 produced zero real trading signals
# for 8 days despite a hard, sustained market move -- traced to RVOL being
# near-zero at the exact moment of every breakout confirmation attempt,
# across dozens of stocks and days. Root cause: this poller's docstring
# claims it "updates only the close field... leaving indicators intact",
# but its full read-modify-write of the same Redis key ZerodhaTicker
# (WebSocket) writes to on every tick could still silently revert
# cur_bar_volume/_last_cum_volume to a stale snapshot -- confirmed live via
# a corrupted tick showing cur_bar_volume near the day's ENTIRE cumulative
# volume, ~40x every other bar that day. Fixed by skipping this poller's
# write for any symbol WebSocket has ticked for recently (see
# _WS_FRESH_WINDOW), removing the race for the ~99% of the session
# WebSocket is healthy while preserving genuine-outage fallback behavior.

class _FakeKiteUnderlying:
    def __init__(self, ltp_response):
        self.ltp_response = ltp_response

    def ltp(self, instruments):
        return self.ltp_response

    def quote(self, instruments):
        return {}


@pytest.mark.asyncio
async def test_skips_write_when_websocket_ticked_recently(monkeypatch):
    now = datetime(2026, 9, 2, 12, 0, 0)
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    monkeypatch.setattr("src.core.utils.now_ist", lambda: now)

    fake_redis = _FakeRedis()
    ws_stamp = (now - timedelta(seconds=3)).isoformat()  # well within the fresh window
    fake_redis.store["tick:RELIANCE"] = json.dumps({
        "symbol": "RELIANCE", "close": 2490.0, "ltp_source": "zerodha_realtime",
        "_ws_last_tick_at": ws_stamp, "cur_bar_volume": 12345,
    })

    fake_kite = _FakeKiteUnderlying({"NSE:RELIANCE": {"last_price": 2500.0}})
    poller = ZerodhaLTPPoller(fake_kite, fake_redis, symbols=["RELIANCE"])

    updated = await poller.refresh_ltp()

    assert updated == 0
    stored = json.loads(fake_redis.store["tick:RELIANCE"])
    assert stored["close"] == 2490.0, "must not overwrite -- WebSocket owns this symbol right now"
    assert stored["cur_bar_volume"] == 12345, "must not touch fields it never intended to change"


@pytest.mark.asyncio
async def test_still_writes_close_when_websocket_gone_stale(monkeypatch):
    now = datetime(2026, 9, 2, 12, 0, 0)
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    monkeypatch.setattr("src.core.utils.now_ist", lambda: now)

    fake_redis = _FakeRedis()
    ws_stamp = (now - timedelta(seconds=60)).isoformat()  # well beyond the fresh window
    fake_redis.store["tick:RELIANCE"] = json.dumps({
        "symbol": "RELIANCE", "close": 2490.0, "ltp_source": "zerodha_realtime",
        "_ws_last_tick_at": ws_stamp,
    })

    fake_kite = _FakeKiteUnderlying({"NSE:RELIANCE": {"last_price": 2500.0}})
    poller = ZerodhaLTPPoller(fake_kite, fake_redis, symbols=["RELIANCE"])

    updated = await poller.refresh_ltp()

    assert updated == 1, "genuine WebSocket outage -- this poller must still act as fallback"
    stored = json.loads(fake_redis.store["tick:RELIANCE"])
    assert stored["close"] == 2500.0
    assert stored["ltp_source"] == "zerodha_rest"


@pytest.mark.asyncio
async def test_still_writes_close_when_no_websocket_stamp_present(monkeypatch):
    """Guard against over-fixing -- ticks written before this fix (or by a
    process that never had a WebSocket connection) have no _ws_last_tick_at
    at all and must not be silently frozen forever."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    monkeypatch.setattr("src.core.utils.now_ist", lambda: now)

    fake_redis = _FakeRedis()
    fake_redis.store["tick:RELIANCE"] = json.dumps({
        "symbol": "RELIANCE", "close": 2490.0, "ltp_source": "zerodha_rest",
    })

    fake_kite = _FakeKiteUnderlying({"NSE:RELIANCE": {"last_price": 2500.0}})
    poller = ZerodhaLTPPoller(fake_kite, fake_redis, symbols=["RELIANCE"])

    updated = await poller.refresh_ltp()

    assert updated == 1
    stored = json.loads(fake_redis.store["tick:RELIANCE"])
    assert stored["close"] == 2500.0


@pytest.mark.asyncio
async def test_malformed_ws_stamp_does_not_block_the_write(monkeypatch):
    now = datetime(2026, 9, 2, 12, 0, 0)
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    monkeypatch.setattr("src.core.utils.now_ist", lambda: now)

    fake_redis = _FakeRedis()
    fake_redis.store["tick:RELIANCE"] = json.dumps({
        "symbol": "RELIANCE", "close": 2490.0, "_ws_last_tick_at": "not-a-timestamp",
    })
    fake_kite = _FakeKiteUnderlying({"NSE:RELIANCE": {"last_price": 2500.0}})
    poller = ZerodhaLTPPoller(fake_kite, fake_redis, symbols=["RELIANCE"])

    updated = await poller.refresh_ltp()

    assert updated == 1
    stored = json.loads(fake_redis.store["tick:RELIANCE"])
    assert stored["close"] == 2500.0
