import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from src.brokers.base import AbstractBroker
from src.risk.risk_manager import RiskManager
from src.database.repositories.base import BaseRepository
from src.database.models.order import Order
from src.database.models.audit import AuditLog

logger = logging.getLogger(__name__)

class OrderManager:
    """
    Institutional-grade Order Management System (OMS).
    Handles order lifecycle, state transitions, risk validation, and audit logging.
    """
    def __init__(
        self, 
        broker: AbstractBroker, 
        risk_manager: RiskManager, 
        order_repo: BaseRepository[Order],
        audit_repo: BaseRepository[AuditLog]
    ):
        self.broker = broker
        self.risk_manager = risk_manager
        self.order_repo = order_repo
        self.audit_repo = audit_repo

    async def _audit(self, action: str, payload: Dict[str, Any]):
        try:
            await self.audit_repo.create({
                "service_name": "OrderManager",
                "action": action,
                "payload": payload,
                "timestamp": datetime.utcnow()
            })
        except Exception as e:
            logger.warning(f"Audit log skipped ({action}): {e}")

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        is_spread_leg: bool = False,
    ) -> Optional[Order]:
        """
        Main entry point for placing orders.
        Validates risk, saves initial state, routes to broker, and updates state.

        is_spread_leg=True skips the open-position count check in RiskManager.
        Pass True for legs 2-4 of credit spreads and iron condors.
        """
        # 1. Create PENDING order in DB
        db_order = await self.order_repo.create({
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "order_status": "PENDING",
            "created_at": datetime.utcnow()
        })
        await self._audit("ORDER_RECEIVED", {"order_id": db_order.id, "symbol": symbol, "side": side})

        # 2. Validate Risk
        if not self.risk_manager.validate_trade(symbol, side, quantity, price, is_spread_leg=is_spread_leg):
            await self.order_repo.update(db_order, {"order_status": "REJECTED_BY_RISK"})
            await self._audit("ORDER_REJECTED_RISK", {"order_id": db_order.id})
            return db_order

        # 3. Route to Broker
        try:
            broker_order_id = await self.broker.place_order(symbol, side, quantity, price)
            await self.order_repo.update(db_order, {
                "broker_order_id": broker_order_id,
                "order_status": "OPEN"
            })
            await self._audit("ORDER_ROUTED", {"order_id": db_order.id, "broker_order_id": broker_order_id})
            return db_order
        except Exception as e:
            logger.error(f"Broker order placement failed: {e}")
            await self.order_repo.update(db_order, {"order_status": "FAILED"})
            await self._audit("ORDER_FAILED", {"order_id": db_order.id, "error": str(e)})
            return db_order

    async def cancel_order(self, internal_order_id: int) -> bool:
        db_order = await self.order_repo.get_by_id(internal_order_id)
        if not db_order or not db_order.broker_order_id:
            logger.warning(f"Order {internal_order_id} not found or has no broker ID.")
            return False

        if db_order.order_status in ["COMPLETED", "CANCELLED", "REJECTED", "FAILED"]:
            logger.warning(f"Cannot cancel order in state {db_order.order_status}")
            return False

        success = await self.broker.cancel_order(db_order.broker_order_id)
        if success:
            await self.order_repo.update(db_order, {"order_status": "CANCELLED"})
            await self._audit("ORDER_CANCELLED", {"order_id": db_order.id})
            
        return success

    async def sync_orders(self):
        """
        Synchronizes open orders from the DB with the live broker status.
        Crucial for detecting partial fills and completions.
        """
        open_db_orders = await self.order_repo.filter(order_status="OPEN")
        if not open_db_orders:
            return

        try:
            broker_orders = await self.broker.get_orders()
            # Map broker orders by broker_order_id for quick lookup
            broker_order_map = {str(o.get("order_id", "")): o for o in broker_orders}

            for db_order in open_db_orders:
                if not db_order.broker_order_id:
                    continue

                b_order = broker_order_map.get(str(db_order.broker_order_id))
                if b_order:
                    # Map broker specific status to internal status
                    new_status = self._map_broker_status(b_order.get("status", "OPEN"))
                    if new_status != db_order.order_status:
                        await self.order_repo.update(db_order, {"order_status": new_status})
                        await self._audit("ORDER_STATUS_SYNC", {"order_id": db_order.id, "new_status": new_status})
        except Exception as e:
            logger.error(f"Failed to sync orders: {e}")

    def _map_broker_status(self, broker_status: str) -> str:
        """Normalizes external broker statuses to our internal ENUM/strings."""
        status_upper = broker_status.upper()
        if status_upper in ["COMPLETE", "COMPLETED", "FILLED"]:
            return "COMPLETED"
        if status_upper in ["CANCELLED", "CANCELED"]:
            return "CANCELLED"
        if status_upper in ["REJECTED"]:
            return "REJECTED"
        return "OPEN"