import pytest
from src.paper_trading.paper_broker import PaperBroker


@pytest.fixture
def paper_broker(monkeypatch):
    # Fixed 2026-08-07: these tests predated PaperBroker's realistic fee +
    # bid-ask-slippage + rejection-probability simulation (added to make
    # paper P&L reflect real execution costs, not frictionless fills -- see
    # the entry-pricing fix from 2026-08-06) and asserted exact balances
    # that assumed zero fees/slippage. Pin random.random() above every
    # rejection-probability tier (max 5%) so fills are deterministic and
    # never flake on the random rejection draw; slippage itself is already
    # deterministic at these price levels (>Rs5, extra_slip_frac=0 --
    # see PaperBroker._fill_simulation), so only rejection needed pinning.
    monkeypatch.setattr("random.random", lambda: 0.9)
    return PaperBroker(initial_balance=100000.0)


@pytest.mark.asyncio
async def test_paper_broker_place_buy_order(paper_broker):
    order_id = await paper_broker.place_order("RELIANCE", "BUY", 10, 2500.0)

    assert order_id is not None
    # Real fill (BUY -> ask = price + 3% half-spread @ >Rs5 tier):
    #   fill = 2500 + 2500*0.03 = 2575.00; cost = 25750.00
    #   fees = brokerage(7.725, capped 20) + exchange(13.6475) + gst(3.8471)
    #        + sebi(0.02575) + stamp(0.7725) = 26.02
    #   balance = 100000 - 25750.00 - 26.02 = 74223.98
    assert paper_broker.balance == pytest.approx(74223.98, abs=0.01)

    orders = await paper_broker.get_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "COMPLETED"
    assert orders[0]["fill_price"] == pytest.approx(2575.00, abs=0.01)

    positions = await paper_broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "RELIANCE"
    assert positions[0]["quantity"] == 10
    assert positions[0]["avg_price"] == pytest.approx(2575.00, abs=0.01)


@pytest.mark.asyncio
async def test_paper_broker_insufficient_funds(paper_broker):
    with pytest.raises(ValueError, match="Insufficient virtual funds"):
        await paper_broker.place_order("MRF", "BUY", 1000, 50000.0)


@pytest.mark.asyncio
async def test_paper_broker_buy_and_sell(paper_broker):
    # Buy 10 @ 100 -> fill 103.00 (100 + 100*0.03), fees 1.04
    await paper_broker.place_order("TCS", "BUY", 10, 100.0)
    assert paper_broker.balance == pytest.approx(98968.96, abs=0.01)

    # Sell 5 @ 110 -> fill 106.70 (110 - 110*0.03), fees 1.06
    await paper_broker.place_order("TCS", "SELL", 5, 110.0)
    assert paper_broker.balance == pytest.approx(99501.40, abs=0.01)

    positions = await paper_broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["quantity"] == 5
    # avg price of remaining longs doesn't change on partial exit
    assert positions[0]["avg_price"] == pytest.approx(103.00, abs=0.01)


# ── SELL-to-open (naked short) -- credit_spread_v1/iron_condor_v1's short
# legs, and credit_spread's own leg entries generally, are SELL orders on a
# symbol with NO existing position, not a close/reduce of a prior BUY. This
# is a genuinely different code path (opens a negative-quantity position,
# blocks approx. margin) from every case test_paper_broker_buy_and_sell
# above covers, and had zero test coverage until now (2026-08-13).

@pytest.mark.asyncio
async def test_paper_broker_sell_to_open_naked_short(paper_broker):
    # Sell 10 @ 100 (fresh symbol, no prior position) -> fill 97.00
    # (100 - 100*0.03), fees 1.92, margin blocked 10*97.00*10.0=9700.00.
    order_id = await paper_broker.place_order("NIFTY24000CE", "SELL", 10, 100.0)

    assert order_id is not None
    assert paper_broker.balance == pytest.approx(100968.08, abs=0.01)

    positions = await paper_broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "NIFTY24000CE"
    assert positions[0]["quantity"] == -10, "a SELL with no prior position must open a SHORT, not go negative-of-nothing"
    assert positions[0]["avg_price"] == pytest.approx(97.00, abs=0.01)

    assert paper_broker.margin_blocked["NIFTY24000CE"] == pytest.approx(9700.00, abs=0.01)


@pytest.mark.asyncio
async def test_paper_broker_sell_to_open_blocked_when_margin_insufficient(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.9)
    broker = PaperBroker(initial_balance=5000.0)  # 9700 required, only 5000 available

    with pytest.raises(ValueError, match="Insufficient virtual margin"):
        await broker.place_order("NIFTY24000CE", "SELL", 10, 100.0)

    # A rejected SELL must not leave a phantom short or blocked margin behind.
    positions = await broker.get_positions()
    assert positions == []
    assert broker.margin_blocked == {}


@pytest.mark.asyncio
async def test_paper_broker_buy_to_cover_short_releases_margin(paper_broker):
    await paper_broker.place_order("NIFTY24000CE", "SELL", 10, 100.0)
    assert paper_broker.margin_blocked["NIFTY24000CE"] == pytest.approx(9700.00, abs=0.01)

    # Buy back all 10 to fully cover -> fill 99.91 (97.00 + 97.00*0.03).
    await paper_broker.place_order("NIFTY24000CE", "BUY", 10, 97.00)

    positions = await paper_broker.get_positions()
    assert positions == [], "get_positions() filters out zero-quantity rows -- fully covered means gone"
    assert "NIFTY24000CE" not in paper_broker.margin_blocked, \
        "margin must be released once the short is fully covered, not left blocked"
