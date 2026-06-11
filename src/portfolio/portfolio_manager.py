import logging
from typing import Dict, Any, List
from datetime import datetime

from src.brokers.base import AbstractBroker
from src.database.repositories.base import BaseRepository
from src.database.models.position import Position
from src.database.models.stock import Stock

logger = logging.getLogger(__name__)

class PortfolioManager:
    """
    Manages current portfolio state, tracks exposure, PnL, and interacts with positions.
    """
    def __init__(
        self, 
        broker: AbstractBroker, 
        position_repo: BaseRepository[Position],
        stock_repo: BaseRepository[Stock]
    ):
        self.broker = broker
        self.position_repo = position_repo
        self.stock_repo = stock_repo

    async def sync_positions(self):
        """
        Fetches true position state from the broker and reconciles it with the database.
        """
        logger.info("Synchronizing positions from broker...")
        try:
            broker_positions = await self.broker.get_positions()
            db_positions = await self.position_repo.get_all()
            
            db_pos_map = {p.symbol: p for p in db_positions}

            for bp in broker_positions:
                symbol = bp.get("tradingsymbol", bp.get("symbol"))
                qty = int(bp.get("quantity", 0))
                avg_price = float(bp.get("average_price", bp.get("avg_price", 0.0)))
                realized_pnl = float(bp.get("realized_pnl", bp.get("pnl", 0.0)))
                
                if symbol in db_pos_map:
                    # Update existing
                    await self.position_repo.update(db_pos_map[symbol], {
                        "quantity": qty,
                        "avg_price": avg_price,
                        "realized_pnl": realized_pnl,
                        "updated_at": datetime.utcnow()
                    })
                    del db_pos_map[symbol]
                else:
                    # Create new position if qty != 0
                    if qty != 0:
                        await self.position_repo.create({
                            "symbol": symbol,
                            "quantity": qty,
                            "avg_price": avg_price,
                            "market_price": avg_price, # placeholder until live tick
                            "unrealized_pnl": 0.0,
                            "realized_pnl": realized_pnl,
                            "updated_at": datetime.utcnow()
                        })

            # Any remaining db_positions are closed/missing in broker, zero them out
            for remaining_pos in db_pos_map.values():
                if remaining_pos.quantity != 0:
                    await self.position_repo.update(remaining_pos, {
                        "quantity": 0,
                        "updated_at": datetime.utcnow()
                    })
                    
        except Exception as e:
            logger.error(f"Failed to sync portfolio positions: {e}")

    async def calculate_pnl(self, current_market_prices: Dict[str, float]) -> Dict[str, float]:
        """
        Calculates unrealized PnL based on a dictionary of live market prices.
        Updates the database and returns aggregate PnL.
        """
        positions = await self.position_repo.get_all()
        total_unrealized = 0.0
        total_realized = 0.0

        for pos in positions:
            total_realized += float(pos.realized_pnl or 0)
            
            if pos.quantity == 0:
                continue

            current_price = current_market_prices.get(pos.symbol)
            if not current_price:
                continue

            avg_price = float(pos.avg_price)
            qty = pos.quantity
            
            # Simple un-realized calc (qty > 0 means LONG, qty < 0 means SHORT)
            unrealized = (current_price - avg_price) * qty
            total_unrealized += unrealized

            # Persist the update
            await self.position_repo.update(pos, {
                "market_price": current_price,
                "unrealized_pnl": unrealized,
                "updated_at": datetime.utcnow()
            })

        return {
            "unrealized_pnl": total_unrealized,
            "realized_pnl": total_realized,
            "total_pnl": total_unrealized + total_realized
        }

    async def get_exposure(self) -> float:
        """Returns total gross exposure (capital at risk) across all open positions."""
        positions = await self.position_repo.get_all()
        exposure = sum(abs(pos.quantity * float(pos.market_price or pos.avg_price)) for pos in positions)
        return exposure

    async def get_sector_exposure(self) -> Dict[str, float]:
        """
        Returns capital exposure grouped by sector to manage correlation risk.
        """
        positions = await self.position_repo.filter() # get all
        stocks = await self.stock_repo.get_all()
        sector_map = {s.symbol: s.sector for s in stocks}

        exposure_by_sector = {}
        for pos in positions:
            if pos.quantity == 0:
                continue
                
            sector = sector_map.get(pos.symbol, "UNKNOWN")
            value = abs(pos.quantity * float(pos.market_price or pos.avg_price))
            exposure_by_sector[sector] = exposure_by_sector.get(sector, 0.0) + value

        return exposure_by_sector