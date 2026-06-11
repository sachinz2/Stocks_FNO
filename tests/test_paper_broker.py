import pytest
from src.paper_trading.paper_broker import PaperBroker

@pytest.fixture
def paper_broker():
    return PaperBroker(initial_balance=100000.0)

@pytest.mark.asyncio
async def test_paper_broker_place_buy_order(paper_broker):
    order_id = await paper_broker.place_order("RELIANCE", "BUY", 10, 2500.0)
    
    assert order_id is not None
    assert paper_broker.balance == 75000.0 # 100000 - (10 * 2500)
    
    orders = await paper_broker.get_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "COMPLETED"
    
    positions = await paper_broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "RELIANCE"
    assert positions[0]["quantity"] == 10
    assert positions[0]["avg_price"] == 2500.0

@pytest.mark.asyncio
async def test_paper_broker_insufficient_funds(paper_broker):
    with pytest.raises(ValueError, match="Insufficient virtual funds"):
        await paper_broker.place_order("MRF", "BUY", 1000, 50000.0)

@pytest.mark.asyncio
async def test_paper_broker_buy_and_sell(paper_broker):
    # Buy 10
    await paper_broker.place_order("TCS", "BUY", 10, 100.0)
    assert paper_broker.balance == 99000.0
    
    # Sell 5
    await paper_broker.place_order("TCS", "SELL", 5, 110.0)
    assert paper_broker.balance == 99550.0 # 99000 + (5 * 110)
    
    positions = await paper_broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["quantity"] == 5
    assert positions[0]["avg_price"] == 100.0 # avg price of remaining longs doesn't change on partial exit
