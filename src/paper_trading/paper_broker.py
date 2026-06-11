import logging
import uuid
from typing import Dict, List, Any, Optional
from datetime import datetime
from src.brokers.base import AbstractBroker

logger = logging.getLogger(__name__)

class PaperBroker(AbstractBroker):
    """
    Virtual Broker for Paper Trading.
    Simulates order execution, maintains virtual balance and positions.
    """
    def __init__(self, initial_balance: float = 300000.0):
        self.balance = initial_balance
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}
        logger.info(f"Initialized PaperBroker with virtual balance: ₹{self.balance}")

    async def place_order(self, symbol: str, side: str, quantity: int, price: float) -> str:
        """
        Simulates instant execution of an order.
        In a more advanced setup, this would queue the order and wait for the market 
        tick to cross the limit price. For now, we assume instant fill at the requested price.
        """
        order_id = str(uuid.uuid4())
        timestamp = datetime.utcnow()
        
        cost = quantity * price
        
        # Basic validation
        if side == "BUY" and self.balance < cost:
            logger.warning(f"PaperBroker: Insufficient funds to BUY {quantity} {symbol} at {price}. Balance: {self.balance}, Required: {cost}")
            raise ValueError("Insufficient virtual funds.")

        order = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "status": "COMPLETED", # Instant fill
            "timestamp": timestamp
        }
        
        self._orders[order_id] = order
        self._update_position(symbol, side, quantity, price)
        
        # Deduct or add virtual balance
        if side == "BUY":
            self.balance -= cost
        else:
            self.balance += cost
            
        logger.info(f"PaperBroker: Executed {side} order for {quantity} {symbol} @ {price}. New Balance: ₹{self.balance:.2f}")
        return order_id

    def _update_position(self, symbol: str, side: str, quantity: int, price: float):
        """Internal method to update virtual positions upon order fill."""
        if symbol not in self._positions:
            self._positions[symbol] = {"symbol": symbol, "quantity": 0, "avg_price": 0.0}
            
        pos = self._positions[symbol]
        current_qty = pos["quantity"]
        
        if side == "BUY":
            # Weighted average price calculation
            total_cost = (current_qty * pos["avg_price"]) + (quantity * price)
            new_qty = current_qty + quantity
            pos["avg_price"] = total_cost / new_qty if new_qty > 0 else 0.0
            pos["quantity"] = new_qty
        else: # SELL
            new_qty = current_qty - quantity
            pos["quantity"] = new_qty
            if new_qty == 0:
                pos["avg_price"] = 0.0
                
        # If position drops to zero, we can remove it or keep it with 0 qty. Keeping it for history.

    async def cancel_order(self, order_id: str) -> bool:
        """Simulate cancellation. Since orders are instantly filled, this mostly fails."""
        order = self._orders.get(order_id)
        if not order:
            logger.warning(f"PaperBroker: Order {order_id} not found.")
            return False
            
        if order["status"] == "COMPLETED":
            logger.warning(f"PaperBroker: Cannot cancel order {order_id}, already COMPLETED.")
            return False
            
        order["status"] = "CANCELLED"
        logger.info(f"PaperBroker: Cancelled order {order_id}")
        return True

    async def modify_order(self, order_id: str, new_price: float, new_quantity: int) -> bool:
        """Simulate modification. Since orders are instantly filled, this mostly fails."""
        logger.warning(f"PaperBroker: Modification not supported for instantly filled orders.")
        return False

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Return all active virtual positions."""
        return [p for p in self._positions.values() if p["quantity"] != 0]

    async def get_orders(self) -> List[Dict[str, Any]]:
        """Return all virtual orders."""
        return list(self._orders.values())
