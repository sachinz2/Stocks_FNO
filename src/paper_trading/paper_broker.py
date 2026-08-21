import logging
import random
import uuid
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from src.brokers.base import AbstractBroker

logger = logging.getLogger(__name__)

# Writing (SELL-to-open/increase) an option requires SPAN + exposure margin in
# real trading, not just the premium received — but that margin is set by the
# exchange based on the underlying's volatility/strike distance, and PaperBroker
# only ever sees the option's own symbol/premium here, not the underlying price
# or strike (that lives one layer up, in the strategy/engine code). Modeling
# real SPAN margin isn't feasible at this layer, so this is a deliberately
# simple, conservative proxy: block SHORT_MARGIN_MULTIPLE_OF_PREMIUM times the
# premium per short lot as a stand-in "used margin" figure. It's meant as a
# guardrail against unbounded short-selling in paper mode (previously there was
# no capital check on SELL at all — see risk_manager.py's own comment "For SELL
# legs the margin is taken by the broker, not our capital tracking", which
# assumed this check existed and it didn't). In LIVE mode this is moot — the
# real Zerodha/exchange margin engine handles it.
SHORT_MARGIN_MULTIPLE_OF_PREMIUM = 10.0


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
        self.margin_blocked: Dict[str, float] = {}  # symbol -> approx margin held for its short qty
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

    @staticmethod
    def _fill_simulation(price: float) -> Tuple[float, float]:
        """
        Return (rejection_probability, extra_slippage_fraction) based on option price.

        Rejection simulates exchange/broker failures (margin, connectivity, no-quote).
        Extra slippage is added on top of the bid-ask half-spread for illiquid options
        where the fill price can drift significantly from the last-traded price.

        Price tiers:
          ≤ ₹0.30  (deep OTM hedge legs): 5% rejection, up to 30% extra slippage
          ≤ ₹1.00  (cheap near-expiry / far-OTM): 2% rejection, up to 15% extra slippage
          ≤ ₹5.00  (typical short legs): 1% rejection, up to 5% extra slippage
          > ₹5.00  (liquid / near-ATM): 0.5% rejection, no extra slippage
        """
        if price <= 0.30:
            return 0.05, 0.30
        if price <= 1.00:
            return 0.02, 0.15
        if price <= 5.00:
            return 0.01, 0.05
        return 0.005, 0.0

    @staticmethod
    def _bid_ask_half_spread(price: float) -> float:
        """
        Estimate half the bid-ask spread for an NSE F&O option.
        Options are quoted at the mid; we assume fills happen at:
          BUY  → ask = mid + half_spread  (we pay more)
          SELL → bid = mid - half_spread  (we receive less)

        Thresholds are calibrated to typical NSE F&O microstructure:
          ≤ ₹0.30  → illiquid deep-OTM hedge legs; spread can be ≥ 40% of mid
          ≤ ₹0.75  → cheap near-expiry / far-OTM legs; 20% half-spread
          ≤ ₹2.00  → standard hedge legs; 10% half-spread
          ≤ ₹5.00  → short legs close to ATM; 6% half-spread
          > ₹5.00  → liquid ITM / high-premium legs; 3% half-spread
        """
        if price <= 0.30:
            return price * 0.40
        if price <= 0.75:
            return price * 0.20
        if price <= 2.00:
            return price * 0.10
        if price <= 5.00:
            return price * 0.06
        return price * 0.03

    async def place_order(
        self, symbol: str, side: str, quantity: int, price: float,
        is_exit_order: bool = False, strategy_name: Optional[str] = None,
        product_override: Optional[str] = None, client_order_id: Optional[str] = None,
    ) -> str:
        """
        Simulates order execution with realistic bid-ask slippage, occasional
        rejection, and enhanced slippage for illiquid/cheap options.

        Fill model:
          1. Rejection check   — random draw against price-tier rejection probability
          2. Bid-ask slippage  — BUY at ask, SELL at bid
          3. Extra slippage    — additional random drift for illiquid options

        is_exit_order, strategy_name, product_override: accepted for
        interface parity with ZerodhaBroker (which uses is_exit_order to
        route exits through a MARKET order, and strategy_name/
        product_override to select MIS vs NRML -- see its
        place_order()/_product_for() for why). None of these are used
        here: PaperBroker always fills synchronously regardless of order
        type or product, so there's no "sits unfilled" state or
        exchange-level product-type distinction to model in paper mode.
        """
        order_id  = str(uuid.uuid4())
        timestamp = datetime.utcnow()

        # Simulated rejection (exchange connectivity, no market maker, stale quote)
        reject_prob, extra_slip_frac = self._fill_simulation(price)
        if random.random() < reject_prob:
            reject_reason = (
                "no market maker quote" if price <= 0.30
                else "exchange order rejection"
            )
            logger.warning(
                f"PaperBroker: {side} {quantity} {symbol} @ ₹{price:.2f} — "
                f"REJECTED ({reject_reason}, simulated {reject_prob:.0%} rate)"
            )
            raise ValueError(f"Order rejected by exchange: {reject_reason}")

        # Apply bid-ask slippage
        half_spread  = self._bid_ask_half_spread(price)
        # Extra random slippage for illiquid options (0 to extra_slip_frac of price)
        extra_slip   = price * extra_slip_frac * random.random() if extra_slip_frac > 0 else 0.0
        fill_price   = price + half_spread + extra_slip if side == "BUY" else price - half_spread - extra_slip
        fill_price   = round(max(fill_price, 0.05), 2)   # floor at NSE min tick
        slippage_ppu = round(fill_price - price, 4)      # per-unit (negative for SELL)

        cost = quantity * fill_price
        fees = self._transaction_fees(side, quantity, fill_price)

        if side == "BUY" and self.balance < cost + fees:
            logger.warning(
                f"PaperBroker: Insufficient funds to BUY {quantity} {symbol} "
                f"@ fill ₹{fill_price:.2f} (expected ₹{price:.2f}). "
                f"Balance: ₹{self.balance:.2f}, Required: ₹{cost + fees:.2f}"
            )
            raise ValueError("Insufficient virtual funds.")

        # Approximate margin check — SELL orders that open or add to a short
        # position (see SHORT_MARGIN_MULTIPLE_OF_PREMIUM above for why this is
        # a proxy, not real SPAN margin). SELL orders that only reduce/close an
        # existing long need no margin — that's just realizing cash.
        if side == "SELL":
            current_qty = self._positions.get(symbol, {}).get("quantity", 0)
            resulting_qty = current_qty - quantity
            if resulting_qty < 0:
                required_margin = abs(resulting_qty) * fill_price * SHORT_MARGIN_MULTIPLE_OF_PREMIUM
                margin_other_symbols = sum(
                    v for k, v in self.margin_blocked.items() if k != symbol
                )
                available_for_margin = self.balance - margin_other_symbols
                if available_for_margin < required_margin:
                    logger.warning(
                        f"PaperBroker: Insufficient margin (approx.) to SELL {quantity} {symbol} "
                        f"@ fill ₹{fill_price:.2f} — resulting short {abs(resulting_qty)} needs "
                        f"~₹{required_margin:,.2f} margin, only ₹{available_for_margin:,.2f} free "
                        f"(balance ₹{self.balance:,.2f} - ₹{margin_other_symbols:,.2f} margin held elsewhere)."
                    )
                    raise ValueError("Insufficient virtual margin (approx.) for short position.")

        order = {
            "order_id":     order_id,
            "symbol":       symbol,
            "side":         side,
            "quantity":     quantity,
            "price":        price,          # engine's expected price
            "fill_price":   fill_price,     # actual execution price (incl. slippage)
            "filled_quantity": quantity,    # always fully filled -- synchronous, no partials
            "slippage":     slippage_ppu,   # fill_price - price per unit
            "fees":         fees,
            "status":       "COMPLETED",
            "timestamp":    timestamp,
        }
        self._orders[order_id] = order
        self._update_position(symbol, side, quantity, fill_price)

        # Recompute (not increment) margin_blocked from the resulting position —
        # self-correcting on partial covers rather than tracking deltas.
        post_qty = self._positions.get(symbol, {}).get("quantity", 0)
        if post_qty < 0:
            self.margin_blocked[symbol] = abs(post_qty) * fill_price * SHORT_MARGIN_MULTIPLE_OF_PREMIUM
        else:
            self.margin_blocked.pop(symbol, None)

        if side == "BUY":
            self.balance -= cost + fees
        else:
            self.balance += cost - fees

        self.total_fees_paid += fees
        slip_pct   = abs(slippage_ppu / price * 100) if price > 0 else 0
        extra_note = f" +extra_slip ₹{extra_slip:.2f}" if extra_slip > 0 else ""
        total_margin = sum(self.margin_blocked.values())
        margin_note  = f" | Margin held (approx.): ₹{total_margin:,.2f}" if total_margin > 0 else ""
        logger.info(
            f"PaperBroker: {side} {quantity} {symbol} "
            f"@ expected ₹{price:.2f} → fill ₹{fill_price:.2f} "
            f"(slip {slippage_ppu:+.3f}, {slip_pct:.1f}%{extra_note}) | "
            f"Fees: ₹{fees:.2f} | Balance: ₹{self.balance:.2f}{margin_note}"
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
