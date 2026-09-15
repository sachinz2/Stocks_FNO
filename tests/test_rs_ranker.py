"""
RSRanker gating logic: fail-closed when no relative-strength data exists
yet, and correct top-10/bottom-10 membership gating once data is
available. Mirrors the exact gate live_trading_engine.py applies to BUY
(long-call, top-10 strongest-vs-NIFTY) and SELL (long-put, bottom-10
weakest-vs-NIFTY, added 2026-09-15 "symmetric RS ranking") entries.
"""
import json
import pytest
from src.market_data.rs_ranker import RSRanker, REDIS_RS_RANKS_KEY, REDIS_RS_BOTTOM10_KEY


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _gate(signal_str, symbol, rs_ranks):
    """Mirrors the exact gating logic in live_trading_engine.py."""
    if not rs_ranks:
        return False  # fail closed -- no data yet
    if signal_str == "BUY":
        top = {e["symbol"] for e in rs_ranks[:10]}
        return symbol in top
    bottom = {e["symbol"] for e in rs_ranks[-10:]}
    return symbol in bottom


@pytest.mark.asyncio
async def test_get_ranks_fails_open_with_no_data():
    # get_ranks() itself returns [] (a plain cache-miss read) -- the
    # fail-CLOSED decision is the caller's (engine/_gate here), not
    # get_ranks()'s own job.
    ranker = RSRanker(_FakeRedis())
    ranks = await ranker.get_ranks()
    assert ranks == []
    assert _gate("BUY", "ANYSTOCK", ranks) is False
    assert _gate("SELL", "ANYSTOCK", ranks) is False


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


@pytest.mark.asyncio
async def test_bottom_10_membership_gating_for_sell():
    # Fixed 2026-09-15 (external review, "symmetric RS ranking"): SELL used
    # to be entirely ungated -- now checked against the weakest-vs-NIFTY
    # bottom-10, the mirror of BUY's top-10 strongest check.
    redis = _FakeRedis()
    ranker = RSRanker(redis)

    fake_ranks = [{"symbol": f"SYM{i}", "rs_score": 100 - i, "rank": i + 1} for i in range(15)]
    # SYM14 (last, rank 15) is the weakest -- must be in the bottom-10.
    # SYM3 (rank 4) is a strong performer -- must NOT be in the bottom-10.
    redis.store[REDIS_RS_RANKS_KEY] = json.dumps(fake_ranks)

    ranks = await ranker.get_ranks()
    assert _gate("SELL", "SYM14", ranks) is True, "weakest-vs-NIFTY symbol must pass the SELL gate"
    assert _gate("SELL", "SYM3", ranks) is False, "a strong performer must be gated out of SELL"


@pytest.mark.asyncio
async def test_get_bottom_n_returns_weakest_first():
    redis = _FakeRedis()
    ranker = RSRanker(redis)
    redis.store[REDIS_RS_BOTTOM10_KEY] = json.dumps(["WEAKEST", "SECOND_WEAKEST", "THIRD_WEAKEST"])

    result = await ranker.get_bottom_n(2)

    assert result == ["WEAKEST", "SECOND_WEAKEST"]


@pytest.mark.asyncio
async def test_get_bottom_n_fails_closed_with_no_data():
    # Unlike get_top_n (which falls back to self.symbols[:n]), get_bottom_n
    # has no such fallback -- an empty cache must read as "can't confirm
    # weakness," not "everything is eligible."
    ranker = RSRanker(_FakeRedis())
    result = await ranker.get_bottom_n(10)
    assert result == []


@pytest.mark.asyncio
async def test_rank_publishes_both_top10_and_bottom10():
    from datetime import datetime

    class _FakeHistoryRedis(_FakeRedis):
        pass

    ranker = RSRanker(_FakeHistoryRedis(), symbols=["A", "B", "C"])

    def _fake_compute_rs(sym):
        return {"A": 90.0, "B": 50.0, "C": 10.0}[sym]

    import pandas as pd
    ranker._compute_rs = _fake_compute_rs
    ranker._nifty = pd.DataFrame({"close": [100.0]})  # non-empty, bypasses the "NIFTY history unavailable" early return
    ranker._last_fetch = datetime.now()  # skip the blocking _load_all_history call

    scores = await ranker.rank()

    assert [s["symbol"] for s in scores] == ["A", "B", "C"]  # highest RS first
    top10 = json.loads(ranker._redis.store["nfo:rs_top10"])
    bottom10 = json.loads(ranker._redis.store[REDIS_RS_BOTTOM10_KEY])
    assert top10[0] == "A"       # strongest first
    assert bottom10[0] == "C"    # weakest first
