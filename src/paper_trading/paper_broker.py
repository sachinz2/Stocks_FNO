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
    Deducts realistic Zerodha F&O fees on every order so paper P&L
    reflects actual take-home rather than gross premium.
    """

    def __init__(self, initial_balance: float = 300000.0):
        self.balance = initial_balance
        self.total_fees_paid = 0.0
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}
        logger.info(f"Initialized PaperBroker with virtual balance: ₹{self.balance}")

    @staticmethod
    def _transaction_fees(side: str, quantity: int, price: float) -> float:
        """
        Estimate Zerodha F&O fees per order (options).
        Brokerage dominates at ₹20 flat — the rest are small for low-premium trades.

        Zerodha F&O fee structure:
          Brokerage        : ₹20 flat per order (or 0.03% turnover, whichever lower)
          STT              : 0.1% of premium turnover on SELL side only
          Exchange charges : 0.053% of premium turnover (NSE)
          GST              : 18% on (brokerage + exchange charges)
          SEBI charges     : ₹10 per crore of turnover
          Stamp duty       : 0.003% of turnover on BUY side only
        """
        turnover = quantity * price
        brokerage = min(20.0, turnover * 0.0003)
        stt = turnover * 0.001 if side == "SELL" else 0.0
        exchange = turnover * 0.00053
        gst = (brokerage + exchange) * 0.18
        sebi = turnover * 0.000001  # ₹10 per crore = ₹10 / 1,00,00,000
        stamp = turnover * 0.00003 if side == "BUY" else 0.0
        return round(brokerage + stt + exchange + gst + sebi + stamp, 2)

    async def place_order(self, symbol: str, side: str, quantity: int, price: float) -> str:
        """Simulates instant execution at the requested price, deducting realistic fees."""
        order_id = str(uuid.uuid4())
        timestamp = datetime.utcnow()
        cost = quantity * price
        fees = self._transaction_fees(side, quantity, price)

        if side == "BUY" and self.balance < cost + fees:
            logger.warning(
                f"PaperBroker: Insufficient funds to BUY {quantity} {symbol} @ {price}. "
                f"Balance: ₹{self.balance:.2f}, Required: ₹{cost + fees:.2f} (incl. fees ₹{fees:.2f})"
            )
            raise ValueError("Insufficient virtual funds.")

        order = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fees": fees,
            "status": "COMPLETED",
            "timestamp": timestamp,
        }
        self._orders[order_id] = order
        self._update_position(symbol, side, quantity, price)

        if side == "BUY":
            self.balance -= cost + fees
        else:
            self.balance += cost - fees

        self.total_fees_paid += fees
        logger.info(
            f"PaperBroker: {side} {quantity} {symbol} @ ₹{price:.2f} | "
            f"Fees: ₹{fees:.2f} | Balance: ₹{self.balance:.2f}"
        )
        return order_id

    def _update_position(self, symbol: str, side: str, quantity: int, price: float):
        """
        Update virtual positions on order fill.

        avg_price tracks the entry price for BOTH long (positive qty) and
        short (negative qty) positions so the dashboard can compute PnL correctly:
          Long  PnL = (market_price - avg_price) × qty
          Short PnL = (avg_price - market_price) × abs(qty)
                    = (market_price - avg_price) × qty   (qty is negative)
        """
        if symbol not in self._positions:
            self._positions[symbol] = {"symbol": symbol, "quantity": 0, "avg_price": 0.0}

        pos = self._positions[symbol]
        current_qty = pos["quantity"]
        current_avg = pos["avg_price"]

        if side == "BUY":
            new_qty = current_qty + quantity
            if current_qty >= 0:
                # Adding to or opening a long position — weighted average
                total_cost = current_qty * current_avg + quantity * price
                pos["avg_price"] = total_cost / new_qty if new_qty > 0 else 0.0
            elif new_qty >= 0:
                # Covering a short position entirely (possibly flipping to long)
                pos["avg_price"] = price if new_qty > 0 else 0.0
            # else: partially covering short — keep short avg_price as-is
            pos["quantity"] = new_qty

        else:  # SELL
            new_qty = current_qty - quantity
            if current_qty <= 0:
                # Adding to or opening a short position — weighted average of sell prices
                total_credit = abs(current_qty) * current_avg + quantity * price
                pos["avg_price"] = total_credit / abs(new_qty) if new_qty != 0 else 0.0
            elif new_qty < 0:
                # Flipping from long to short — record the short entry price
                pos["avg_price"] = price
            elif new_qty == 0:
                pos["avg_price"] = 0.0
            # else: partially reducing a long — keep long avg_price as-is
            pos["quantity"] = new_qty

    async def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if not order:
            return False
        if order["status"] == "COMPLETED":
            logger.warning(f"PaperBroker: Cannot cancel {order_id}, already COMPLETED.")
            return False
        order["status"] = "CANCELLED"
        return True

    async def modify_order(self, order_id: str, new_price: float, new_quantity: int) -> bool:
        logger.warning("PaperBroker: Modification not supported for instantly filled orders.")
        return False

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Return all active virtual positions (non-zero quantity)."""
        return [p for p in self._positions.values() if p["quantity"] != 0]

    async def get_orders(self) -> List[Dict[str, Any]]:
        return list(self._orders.values())
