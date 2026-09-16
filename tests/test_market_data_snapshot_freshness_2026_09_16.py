"""
LTP-poll -> signal-cycle snapshot freshness gate (2026-09-16, external
review round 2, "LTP -> signal-cycle dependency ordering").

LTPPoller and LiveTradingEngine run on independent scheduler timers with no
hard dependency between them -- a fully-stalled poller could otherwise go
undetected at the CYCLE level until enough individual symbols aged past
_get_market_data()'s own per-symbol 90s check. market:universe_health now
carries poll_seq/poll_epoch, and the engine checks staleness before
evaluating any new entries -- deliberately a staleness check only, not a
strict "poll_seq must be newer than last processed" gate (see the method's
own docstring for why).
"""
import json
import time

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeRedis:
    def __init__(self, store=None, raise_exc=None):
        self.store = store or {}
        self._raise_exc = raise_exc

    async def get(self, key):
        if self._raise_exc:
            raise self._raise_exc
        return self.store.get(key)


class _FakeEngine:
    _market_data_snapshot_is_stale = LiveTradingEngine._market_data_snapshot_is_stale
    _MARKET_DATA_SNAPSHOT_MAX_AGE_SECONDS = LiveTradingEngine._MARKET_DATA_SNAPSHOT_MAX_AGE_SECONDS

    def __init__(self, redis):
        self._redis = redis


@pytest.mark.asyncio
async def test_fresh_snapshot_does_not_block_entries():
    redis = _FakeRedis({"market:universe_health": json.dumps({
        "poll_seq": 5, "poll_epoch": time.time(),
    })})
    engine = _FakeEngine(redis)

    assert await engine._market_data_snapshot_is_stale() is False


@pytest.mark.asyncio
async def test_stale_snapshot_blocks_entries():
    redis = _FakeRedis({"market:universe_health": json.dumps({
        "poll_seq": 5, "poll_epoch": time.time() - 200,  # well past 75s
    })})
    engine = _FakeEngine(redis)

    assert await engine._market_data_snapshot_is_stale() is True


@pytest.mark.asyncio
async def test_snapshot_right_at_the_threshold_boundary():
    redis = _FakeRedis({"market:universe_health": json.dumps({
        "poll_seq": 5, "poll_epoch": time.time() - 74,
    })})
    engine = _FakeEngine(redis)

    assert await engine._market_data_snapshot_is_stale() is False


@pytest.mark.asyncio
async def test_missing_snapshot_key_fails_closed():
    redis = _FakeRedis({})
    engine = _FakeEngine(redis)

    assert await engine._market_data_snapshot_is_stale() is True


@pytest.mark.asyncio
async def test_unreadable_snapshot_fails_closed():
    redis = _FakeRedis(raise_exc=ConnectionError("redis down"))
    engine = _FakeEngine(redis)

    assert await engine._market_data_snapshot_is_stale() is True


@pytest.mark.asyncio
async def test_malformed_json_fails_closed():
    redis = _FakeRedis({"market:universe_health": "{not valid json"})
    engine = _FakeEngine(redis)

    assert await engine._market_data_snapshot_is_stale() is True


@pytest.mark.asyncio
async def test_payload_without_poll_epoch_does_not_block():
    # Older/degenerate payload -- don't block on missing instrumentation itself.
    redis = _FakeRedis({"market:universe_health": json.dumps({"universe_size": 132})})
    engine = _FakeEngine(redis)

    assert await engine._market_data_snapshot_is_stale() is False


@pytest.mark.asyncio
async def test_no_redis_wired_fails_open():
    # Matches _get_active_symbols()'s existing "no redis -> fall back
    # gracefully" convention -- a fundamentally different situation from
    # "redis is up but this key is stale/missing", which fails closed.
    engine = _FakeEngine(None)

    assert await engine._market_data_snapshot_is_stale() is False
