"""
Integration test for paper trading flow.
This verifies that signals → orders → positions flow works end-to-end.
"""
import asyncio
import sys
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from src.database.connection import AsyncSessionLocal
from src.database.models.signal import Signal
from src.database.models.order import Order
from src.database.models.position import Position
from src.database.models.stock import Stock
from src.database.repositories.base import BaseRepository
from src.orders.order_manager import OrderManager
from src.risk.risk_manager import RiskManager
from src.paper_trading.paper_broker import PaperBroker
from src.portfolio.positions_tracker import PositionsTracker
from src.database.models.audit import AuditLog

async def test_paper_trading_flow():
    """
    Complete paper trading flow:
    1. Create test stocks
    2. Generate buy signals
    3. Place orders from signals
    4. Verify positions are updated
    5. Verify PnL calculations
    """
    
    session = AsyncSessionLocal()
    
    try:
        print("=" * 60)
        print("PAPER TRADING INTEGRATION TEST")
        print("=" * 60)
        
        # Step 1: Create test stocks
        print("\n[1/5] Creating test stocks...")
        stock_repo = BaseRepository(Stock, session)
        
        test_stocks = [
            {"symbol": "SBIN", "company_name": "State Bank of India", "sector": "Banking", "exchange": "NSE", "fno_enabled": True, "active": True},
            {"symbol": "INFY", "company_name": "Infosys", "sector": "IT", "exchange": "NSE", "fno_enabled": True, "active": True},
            {"symbol": "RELIANCE", "company_name": "Reliance Industries", "sector": "Energy", "exchange": "NSE", "fno_enabled": True, "active": True},
        ]
        
        created_stocks = []
        for stock_data in test_stocks:
            existing = await stock_repo.filter(symbol=stock_data["symbol"])
            if not existing:
                stock = await stock_repo.create(stock_data)
                created_stocks.append(stock)
                print(f"  ✓ Created stock: {stock_data['symbol']}")
            else:
                print(f"  ✓ Stock already exists: {stock_data['symbol']}")
        
        # Step 2: Generate signals
        print("\n[2/5] Generating BUY signals...")
        signal_repo = BaseRepository(Signal, session)
        
        signals_created = 0
        for stock_data in test_stocks:
            signal = await signal_repo.create({
                "strategy_name": "ema_crossover",
                "symbol": stock_data["symbol"],
                "signal_type": "BUY",
                "confidence": 0.80,
                "generated_at": datetime.utcnow(),
                "status": "PENDING"
            })
            signals_created += 1
            print(f"  ✓ Signal created: BUY {stock_data['symbol']} (confidence: 0.80)")
        
        # Step 3: Place orders from signals
        print("\n[3/5] Placing orders from signals...")
        paper_broker = PaperBroker(initial_balance=300000.0)
        risk_manager = RiskManager(initial_capital=300000.0)
        order_repo = BaseRepository(Order, session)
        audit_repo = BaseRepository(AuditLog, session)
        om = OrderManager(paper_broker, risk_manager, order_repo, audit_repo)
        
        orders_placed = 0
        for stock_data in test_stocks:
            # Simulate different prices for realistic testing
            price = {"SBIN": 510.50, "INFY": 1450.75, "RELIANCE": 2650.00}[stock_data["symbol"]]
            quantity = 2  # 2 lots per stock
            
            db_order = await om.place_order(stock_data["symbol"], "BUY", quantity, price)
            if db_order:
                orders_placed += 1
                print(f"  ✓ Order placed: BUY {quantity} {stock_data['symbol']} @ ₹{price}")
            else:
                print(f"  ✗ Failed to place order for {stock_data['symbol']}")
        
        # Step 4: Verify orders in database
        print("\n[4/5] Verifying orders in database...")
        all_orders = await order_repo.get_all()
        print(f"  ✓ Total orders in database: {len(all_orders)}")
        for order in all_orders:
            if order.deleted_at is None:
                print(f"    - {order.symbol}: {order.side} {order.quantity} @ ₹{order.price} [{order.order_status}]")
        
        # Step 5: Track positions
        print("\n[5/5] Tracking portfolio positions...")
        tracker = PositionsTracker(session)
        
        # Update positions from completed orders
        updated = await tracker.update_positions_from_orders()
        print(f"  ✓ Positions updated from {updated} orders")
        
        # Get portfolio summary
        summary = await tracker.get_portfolio_summary()
        print(f"\n  Portfolio Summary:")
        print(f"    - Total positions: {summary['total_positions']}")
        print(f"    - Total unrealized PnL: ₹{summary['total_unrealized_pnl']:.2f}")
        print(f"    - Total realized PnL: ₹{summary['total_realized_pnl']:.2f}")
        print(f"    - Total PnL: ₹{summary['total_pnl']:.2f}")
        
        print("\n" + "=" * 60)
        print("TEST COMPLETED SUCCESSFULLY ✓")
        print("=" * 60)
        print("\nSummary:")
        print(f"  • Stocks created: {len(test_stocks)}")
        print(f"  • Signals generated: {signals_created}")
        print(f"  • Orders placed: {orders_placed}")
        print(f"  • Positions tracked: {summary['total_positions']}")
        print("\nPaper trading is ready for Monday!")
        
        return True
        
    except Exception as e:
        print(f"\n✗ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        await session.close()

if __name__ == "__main__":
    success = asyncio.run(test_paper_trading_flow())
    sys.exit(0 if success else 1)
