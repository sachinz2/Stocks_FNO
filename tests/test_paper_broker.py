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
