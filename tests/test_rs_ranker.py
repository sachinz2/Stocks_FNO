"""
RSRanker gating logic: fail-open when no relative-strength data exists yet,
and correct top-10 membership gating once data is available. This mirrors
the exact gate live_trading_engine.py applies to BUY (long-call) entries.
"""
import json
import pytest
from src.market_data.rs_ranker import RSRanker, REDIS_RS_RANKS_KEY


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _gate(signal_str, symbol, rs_ranks):
    """Mirrors the exact gating logic in live_trading_engine.py."""
    if signal_str == "BUY" and rs_ranks:
        top = {e["symbol"] for e in rs_ranks[:10]}
        return symbol in top
    return True  # not gated (SELL, or no data yet)


@pytest.mark.asyncio
async def test_get_ranks_fails_open_with_no_data():
    ranker = RSRanker(_FakeRedis())
    ranks = await ranker.get_ranks()
    assert ranks == []
    assert _gate("BUY", "ANYSTOCK", ranks) is True


@pytest.mark.asyncio
async def test_top_10_membership_gating():
    redis = _FakeRedis()
    ranker = RSRanker(redis)

    fake_ranks = [{"symbol": f"SYM{i}", "rs_score": 100 - i, "rank": i + 1} for i in range(15)]
    fake_ranks[11]["symbol"] = "ANYSTOCK"  # rank 12, outside top 10
    redis.store[REDIS_RS_RANKS_KEY] = json.dumps(fake_ranks)

    ranks = await ranker.get_ranks()
    assert len(ranks) == 15
    assert _gate("BUY", "ANYSTOCK", ranks) is False, "rank-12 symbol must be gated out of BUY"
    assert _gate("BUY", "SYM3", ranks) is True, "rank-4 (top 10) symbol must pass"
    assert _gate("SELL", "ANYSTOCK", ranks) is True, "SELL must never be gated by RS"
