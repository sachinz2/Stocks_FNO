import json
import logging
from typing import Any, Dict, List, Optional

from src.brokers.base import AbstractBroker
from src.core.config import settings
from src.core.constants import FNO_SYMBOLS, REDIS_LOT_SIZE_PREFIX, REDIS_TICK_PREFIX, REDIS_TOP_SYMBOLS_KEY
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

# Sorted once at import time — longest symbol first so BAJFINANCE matches before BAJ
_FNO_SYMBOLS_BY_LEN = sorted(FNO_SYMBOLS, key=len, reverse=True)


class LiveTradingEngine:
    """
    Central engine for paper and live trading.
    Wires together: strategies → risk → orders → broker → portfolio.
    Driven by APScheduler (core/scheduler.py).
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
        self._today_order_count: int = 0
        # Tracks peak premium per contract for trailing stop: {contract: peak_premium}
        self._peak_premiums: Dict[str, float] = {}

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

    async def stop(self) -> None:
        self.is_running = False
        logger.info("Trading engine STOPPED")

    # ------------------------------------------------------------------
    # Scheduler callbacks
    # ------------------------------------------------------------------

    async def on_market_open(self) -> None:
        logger.info("Market OPEN — 09:15 IST")

    async def on_market_close(self) -> None:
        logger.info("Market CLOSE — 15:30 IST")

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

        positions = await self._safe_get_positions()
        await self._refresh_risk_state(positions)

        # FIX 1 + 3 + 4: Check all open option positions for exits every cycle
        await self._check_open_option_exits(positions, active_strategies)

        # Generate new entry signals on top-ranked symbols
        symbols = await self._get_active_symbols()
        for strategy_id, strategy in active_strategies.items():
            for symbol in symbols:
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
        """Called at 15:45 IST. Sends EOD email only if trades were placed today."""
        if self._today_order_count == 0:
            logger.info("EOD: no trades today, skipping report email.")
            return

        positions = await self._safe_get_positions()
        total_pnl = sum(
            p.get("unrealized_pnl", 0) + p.get("realized_pnl", 0) for p in positions
        )
        await self._notify(
            f"EOD REPORT\n"
            f"Date: {now_ist().strftime('%d-%b-%Y')}\n"
            f"Mode: {self.mode.value.upper()}\n"
            f"Orders today: {self._today_order_count}\n"
            f"Open positions: {len(positions)}\n"
            f"Total PnL: Rs{total_pnl:,.2f}"
        )
        self._today_order_count = 0
        self._peak_premiums.clear()

    # ------------------------------------------------------------------
    # Exit management
    # ------------------------------------------------------------------

    async def _check_open_option_exits(
        self,
        positions: List[Dict[str, Any]],
        active_strategies: Dict[str, Any],
    ) -> None:
        """
        FIX 1, 3, 4 — Runs every cycle for each open option position:

        Exit triggers (checked in priority order):
          1. DTE < 4          — time decay protection (roll rule)
          2. Strategy SL/TP   — manage_position() called with current premium estimate
             a. Premium fell 50% from entry → stop loss
             b. Premium rose 100% (2×) from entry → take profit
             c. Premium fell 25% from its own peak → trailing stop
        """
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days

        for pos in positions:
            contract = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            entry_premium = float(pos.get("avg_price") or 0)

            if qty == 0 or entry_premium <= 0:
                continue

            underlying = self._get_underlying_from_contract(contract)
            if not underlying:
                continue  # not one of our option contracts

            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            atr = float(market_data.get("atr14", 0))
            current_premium = round(atr * 4, 2) if atr > 0 else entry_premium

            # Update peak premium for trailing stop
            peak = self._peak_premiums.get(contract, entry_premium)
            if current_premium > peak:
                peak = current_premium
                self._peak_premiums[contract] = peak

            exit_reason: Optional[str] = None

            # Priority 1: time-decay exit
            if dte < 4:
                exit_reason = f"DTE={dte} — entering illiquid expiry window"

            # Priority 2: strategy-driven SL / TP / trailing stop
            if exit_reason is None:
                pos_data = {
                    "avg_price": entry_premium,
                    "peak_premium": peak,
                }
                for strategy in active_strategies.values():
                    result = strategy.manage_position(pos_data, current_premium)
                    if result == "EXIT":
                        pnl_pct = (current_premium - entry_premium) / entry_premium * 100
                        exit_reason = (
                            f"strategy={strategy.name} "
                            f"entry=Rs{entry_premium:.2f} "
                            f"current=Rs{current_premium:.2f} "
                            f"({pnl_pct:+.1f}%)"
                        )
                        break

            if exit_reason:
                logger.info(f"EXIT [{contract}]: {exit_reason}")
                await self.order_manager.place_order(contract, "SELL", abs(qty), current_premium)
                self._peak_premiums.pop(contract, None)
                pnl = (current_premium - entry_premium) * abs(qty)
                await self._notify(
                    f"POSITION CLOSED\n"
                    f"Contract: {contract}\n"
                    f"Reason: {exit_reason}\n"
                    f"Entry: Rs{entry_premium:.2f} | Exit: Rs{current_premium:.2f}\n"
                    f"Est. PnL: Rs{pnl:,.2f}"
                )

    async def _process_signal(self, strategy, symbol: str) -> None:
        """
        FIX 2 — Handles reversal and duplicate-position guard before entering.
        BUY signal → buy CE (close any open PE on this underlying first).
        SELL signal → buy PE (close any open CE on this underlying first).
        """
        market_data = await self._get_market_data(symbol)
        if not market_data:
            return

        signal = strategy.generate_signal(market_data)
        if not signal or signal == SignalType.HOLD:
            return

        signal_str = signal.value if hasattr(signal, "value") else str(signal)
        logger.info(f"Signal [{strategy.name}] {signal_str} {symbol}")

        if signal_str not in ("BUY", "SELL"):
            if signal == SignalType.EXIT:
                await self._exit_all_options_for(symbol)
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        option_type = "CE" if signal_str == "BUY" else "PE"
        opposite_type = "PE" if signal_str == "BUY" else "CE"

        # FIX 2a: Exit opposite position (reversal)
        await self._close_option_positions(symbol, opposite_type, market_data)

        # FIX 2b: Don't double-enter same direction
        if await self._has_open_option(symbol, option_type):
            logger.info(f"Already holding {option_type} on {symbol}, skipping entry.")
            return

        strike = get_atm_strike(underlying_price, symbol)
        expiry = get_near_month_expiry()
        contract = build_option_symbol(symbol, strike, option_type, expiry)
        lot_size = await self._get_lot_size(symbol)

        atr = float(market_data.get("atr14", underlying_price * 0.01))
        option_price = round(atr * 4, 2)

        order = await self.order_manager.place_order(contract, "BUY", lot_size, option_price)
        if order and order.order_status == "OPEN":
            self._today_order_count += 1
            self._peak_premiums[contract] = option_price
            dte = (expiry - now_ist().replace(tzinfo=None)).days
            await self._notify(
                f"ORDER PLACED\n"
                f"Strategy: {strategy.name}\n"
                f"BUY {lot_size} {contract} @ Rs{option_price:.2f}\n"
                f"Underlying: {symbol} @ Rs{underlying_price:.2f}\n"
                f"Strike: {strike} {option_type} | DTE: {dte}"
            )

    async def _close_option_positions(
        self, underlying: str, option_type: str, market_data: Dict
    ) -> None:
        """Close all open positions of a given type (CE/PE) for an underlying."""
        positions = await self._safe_get_positions()
        atr = float(market_data.get("atr14", 0))

        for pos in positions:
            contract = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            if not (contract.startswith(underlying) and contract.endswith(option_type)):
                continue

            entry_premium = float(pos.get("avg_price") or 0)
            exit_price = round(atr * 4, 2) if atr > 0 else entry_premium
            await self.order_manager.place_order(contract, "SELL", abs(qty), exit_price)
            self._peak_premiums.pop(contract, None)
            logger.info(f"REVERSAL EXIT: SELL {contract} @ Rs{exit_price:.2f}")
            await self._notify(
                f"REVERSAL EXIT\n"
                f"Contract: {contract}\n"
                f"Closed {option_type} before entering opposite side"
            )

    async def _exit_all_options_for(self, underlying: str) -> None:
        """Exit all open option positions for the given underlying (EXIT signal)."""
        positions = await self._safe_get_positions()
        for pos in positions:
            contract = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            if qty == 0 or not contract.startswith(underlying):
                continue
            entry_premium = float(pos.get("avg_price") or 0)
            market_data = await self._get_market_data(underlying)
            atr = float(market_data.get("atr14", 0)) if market_data else 0
            exit_price = round(atr * 4, 2) if atr > 0 else entry_premium
            await self.order_manager.place_order(contract, "SELL", abs(qty), exit_price)
            self._peak_premiums.pop(contract, None)
            logger.info(f"EXIT signal: SELL {contract} @ Rs{exit_price:.2f}")

    async def _square_off_all(self) -> None:
        """Auto square-off all open positions at 15:20 IST."""
        positions = await self._safe_get_positions()
        closed = 0
        for pos in positions:
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            contract = pos["symbol"]
            entry_premium = float(pos.get("avg_price") or 0)
            underlying = self._get_underlying_from_contract(contract)
            exit_price = entry_premium  # fallback
            if underlying:
                market_data = await self._get_market_data(underlying)
                if market_data:
                    atr = float(market_data.get("atr14", 0))
                    if atr > 0:
                        exit_price = round(atr * 4, 2)
            await self.order_manager.place_order(contract, "SELL", abs(qty), exit_price)
            self._peak_premiums.pop(contract, None)
            closed += 1
            logger.warning(f"Auto square-off: SELL {abs(qty)} {contract}")

        if closed:
            await self._notify(f"AUTO SQUARE-OFF\nClosed {closed} option positions at 15:20 IST")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_underlying_from_contract(self, contract: str) -> Optional[str]:
        """
        Extract the NSE underlying symbol from an option contract string.
        e.g. 'HDFCBANK25JUL800CE' → 'HDFCBANK'
             'BAJAJ-AUTO25JUL5000CE' → 'BAJAJ-AUTO'
        Uses longest-match to avoid BAJFINANCE matching as BAJ.
        """
        for sym in _FNO_SYMBOLS_BY_LEN:
            if contract.startswith(sym):
                return sym
        return None

    async def _has_open_option(self, underlying: str, option_type: str) -> bool:
        """True if there is already an open position in this underlying + CE/PE."""
        positions = await self._safe_get_positions()
        for pos in positions:
            contract = pos.get("symbol", "")
            if (
                pos.get("quantity", 0) != 0
                and contract.startswith(underlying)
                and contract.endswith(option_type)
            ):
                return True
        return False

    async def _refresh_risk_state(self, positions: List[Dict[str, Any]]) -> None:
        realized = sum(p.get("realized_pnl", 0) for p in positions)
        unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
        self.risk_manager.update_state(positions, realized, unrealized)

    async def _safe_get_positions(self) -> List[Dict[str, Any]]:
        try:
            return await self.broker.get_positions()
        except Exception as exc:
            logger.error(f"Failed to fetch positions: {exc}")
            return []

    async def _get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        redis = getattr(self, "_redis", None)
        if redis is None:
            return None
        try:
            raw = await redis.get(f"{REDIS_TICK_PREFIX}{symbol}")
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.error(f"Redis read failed [{symbol}]: {exc}")
            return None

    def attach_redis(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def _get_lot_size(self, symbol: str) -> int:
        redis = getattr(self, "_redis", None)
        if redis:
            try:
                val = await redis.get(f"{REDIS_LOT_SIZE_PREFIX}{symbol}")
                if val:
                    return int(val)
            except Exception:
                pass
        return get_lot_size(symbol)

    async def _get_active_symbols(self) -> List[str]:
        redis = getattr(self, "_redis", None)
        if redis:
            try:
                raw = await redis.get(REDIS_TOP_SYMBOLS_KEY)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        return self._symbols

    async def _notify(self, message: str) -> None:
        if self.notifier:
            try:
                await self.notifier.send(message)
            except Exception as exc:
                logger.error(f"Notify failed: {exc}")
