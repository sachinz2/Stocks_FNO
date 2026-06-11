import logging
from typing import List, Dict, Any
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from src.database.models.position import Position
from src.database.models.order import Order
from src.database.models.trade import Trade
from src.database.repositories.base import BaseRepository

logger = logging.getLogger(__name__)

class PositionsTracker:
    """
    Tracks open positions, calculates PnL, and monitors position health.
    """
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self.position_repo = BaseRepository(Position, session)
        self.order_repo = BaseRepository(Order, session)
        self.trade_repo = BaseRepository(Trade, session)
    
    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions"""
        positions = await self.position_repo.get_all()
        return [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_price": float(p.avg_price) if p.avg_price else 0,
                "market_price": float(p.market_price) if p.market_price else 0,
                "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else 0,
                "realized_pnl": float(p.realized_pnl) if p.realized_pnl else 0
            }
            for p in positions if p.deleted_at is None and p.quantity != 0
        ]
    
    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Get portfolio summary with total PnL"""
        positions = await self.get_open_positions()
        
        total_unrealized = sum(p["unrealized_pnl"] for p in positions)
        total_realized = sum(p["realized_pnl"] for p in positions)
        total_pnl = total_unrealized + total_realized
        
        return {
            "total_positions": len(positions),
            "total_unrealized_pnl": total_unrealized,
            "total_realized_pnl": total_realized,
            "total_pnl": total_pnl,
            "positions": positions
        }
    
    async def update_position_pnl(
        self, 
        symbol: str, 
        market_price: float
    ) -> bool:
        """Update unrealized PnL for a position based on current market price"""
        try:
            positions = await self.position_repo.filter(symbol=symbol)
            
            if not positions:
                logger.warning(f"No position found for {symbol}")
                return False
            
            position = positions[0]
            
            if position.deleted_at or position.quantity == 0:
                return False
            
            avg_price = float(position.avg_price) if position.avg_price else 0
            quantity = position.quantity
            
            # Calculate unrealized PnL
            unrealized_pnl = (market_price - avg_price) * quantity
            
            # Update position
            await self.position_repo.update(
                position,
                {
                    "market_price": market_price,
                    "unrealized_pnl": unrealized_pnl
                }
            )
            
            logger.info(f"Updated position {symbol}: price={market_price}, qty={quantity}, unrealized_pnl={unrealized_pnl}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating position PnL for {symbol}: {e}")
            return False
    
    async def close_position(self, symbol: str, exit_price: float) -> bool:
        """Close a position and record realized PnL"""
        try:
            positions = await self.position_repo.filter(symbol=symbol)
            
            if not positions:
                logger.warning(f"No position found for {symbol}")
                return False
            
            position = positions[0]
            
            if position.deleted_at or position.quantity == 0:
                return False
            
            avg_price = float(position.avg_price) if position.avg_price else 0
            quantity = position.quantity
            
            # Calculate realized PnL
            realized_pnl = (exit_price - avg_price) * quantity
            
            # Create a trade record for this exit
            trade_data = {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": avg_price,
                "exit_price": exit_price,
                "pnl": realized_pnl,
                "entry_time": position.created_at,
                "exit_time": datetime.utcnow(),
                "strategy_name": "manual_close"
            }
            
            await self.trade_repo.create(trade_data)
            
            # Close the position
            await self.position_repo.update(
                position,
                {
                    "quantity": 0,
                    "market_price": exit_price,
                    "realized_pnl": realized_pnl,
                    "unrealized_pnl": 0
                }
            )
            
            logger.info(f"Closed position {symbol}: realized_pnl={realized_pnl}")
            return True
            
        except Exception as e:
            logger.error(f"Error closing position {symbol}: {e}")
            return False
    
    async def update_positions_from_orders(self) -> int:
        """Process completed orders and update positions accordingly"""
        updated_count = 0
        
        try:
            # Get all completed orders
            orders = await self.order_repo.filter(order_status="COMPLETED")
            
            for order in orders:
                if order.deleted_at:
                    continue
                
                symbol = order.symbol
                side = order.side
                quantity = order.quantity
                price = float(order.price) if order.price else 0
                
                # Get or create position
                positions = await self.position_repo.filter(symbol=symbol)
                
                if positions and positions[0].deleted_at is None:
                    position = positions[0]
                else:
                    # Create new position
                    position = await self.position_repo.create({
                        "symbol": symbol,
                        "quantity": 0,
                        "avg_price": 0,
                        "market_price": 0,
                        "unrealized_pnl": 0,
                        "realized_pnl": 0
                    })
                
                # Update position based on order
                current_qty = position.quantity
                current_avg_price = float(position.avg_price) if position.avg_price else 0
                
                if side == "BUY":
                    # Update weighted average price
                    total_cost = (current_qty * current_avg_price) + (quantity * price)
                    new_qty = current_qty + quantity
                    new_avg_price = total_cost / new_qty if new_qty > 0 else 0
                    
                    await self.position_repo.update(
                        position,
                        {
                            "quantity": new_qty,
                            "avg_price": new_avg_price
                        }
                    )
                else:  # SELL
                    new_qty = current_qty - quantity
                    
                    # Calculate realized PnL for this sell
                    realized_pnl = (price - current_avg_price) * quantity
                    current_realized = float(position.realized_pnl) if position.realized_pnl else 0
                    
                    await self.position_repo.update(
                        position,
                        {
                            "quantity": new_qty,
                            "realized_pnl": current_realized + realized_pnl,
                            "avg_price": current_avg_price if new_qty > 0 else 0
                        }
                    )
                
                updated_count += 1
                logger.info(f"Updated position for {symbol} from order {order.id}")
            
            return updated_count
            
        except Exception as e:
            logger.error(f"Error updating positions from orders: {e}")
            return 0
