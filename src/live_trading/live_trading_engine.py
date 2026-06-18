import logging
from typing import Any, Dict, List, Optional

from src.brokers.base import AbstractBroker
from src.core.config import settings
from src.core.enums import SignalType, TradingMode
from src.core.utils import (
    build_option_symbol,
    get_atm_strike,
    get_lot_size,
    get_near_month_expiry,
    is_market_open,
    is_square_off_time,
    now_ist,
)
from src.orders.order_manager import OrderManager
from src.portfolio.portfolio_manager import PortfolioManager
from src.risk.risk_manager import RiskManager
from src.strategies.base import StrategyRegistry

logger = logging.getLogger(__name__)


class LiveTradingEngine:
    """
    Central engine for paper and live trading.
    Wires together: strategies → risk → orders → broker → portfolio.
    Designed to be driven by the scheduler (core/scheduler.py).
    """

    def __init__(
        self,
        broker: AbstractBroker,
        risk_manager: RiskManager,
        order_manager: OrderManager,
        portfolio_manager: PortfolioManager,
        notifier: Any = None,
    ):
        self.broker = broker
        self.risk_manager = risk_manager
        self.order_manager = order_manager
        self.portfolio_manager = portfolio_manager
        self.notifier = notifier
        self.mode = TradingMode(settings.TRADING_MODE)
        self.is_running = False
        self._symbols: List[str] = []

        logger.info(f"LiveTradingEngine initialised — mode: {self.mode.value.upper()}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_symbols(self, symbols: List[str]) -> None:
        self._symbols = symbols
        logger.info(f"Trading symbols set: {symbols}")

    async def start(self) -> None:
        self.is_running = True
        logger.info(f"Trading engine STARTED — {self.mode.value.upper()} mode")
        await self._notify(
            f"Falcon Trader STARTED\n"
            f"Mode: {self.mode.value.upper()}\n"
            f"Symbols: {len(self._symbols)}\n"
            f"Capital: ₹{settings.INITIAL_CAPITAL:,.0f}"
        )

    async def stop(self) -> None:
        self.is_running = False
        logger.info("Trading engine STOPPED")
        await self._notify("Falcon Trader STOPPED")

    # ------------------------------------------------------------------
    # Scheduler callbacks
    # ------------------------------------------------------------------

    async def on_market_open(self) -> None:
        logger.info("Market OPEN — 09:15 IST")
        positions = await self._safe_get_positions()
        await self._notify(
            f"Market OPEN ✅\n"
            f"Mode: {self.mode.value.upper()}\n"
            f"Active strategies: {len(StrategyRegistry.get_active_strategies())}\n"
            f"Open positions: {len(positions)}"
        )

    async def on_market_close(self) -> None:
        logger.info("Market CLOSE — 15:30 IST")
        await self._notify("Market CLOSED — 15:30 IST")

    async def run_signal_cycle(self) -> None:
        """Called every minute by the scheduler. Core trading loop."""
        if not self.is_running:
            return

        if not is_market_open():
            return

        # Auto square-off window: 15:20–15:30
        if is_square_off_time():
            await self._square_off_all()
            return

        active_strategies = StrategyRegistry.get_active_strategies()
        if not active_strategies:
            return

        # Refresh risk state before evaluating signals
        positions = await self._safe_get_positions()
        await self._refresh_risk_state(positions)

        for strategy_id, strategy in active_strategies.items():
            for symbol in self._symbols:
                try:
                    await self._process_signal(strategy, symbol)
                except Exception as exc:
                    logger.error(f"Signal error [{strategy_id}:{symbol}]: {exc}")

    async def sync_orders(self) -> None:
        """Called every 30 s by the scheduler."""
        try:
            await self.order_manager.sync_orders()
        except Exception as exc:
            logger.error(f"Order sync failed: {exc}")

    async def sync_positions(self) -> None:
        """Called every minute by the scheduler."""
        try:
            await self.portfolio_manager.sync_positions()
        except Exception as exc:
            logger.error(f"Position sync failed: {exc}")

    async def send_daily_report(self) -> None:
        """Called at 15:45 IST by the scheduler."""
        positions = await self._safe_get_positions()
        total_pnl = sum(p.get("pnl", p.get("unrealised", 0)) for p in positions)
        await self._notify(
            f"EOD REPORT 📊\n"
            f"Date: {now_ist().strftime('%d-%b-%Y')}\n"
            f"Mode: {self.mode.value.upper()}\n"
            f"Open positions: {len(positions)}\n"
            f"Total PnL: ₹{total_pnl:,.2f}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _process_signal(self, strategy, symbol: str) -> None:
        market_data = await self._get_market_data(symbol)
        if not market_data:
            return

        signal = strategy.generate_signal(market_data)
        if not signal or signal == SignalType.HOLD:
            return

        logger.info(f"Signal [{strategy.name}] {signal} {symbol}")

        if signal in (SignalType.BUY, SignalType.SELL):
            underlying_price = float(market_data.get("close", 0))
            if underlying_price <= 0:
                return

            # signal may be a SignalType enum or a plain string
            signal_str = signal.value if hasattr(signal, "value") else str(signal)

            # Map direction → option type (always BUY the option)
            option_type = "CE" if signal_str == "BUY" else "PE"
            strike = get_atm_strike(underlying_price, symbol)
            expiry = get_near_month_expiry()
            contract = build_option_symbol(symbol, strike, option_type, expiry)
            lot_size = get_lot_size(symbol)

            # Estimate ATM option premium for paper trading:
            # Use ATR-based approximation: ATR × 4 ≈ near-month ATM premium
            atr = float(market_data.get("atr14", underlying_price * 0.01))
            option_price = round(atr * 4, 2)

            order = await self.order_manager.place_order(contract, "BUY", lot_size, option_price)
            if order and order.order_status == "OPEN":
                dte = (expiry - now_ist().replace(tzinfo=None)).days
                await self._notify(
                    f"ORDER PLACED\n"
                    f"Strategy: {strategy.name}\n"
                    f"BUY {lot_size} {contract} @ ₹{option_price:.2f}\n"
                    f"Underlying: {symbol} @ ₹{underlying_price:.2f}\n"
                    f"Strike: {strike} {option_type} | DTE: {dte}"
                )

        elif signal == SignalType.EXIT:
            await self._exit_position(symbol)

    async def _exit_position(self, symbol: str) -> None:
        positions = await self._safe_get_positions()
        for pos in positions:
            if pos.get("symbol") != symbol:
                continue
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            side = "SELL" if qty > 0 else "BUY"
            price = float(pos.get("market_price", pos.get("avg_price", 0)))
            await self.order_manager.place_order(symbol, side, abs(qty), price)
            logger.info(f"EXIT: {side} {abs(qty)} {symbol} @ ₹{price:.2f}")

    async def _square_off_all(self) -> None:
        """Auto square-off all open positions at 15:20 IST."""
        positions = await self._safe_get_positions()
        closed = 0
        for pos in positions:
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            symbol = pos["symbol"]
            side = "SELL" if qty > 0 else "BUY"
            price = float(pos.get("market_price", pos.get("avg_price", 0)))
            await self.order_manager.place_order(symbol, side, abs(qty), price)
            closed += 1
            logger.warning(f"Auto square-off: {side} {abs(qty)} {symbol}")

        if closed:
            await self._notify(f"AUTO SQUARE-OFF ⚠️\nClosed {closed} positions at 15:20 IST")

    async def _refresh_risk_state(self, positions: List[Dict[str, Any]]) -> None:
        realized = sum(p.get("realised", 0) for p in positions)
        unrealized = sum(p.get("unrealised", 0) for p in positions)
        self.risk_manager.update_state(positions, realized, unrealized)

    async def _safe_get_positions(self) -> List[Dict[str, Any]]:
        try:
            return await self.broker.get_positions()
        except Exception as exc:
            logger.error(f"Failed to fetch positions: {exc}")
            return []

    async def _get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetch the latest market data for a symbol.
        Reads from Redis tick cache populated by MarketDataService.
        Returns None if no data available yet.
        """
        # Redis integration is wired at startup via dependency injection.
        # The redis_client is attached by the API startup event handler.
        redis = getattr(self, "_redis", None)
        if redis is None:
            return None
        try:
            import json
            from src.core.constants import REDIS_TICK_PREFIX
            raw = await redis.get(f"{REDIS_TICK_PREFIX}{symbol}")
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.error(f"Redis read failed [{symbol}]: {exc}")
            return None

    def attach_redis(self, redis_client: Any) -> None:
        """Called at startup to wire the Redis client in."""
        self._redis = redis_client

    def _calculate_quantity(self, symbol: str, price: float = 0) -> int:
        """Return the NSE lot size for this symbol. Always trade exactly 1 lot."""
        return get_lot_size(symbol)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def _notify(self, message: str) -> None:
        if self.notifier:
            try:
                await self.notifier.send(message)
            except Exception as exc:
                logger.error(f"Telegram notify failed: {exc}")
