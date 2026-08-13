"""
resolve_reliable_option_price() (2026-08-13).

Zerodha's last_price is the LAST TRADED price -- for an option contract
nobody's traded in a while, that can be badly stale while still being
served as if current. Confirmed live: TITAN26SEP4650PE's last_price sat
frozen at Rs82 (last actually traded 2026-07-29, two weeks earlier) with
zero bids and zero volume that day, while the real, current, tradeable
price -- visible only in the order book -- was far lower. This feeds both
the dashboard's displayed PnL and credit_spread_v1/iron_condor_v1's own
profit-target/stop-loss exit checks, so a stale price isn't cosmetic -- it
can make a strategy's exit logic blind to a position's real value.
"""
from datetime import datetime, timedelta
import pytest

from src.market_data.option_chain import resolve_reliable_option_price


def _quote(last_price, last_trade_time, buy=None, sell=None):
    return {
        "last_price": last_price,
        "last_trade_time": last_trade_time,
        "depth": {
            "buy":  [{"price": p, "quantity": 1, "orders": 1} for p in (buy or [])],
            "sell": [{"price": p, "quantity": 1, "orders": 1} for p in (sell or [])],
        },
    }


@pytest.fixture
def frozen_today(monkeypatch):
    """Freeze now_ist() to a fixed point so 'traded today' is deterministic."""
    fixed_now = datetime(2026, 8, 13, 12, 0, 0)
    monkeypatch.setattr("src.core.utils.now_ist", lambda: fixed_now)
    return fixed_now


def test_trusts_last_price_when_traded_today(frozen_today):
    q = _quote(last_price=45.0, last_trade_time=frozen_today, sell=[34.80])
    assert resolve_reliable_option_price(q) == 45.0


def test_reproduces_the_titan_scenario_uses_ask_not_stale_last_price(frozen_today):
    # Exact shape of the real TITAN26SEP4650PE quote: last traded 2 weeks
    # ago at Rs82, zero bids today, real asks starting at Rs34.80.
    stale_time = frozen_today - timedelta(days=15)
    q = _quote(
        last_price=82.0, last_trade_time=stale_time,
        buy=[], sell=[34.80, 37.15, 37.35],
    )
    price = resolve_reliable_option_price(q)
    assert price == 34.80, "must use the real, current ask -- not the frozen last_price"


def test_uses_bid_ask_midpoint_when_both_sides_present_but_stale(frozen_today):
    stale_time = frozen_today - timedelta(days=3)
    q = _quote(last_price=50.0, last_trade_time=stale_time, buy=[28.0], sell=[32.0])
    assert resolve_reliable_option_price(q) == 30.0


def test_uses_bid_alone_when_only_bid_present_and_stale(frozen_today):
    stale_time = frozen_today - timedelta(days=3)
    q = _quote(last_price=50.0, last_trade_time=stale_time, buy=[28.0], sell=[])
    assert resolve_reliable_option_price(q) == 28.0


def test_returns_none_when_stale_and_no_depth_at_all(frozen_today):
    stale_time = frozen_today - timedelta(days=15)
    q = _quote(last_price=82.0, last_trade_time=stale_time, buy=[], sell=[])
    assert resolve_reliable_option_price(q) is None


def test_missing_last_trade_time_falls_through_to_depth(frozen_today):
    q = _quote(last_price=82.0, last_trade_time=None, buy=[], sell=[40.0])
    assert resolve_reliable_option_price(q) == 40.0


def test_zero_price_levels_in_depth_are_ignored(frozen_today):
    # Zerodha pads unused depth levels with price=0 -- must not be mistaken
    # for a real zero-price bid/ask (confirmed shape in the real API response).
    stale_time = frozen_today - timedelta(days=3)
    q = {
        "last_price": 82.0,
        "last_trade_time": stale_time,
        "depth": {
            "buy": [{"price": 0, "quantity": 0, "orders": 0}] * 5,
            "sell": [{"price": 34.80, "quantity": 1750, "orders": 2}] + [{"price": 0, "quantity": 0, "orders": 0}] * 4,
        },
    }
    assert resolve_reliable_option_price(q) == 34.80
