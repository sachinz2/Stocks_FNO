"""
get_option_quality_metrics() (2026-09-16, "Trade Quality Layer" v1 of 3,
external review round 2 Part 2, option-quality filter).

Distinct from get_option_quote() (tested in test_option_chain_reliable_price.py)
-- this always makes its own fresh kite.quote() call to read bid/ask depth,
open interest, and volume, none of which the Redis price caches persist.
Reuses the same _FakeKiteQuote double as that file.
"""
import pytest

from src.market_data.option_chain import get_option_quality_metrics


class _FakeKiteQuote:
    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.quote_calls = []

    def quote(self, instruments):
        self.quote_calls.append(list(instruments))
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _quote(bid=None, ask=None, oi=None, volume=None):
    return {
        "NFO:SBIN26SEP800CE": {
            "depth": {
                "buy":  [{"price": p, "quantity": 1, "orders": 1} for p in (bid or [])],
                "sell": [{"price": p, "quantity": 1, "orders": 1} for p in (ask or [])],
            },
            "oi": oi,
            "volume": volume,
        }
    }


@pytest.mark.asyncio
async def test_returns_none_when_kite_is_unavailable():
    assert await get_option_quality_metrics("SBIN26SEP800CE", None) is None


@pytest.mark.asyncio
async def test_returns_none_when_the_quote_call_raises():
    fake_kite = _FakeKiteQuote(raise_exc=ConnectionError("timeout"))
    assert await get_option_quality_metrics("SBIN26SEP800CE", fake_kite) is None


@pytest.mark.asyncio
async def test_computes_spread_pct_from_both_sides_of_the_book():
    fake_kite = _FakeKiteQuote(_quote(bid=[95.0], ask=[105.0], oi=12000, volume=5400))

    result = await get_option_quality_metrics("SBIN26SEP800CE", fake_kite)

    assert fake_kite.quote_calls == [["NFO:SBIN26SEP800CE"]]
    assert result["bid"] == 95.0
    assert result["ask"] == 105.0
    # spread = (105-95)/mid(100) * 100 = 10%
    assert result["spread_pct"] == pytest.approx(10.0)
    assert result["oi"] == 12000
    assert result["volume"] == 5400


@pytest.mark.asyncio
async def test_spread_pct_is_none_when_only_one_side_of_the_book_has_depth():
    fake_kite = _FakeKiteQuote(_quote(bid=[95.0], ask=[]))

    result = await get_option_quality_metrics("SBIN26SEP800CE", fake_kite)

    assert result["bid"] == 95.0
    assert result["ask"] is None
    assert result["spread_pct"] is None


@pytest.mark.asyncio
async def test_spread_pct_is_none_when_the_book_is_entirely_empty():
    fake_kite = _FakeKiteQuote(_quote(bid=[], ask=[]))

    result = await get_option_quality_metrics("SBIN26SEP800CE", fake_kite)

    assert result["bid"] is None
    assert result["ask"] is None
    assert result["spread_pct"] is None


@pytest.mark.asyncio
async def test_zero_price_depth_levels_are_ignored_same_as_get_option_quote():
    # Zerodha pads unused depth levels with price=0 -- must not be mistaken
    # for a real zero-price bid/ask (same convention as resolve_reliable_
    # option_price(), see test_option_chain_reliable_price.py).
    fake_kite = _FakeKiteQuote({
        "NFO:SBIN26SEP800CE": {
            "depth": {
                "buy": [{"price": 0, "quantity": 0, "orders": 0}] * 5,
                "sell": [{"price": 105.0, "quantity": 100, "orders": 1}] + [{"price": 0, "quantity": 0, "orders": 0}] * 4,
            },
            "oi": None, "volume": None,
        }
    })

    result = await get_option_quality_metrics("SBIN26SEP800CE", fake_kite)

    assert result["bid"] is None
    assert result["ask"] == 105.0
    assert result["spread_pct"] is None
