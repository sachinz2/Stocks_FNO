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
        position_repo: BaseRepository,
        stock_repo: BaseRepository,
    ):
        self.broker = broker
        self.position_repo = position_repo
        self.stock_repo = stock_repo

    async def calculate_pnl(
        self, current_market_prices: Dict[str, float]
    ) -> Dict[str, float]:
        """
        Calculates unrealized PnL based on a dictionary of live market prices.
        Returns aggregate PnL summary.
        """
        positions = await self.position_repo.get_all()
        total_unrealized = 0.0
        total_realized = 0.0

        for pos in positions:
            if pos.deleted_at is not None:
                continue
            total_realized += float(pos.realized_pnl or 0)

            if pos.quantity == 0:
                continue

            current_price = current_market_prices.get(pos.symbol)
            if not current_price:
                continue

            avg_price = float(pos.avg_price)
            qty = pos.quantity
            unrealized = (current_price - avg_price) * qty
            total_unrealized += unrealized

            await self.position_repo.update(pos, {
                "market_price": current_price,
                "unrealized_pnl": unrealized,
                "updated_at": datetime.utcnow(),
            })

        return {
            "unrealized_pnl": total_unrealized,
            "realized_pnl": total_realized,
            "total_pnl": total_unrealized + total_realized,
        }

    async def get_exposure(self) -> float:
        positions = await self.position_repo.get_all()
        return sum(
            abs(pos.quantity * float(pos.market_price or pos.avg_price))
            for pos in positions
            if pos.deleted_at is None
        )

    async def get_sector_exposure(self) -> Dict[str, float]:
        positions = await self.position_repo.get_all()
        stocks = await self.stock_repo.get_all()
        sector_map = {s.symbol: s.sector for s in stocks}

        exposure_by_sector: Dict[str, float] = {}
        for pos in positions:
            if pos.quantity == 0 or pos.deleted_at is not None:
                continue
            sector = sector_map.get(pos.symbol, "UNKNOWN")
            value = abs(pos.quantity * float(pos.market_price or pos.avg_price))
            exposure_by_sector[sector] = exposure_by_sector.get(sector, 0.0) + value

        return exposure_by_sector
