import json
import logging
from typing import Any, Dict, List, Optional

from src.brokers.base import AbstractBroker
from src.core.config import settings
from src.core.constants import (
    FNO_SYMBOLS,
    REDIS_LOT_SIZE_PREFIX,
    REDIS_TICK_PREFIX,
    REDIS_TOP_SYMBOLS_KEY,
    REDIS_TOP_SYMBOLS_CREDIT_SPREAD,
    REDIS_TOP_SYMBOLS_IRON_CONDOR,
)
from src.core.enums import SignalType, TradingMode
from src.core.utils import (
    build_option_symbol,
    estimate_option_premium,
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

_FNO_SYMBOLS_BY_LEN = sorted(FNO_SYMBOLS, key=len, reverse=True)

# Redis keys for persisting spread/condor state across restarts
_REDIS_ACTIVE_SPREADS = "engine:active_spreads"
_REDIS_ACTIVE_CONDORS = "engine:active_condors"


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
        # Max new entries per day (0 = unlimited). Prevents runaway on volatile days.
        self._max_daily_orders: int = settings.MAX_DAILY_ORDERS if hasattr(settings, "MAX_DAILY_ORDERS") else 30
        # Peak premium tracking for trailing stop: {contract: peak_premium}
        self._peak_premiums: Dict[str, float] = {}
        # Active multi-leg structures (persisted to Redis for crash recovery)
        self._active_spreads: Dict[str, Dict[str, Any]] = {}
        self._active_condors: Dict[str, Dict[str, Any]] = {}

        logger.info(f"LiveTradingEngine initialised — mode: {self.mode.value.upper()}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_symbols(self, symbols: List[str]) -> None:
        self._symbols = symbols
        logger.info(f"Trading symbols set (fallback): {symbols}")

    def attach_redis(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def start(self) -> None:
        self.is_running = True
        await self._restore_state()
        logger.info(f"Trading engine STARTED — {self.mode.value.upper()} mode")

    async def stop(self) -> None:
        self.is_running = False
        await self._persist_state()
        logger.info("Trading engine STOPPED")

    # ------------------------------------------------------------------
    # Scheduler callbacks
    # ------------------------------------------------------------------

    async def on_market_open(self) -> None:
        logger.info("Market OPEN — 09:15 IST")
        # Reset daily counters
        self._today_order_count = 0
        self.risk_manager.reset_daily_state()

        # In live mode, verify the Zerodha token is still valid
        if self.mode == TradingMode.LIVE:
            redis = getattr(self, "_redis", None)
            if redis:
                token = await redis.get("zerodha:access_token")
                if not token:
                    logger.critical("LIVE MODE: Zerodha access token missing at market open!")
                    self.risk_manager.activate_kill_switch("Zerodha token missing at market open")
                    await self._notify(
                        "CRITICAL: Zerodha access token not found in Redis at market open.\n"
                        "Kill switch activated — no trades will fire.\n"
                        "Re-run the auth script and manually deactivate the kill switch."
                    )

        await self._notify(f"Market OPEN — {self.mode.value.upper()} mode | "
                           f"Capital: ₹{settings.INITIAL_CAPITAL:,.0f}")

    async def on_market_close(self) -> None:
        logger.info("Market CLOSE — 15:30 IST")

    async def run_signal_cycle(self) -> None:
        """Called every minute by the scheduler. Core trading loop."""
        if not self.is_running:
            return
        if not is_market_open():
            return

        if is_square_off_time():
            await self._square_off_all()
            return

        active_strategies = StrategyRegistry.get_active_strategies()
        if not active_strategies:
            return

        positions = await self._safe_get_positions()
        await self._refresh_risk_state(positions)

        # Exit checks — managed by their own routines
        await self._check_spread_exits(active_strategies)
        await self._check_condor_exits(active_strategies)
        await self._check_open_option_exits(positions, active_strategies)

        # Entry signals — each strategy reads from its own regime-specific symbol pool
        for strategy_id, strategy in active_strategies.items():
            symbols = await self._get_active_symbols(strategy)
            for symbol in symbols:
                try:
                    await self._process_signal(strategy, symbol)
                except Exception as exc:
                    logger.error(f"Signal error [{strategy_id}:{symbol}]: {exc}")

    async def sync_orders(self) -> None:
        try:
            await self.order_manager.sync_orders()
        except Exception as exc:
            logger.error(f"Order sync failed: {exc}")

    async def sync_positions(self) -> None:
        try:
            await self.portfolio_manager.sync_positions()
        except Exception as exc:
            logger.error(f"Position sync failed: {exc}")

    async def send_daily_report(self) -> None:
        """Called at 15:45 IST. Sends EOD email only if trades were placed today."""
        if self._today_order_count == 0:
            logger.info("EOD: no trades today, skipping report email.")
        else:
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
                f"Total PnL: ₹{total_pnl:,.2f}"
            )

        # Persist state then reset for next day
        await self._persist_state()
        self._today_order_count = 0
        self._peak_premiums.clear()
        self._active_spreads.clear()
        self._active_condors.clear()

    # ------------------------------------------------------------------
    # State persistence (crash recovery)
    # ------------------------------------------------------------------

    async def _persist_state(self) -> None:
        """Save active spread/condor tracking dicts to Redis for crash recovery."""
        redis = getattr(self, "_redis", None)
        if not redis:
            return
        try:
            await redis.set(_REDIS_ACTIVE_SPREADS, json.dumps(self._active_spreads))
            await redis.set(_REDIS_ACTIVE_CONDORS, json.dumps(self._active_condors))
        except Exception as e:
            logger.error(f"Failed to persist engine state: {e}")

    async def _restore_state(self) -> None:
        """Load spread/condor state from Redis on startup (survives container restarts)."""
        redis = getattr(self, "_redis", None)
        if not redis:
            return
        try:
            spreads_raw = await redis.get(_REDIS_ACTIVE_SPREADS)
            if spreads_raw:
                self._active_spreads = json.loads(spreads_raw)
                logger.info(f"Restored {len(self._active_spreads)} active spread(s) from Redis")

            condors_raw = await redis.get(_REDIS_ACTIVE_CONDORS)
            if condors_raw:
                self._active_condors = json.loads(condors_raw)
                logger.info(f"Restored {len(self._active_condors)} active condor(s) from Redis")
        except Exception as e:
            logger.error(f"Failed to restore engine state from Redis: {e}")

    # ------------------------------------------------------------------
    # Exit management
    # ------------------------------------------------------------------

    async def _check_open_option_exits(
        self,
        positions: List[Dict[str, Any]],
        active_strategies: Dict[str, Any],
    ) -> None:
        """
        Runs every cycle for each open LONG option position.

        Exit triggers (priority order):
          1. DTE < 4 — time decay protection
          2. Strategy SL / take-profit / trailing stop via manage_position()

        Spread and condor legs are skipped (their own exit routines handle them).
        Updates market_price and unrealized_pnl in DB on every cycle.
        """
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days

        # Build the set of contracts already managed by spread/condor routines
        managed_contracts: set = set()
        for spread in self._active_spreads.values():
            managed_contracts.update([
                spread.get("short_contract", ""), spread.get("long_contract", "")
            ])
        for condor in self._active_condors.values():
            managed_contracts.update([
                condor.get("put_short_contract", ""), condor.get("put_long_contract", ""),
                condor.get("call_short_contract", ""), condor.get("call_long_contract", ""),
            ])

        for pos in positions:
            contract = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            entry_premium = float(pos.get("avg_price") or 0)

            if qty == 0 or entry_premium <= 0:
                continue
            if contract in managed_contracts:
                continue
            if qty < 0:
                continue  # short positions only from spreads/condors which are already skipped

            underlying = self._get_underlying_from_contract(contract)
            if not underlying:
                continue

            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            atr = float(market_data.get("atr14", 0))
            current_premium = estimate_option_premium(atr, dte) if atr > 0 else entry_premium

            # Update DB so dashboard shows live PnL
            await self.portfolio_manager.update_position_market_price(contract, current_premium)

            # Track peak for trailing stop
            peak = self._peak_premiums.get(contract, entry_premium)
            if current_premium > peak:
                peak = current_premium
                self._peak_premiums[contract] = peak

            exit_reason: Optional[str] = None

            if dte < 4:
                exit_reason = f"DTE={dte} — entering illiquid expiry window"

            if exit_reason is None:
                pos_data = {"avg_price": entry_premium, "peak_premium": peak}
                for strategy in active_strategies.values():
                    result = strategy.manage_position(pos_data, current_premium)
                    if result == "EXIT":
                        pnl_pct = (current_premium - entry_premium) / entry_premium * 100
                        exit_reason = (
                            f"strategy={strategy.name} "
                            f"entry=₹{entry_premium:.2f} "
                            f"current=₹{current_premium:.2f} ({pnl_pct:+.1f}%)"
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
                    f"Entry: ₹{entry_premium:.2f} | Exit: ₹{current_premium:.2f}\n"
                    f"Est. PnL: ₹{pnl:,.2f}"
                )

    async def _process_signal(self, strategy, symbol: str) -> None:
        """
        Core signal handler. Generates a signal from the strategy and routes it
        to the appropriate entry method. Guards: daily order limit, duplicate positions.
        """
        market_data = await self._get_market_data(symbol)
        if not market_data:
            return

        signal = strategy.generate_signal(market_data)
        if not signal or signal == SignalType.HOLD:
            return

        signal_str = signal.value if hasattr(signal, "value") else str(signal)
        if signal_str == "HOLD":
            return

        logger.info(f"Signal [{strategy.name}] {signal_str} {symbol}")

        # Route multi-leg strategies
        if signal_str in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
            await self._process_credit_spread(strategy, symbol, signal_str, market_data)
            return

        if signal_str == "IRON_CONDOR":
            await self._process_iron_condor(strategy, symbol, market_data)
            return

        if signal_str not in ("BUY", "SELL"):
            if signal_str == "EXIT":
                await self._exit_all_options_for(symbol)
            return

        # Daily order cap (entries only)
        if self._max_daily_orders > 0 and self._today_order_count >= self._max_daily_orders:
            logger.warning(f"Daily order limit ({self._max_daily_orders}) reached — skipping entry.")
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        option_type = "CE" if signal_str == "BUY" else "PE"
        opposite_type = "PE" if signal_str == "BUY" else "CE"

        await self._close_option_positions(symbol, opposite_type, market_data)

        if await self._has_open_option(symbol, option_type):
            logger.info(f"Already holding {option_type} on {symbol}, skipping entry.")
            return

        strike = get_atm_strike(underlying_price, symbol)
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        lot_size = await self._get_lot_size(symbol)
        atr = float(market_data.get("atr14", underlying_price * 0.01))
        option_price = estimate_option_premium(atr, dte)

        contract = build_option_symbol(symbol, strike, option_type, expiry)
        order = await self.order_manager.place_order(contract, "BUY", lot_size, option_price)
        if order and order.order_status == "OPEN":
            self._today_order_count += 1
            self._peak_premiums[contract] = option_price
            await self._notify(
                f"ORDER PLACED\n"
                f"Strategy: {strategy.name}\n"
                f"BUY {lot_size} {contract} @ ₹{option_price:.2f}\n"
                f"Underlying: {symbol} @ ₹{underlying_price:.2f}\n"
                f"Strike: {strike} {option_type} | DTE: {dte}"
            )

    async def _close_option_positions(
        self, underlying: str, option_type: str, market_data: Dict
    ) -> None:
        """Close all open long positions of a given type (CE/PE) for an underlying."""
        positions = await self._safe_get_positions()
        atr = float(market_data.get("atr14", 0))
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days

        for pos in positions:
            contract = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            if qty <= 0:
                continue
            if not (contract.startswith(underlying) and contract.endswith(option_type)):
                continue
            entry_premium = float(pos.get("avg_price") or 0)
            exit_price = estimate_option_premium(atr, dte) if atr > 0 else entry_premium
            await self.order_manager.place_order(contract, "SELL", abs(qty), exit_price)
            self._peak_premiums.pop(contract, None)
            logger.info(f"REVERSAL EXIT: SELL {contract} @ ₹{exit_price:.2f}")
            await self._notify(f"REVERSAL EXIT\nContract: {contract}\nClosed {option_type} before entering opposite side")

    async def _process_credit_spread(
        self, strategy, symbol: str, spread_type: str, market_data: Dict[str, Any]
    ) -> None:
        """
        Enter a credit spread.
        Bull Put Spread: SELL ATM PE + BUY (ATM - 2×interval) PE
        Bear Call Spread: SELL ATM CE + BUY (ATM + 2×interval) CE
        """
        if symbol in self._active_spreads:
            return
        if self._max_daily_orders > 0 and self._today_order_count >= self._max_daily_orders:
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        from src.core.constants import FNO_STRIKE_INTERVALS
        interval = FNO_STRIKE_INTERVALS.get(symbol, 50)
        spread_width = getattr(strategy, "spread_width", 2) * interval

        atm_strike = get_atm_strike(underlying_price, symbol)
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        lot_size = await self._get_lot_size(symbol)
        atr = float(market_data.get("atr14", underlying_price * 0.01))

        short_premium = estimate_option_premium(atr, dte, otm_intervals=0)   # ATM
        long_premium = estimate_option_premium(atr, dte, otm_intervals=2)    # 2 intervals OTM

        if spread_type == "BULL_PUT_SPREAD":
            short_strike, long_strike, opt = atm_strike, atm_strike - spread_width, "PE"
        else:
            short_strike, long_strike, opt = atm_strike, atm_strike + spread_width, "CE"

        short_contract = build_option_symbol(symbol, short_strike, opt, expiry)
        long_contract = build_option_symbol(symbol, long_strike, opt, expiry)
        net_credit = round(short_premium - long_premium, 2)

        logger.info(
            f"[CreditSpread] {spread_type} {symbol} | "
            f"SELL {short_contract}@₹{short_premium} BUY {long_contract}@₹{long_premium} | "
            f"credit=₹{net_credit}×{lot_size} DTE={dte}"
        )

        short_order = await self.order_manager.place_order(
            short_contract, "SELL", lot_size, short_premium, is_spread_leg=False
        )
        if not short_order or short_order.order_status != "OPEN":
            logger.warning(f"[CreditSpread] Short leg failed: {short_contract}")
            return

        long_order = await self.order_manager.place_order(
            long_contract, "BUY", lot_size, long_premium, is_spread_leg=True
        )
        if not long_order or long_order.order_status != "OPEN":
            logger.error(f"[CreditSpread] Long leg failed: {long_contract}. Unwinding short leg.")
            await self.order_manager.place_order(short_contract, "BUY", lot_size, short_premium, is_spread_leg=True)
            return

        self._today_order_count += 2
        self._active_spreads[symbol] = {
            "spread_type": spread_type,
            "short_contract": short_contract,
            "long_contract": long_contract,
            "short_strike": short_strike,
            "long_strike": long_strike,
            "option_type": opt,
            "short_premium": short_premium,
            "long_premium": long_premium,
            "net_credit": net_credit,
            "lot_size": lot_size,
        }
        await self._persist_state()

        await self._notify(
            f"CREDIT SPREAD OPENED\n"
            f"Strategy: {strategy.name} | Type: {spread_type}\n"
            f"Underlying: {symbol} @ ₹{underlying_price:.2f} | DTE: {dte}\n"
            f"SELL {short_contract} @ ₹{short_premium:.2f}\n"
            f"BUY  {long_contract} @ ₹{long_premium:.2f}\n"
            f"Net credit: ₹{net_credit:.2f}/share × {lot_size} = ₹{net_credit * lot_size:,.2f}\n"
            f"Max profit: ₹{net_credit * lot_size:,.2f} | "
            f"Max loss: ₹{(spread_width - net_credit) * lot_size:,.2f}"
        )

    async def _check_spread_exits(self, active_strategies: Dict[str, Any]) -> None:
        """Exit active credit spreads when DTE, strike breach, SL, or profit target hit."""
        if not self._active_spreads:
            return

        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        to_remove: List[str] = []

        cs_strategy = next(
            (s for s in active_strategies.values() if s.__class__.__name__ == "CreditSpreadStrategy"),
            None,
        )

        for underlying, spread in self._active_spreads.items():
            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            current_price = float(market_data.get("close", 0))
            atr = float(market_data.get("atr14", 0))
            current_short_premium = estimate_option_premium(atr, dte) if atr > 0 else spread["short_premium"]
            current_long_premium = estimate_option_premium(atr, dte, otm_intervals=2) if atr > 0 else spread["long_premium"]

            exit_reason: Optional[str] = None
            min_dte = getattr(cs_strategy, "min_dte", 7) if cs_strategy else 7

            if dte < min_dte:
                exit_reason = f"DTE={dte} < {min_dte} — close before gamma risk"

            if exit_reason is None and current_price > 0:
                short_strike = spread["short_strike"]
                if spread["spread_type"] == "BULL_PUT_SPREAD" and current_price < short_strike:
                    exit_reason = f"Strike breach: {underlying} ₹{current_price:.2f} < put short ₹{short_strike}"
                elif spread["spread_type"] == "BEAR_CALL_SPREAD" and current_price > short_strike:
                    exit_reason = f"Strike breach: {underlying} ₹{current_price:.2f} > call short ₹{short_strike}"

            if exit_reason is None and cs_strategy:
                result = cs_strategy.manage_position(
                    {"short_premium": spread["short_premium"]}, current_short_premium
                )
                if result == "EXIT":
                    pnl_pct = (spread["short_premium"] - current_short_premium) / spread["short_premium"] * 100
                    exit_reason = (
                        f"strategy={cs_strategy.name} "
                        f"short sold ₹{spread['short_premium']:.2f} now ₹{current_short_premium:.2f} ({pnl_pct:+.1f}%)"
                    )

            if exit_reason is None:
                continue

            logger.info(f"[CreditSpread] EXIT {underlying}: {exit_reason}")
            lot_size = spread["lot_size"]
            await self.order_manager.place_order(spread["short_contract"], "BUY", lot_size, current_short_premium, is_spread_leg=True)
            await self.order_manager.place_order(spread["long_contract"], "SELL", lot_size, current_long_premium, is_spread_leg=True)

            net_pnl = (
                (spread["short_premium"] - current_short_premium)
                - (spread["long_premium"] - current_long_premium)
            ) * lot_size
            to_remove.append(underlying)
            await self._notify(
                f"CREDIT SPREAD CLOSED\n"
                f"Underlying: {underlying}\nReason: {exit_reason}\n"
                f"Short: sold ₹{spread['short_premium']:.2f}, closed ₹{current_short_premium:.2f}\n"
                f"Long:  paid ₹{spread['long_premium']:.2f}, sold ₹{current_long_premium:.2f}\n"
                f"Est. Net PnL: ₹{net_pnl:,.2f}"
            )

        for sym in to_remove:
            del self._active_spreads[sym]
        if to_remove:
            await self._persist_state()

    async def _process_iron_condor(
        self, strategy, symbol: str, market_data: Dict[str, Any]
    ) -> None:
        """
        Enter an iron condor (4 legs).
        PUT wing:  SELL (ATM-1×interval) PE + BUY (ATM-3×interval) PE
        CALL wing: SELL (ATM+1×interval) CE + BUY (ATM+3×interval) CE
        Unwinds all placed legs atomically if any leg order fails.
        """
        if symbol in self._active_condors:
            return
        if self._max_daily_orders > 0 and self._today_order_count >= self._max_daily_orders:
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        from src.core.constants import FNO_STRIKE_INTERVALS
        interval = FNO_STRIKE_INTERVALS.get(symbol, 50)
        short_offset = getattr(strategy, "short_offset", 1) * interval
        hedge_offset = getattr(strategy, "hedge_offset", 2) * interval

        atm = get_atm_strike(underlying_price, symbol)
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        lot_size = await self._get_lot_size(symbol)
        atr = float(market_data.get("atr14", underlying_price * 0.01))

        put_short_strike = atm - short_offset
        put_long_strike = put_short_strike - hedge_offset
        call_short_strike = atm + short_offset
        call_long_strike = call_short_strike + hedge_offset

        put_short_prem = estimate_option_premium(atr, dte, otm_intervals=1)
        put_long_prem = estimate_option_premium(atr, dte, otm_intervals=3)
        call_short_prem = estimate_option_premium(atr, dte, otm_intervals=1)
        call_long_prem = estimate_option_premium(atr, dte, otm_intervals=3)
        net_credit = round((put_short_prem - put_long_prem) + (call_short_prem - call_long_prem), 2)

        psc = build_option_symbol(symbol, put_short_strike, "PE", expiry)
        plc = build_option_symbol(symbol, put_long_strike, "PE", expiry)
        csc = build_option_symbol(symbol, call_short_strike, "CE", expiry)
        clc = build_option_symbol(symbol, call_long_strike, "CE", expiry)

        legs = [
            (psc, "SELL", put_short_prem, False),
            (plc, "BUY",  put_long_prem,  True),
            (csc, "SELL", call_short_prem, True),
            (clc, "BUY",  call_long_prem,  True),
        ]
        placed = []
        for contract, side, price, is_leg in legs:
            order = await self.order_manager.place_order(contract, side, lot_size, price, is_spread_leg=is_leg)
            if not order or order.order_status != "OPEN":
                logger.error(f"[IronCondor] Leg failed: {side} {contract}. Unwinding {len(placed)} legs.")
                for (c, s, p, _) in placed:
                    reverse = "BUY" if s == "SELL" else "SELL"
                    await self.order_manager.place_order(c, reverse, lot_size, p, is_spread_leg=True)
                return
            placed.append((contract, side, price, is_leg))

        self._today_order_count += 4
        self._active_condors[symbol] = {
            "put_short_contract":  psc,  "put_long_contract":   plc,
            "call_short_contract": csc,  "call_long_contract":  clc,
            "put_short_strike":    put_short_strike,
            "call_short_strike":   call_short_strike,
            "put_short_premium":   put_short_prem,
            "put_long_premium":    put_long_prem,
            "call_short_premium":  call_short_prem,
            "call_long_premium":   call_long_prem,
            "net_credit":          net_credit,
            "lot_size":            lot_size,
        }
        await self._persist_state()

        wing_spread = short_offset + hedge_offset
        max_loss_per_wing = (wing_spread - (put_short_prem - put_long_prem)) * lot_size
        await self._notify(
            f"IRON CONDOR OPENED\n"
            f"Strategy: {strategy.name}\n"
            f"Underlying: {symbol} @ ₹{underlying_price:.2f} | DTE: {dte}\n"
            f"PUT  wing: SELL {psc} @ ₹{put_short_prem:.2f} + BUY {plc} @ ₹{put_long_prem:.2f}\n"
            f"CALL wing: SELL {csc} @ ₹{call_short_prem:.2f} + BUY {clc} @ ₹{call_long_prem:.2f}\n"
            f"Net credit: ₹{net_credit:.2f}/share × {lot_size} = ₹{net_credit * lot_size:,.2f}\n"
            f"Max profit: ₹{net_credit * lot_size:,.2f} | Max loss/wing: ~₹{max_loss_per_wing:,.2f}"
        )

    async def _check_condor_exits(self, active_strategies: Dict[str, Any]) -> None:
        """Exit active iron condors on DTE, strike breach, SL, or both wings at profit target."""
        if not self._active_condors:
            return

        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        to_remove: List[str] = []

        ic_strategy = next(
            (s for s in active_strategies.values() if s.__class__.__name__ == "IronCondorStrategy"),
            None,
        )

        for underlying, condor in self._active_condors.items():
            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            current_price = float(market_data.get("close", 0))
            atr = float(market_data.get("atr14", 0))

            cur_put_short  = estimate_option_premium(atr, dte, otm_intervals=1) if atr > 0 else condor["put_short_premium"]
            cur_put_long   = estimate_option_premium(atr, dte, otm_intervals=3) if atr > 0 else condor["put_long_premium"]
            cur_call_short = estimate_option_premium(atr, dte, otm_intervals=1) if atr > 0 else condor["call_short_premium"]
            cur_call_long  = estimate_option_premium(atr, dte, otm_intervals=3) if atr > 0 else condor["call_long_premium"]

            min_dte = getattr(ic_strategy, "min_dte", 7) if ic_strategy else 7
            profit_pct = getattr(ic_strategy, "profit_close_pct", 0.25) if ic_strategy else 0.25
            sl_mult = getattr(ic_strategy, "stop_loss_multiple", 2.0) if ic_strategy else 2.0

            exit_reason: Optional[str] = None

            if dte < min_dte:
                exit_reason = f"DTE={dte} < {min_dte} — close before gamma risk"

            if exit_reason is None and current_price > 0:
                if current_price < condor["put_short_strike"]:
                    exit_reason = f"Put breach: {underlying} ₹{current_price:.2f} < ₹{condor['put_short_strike']}"
                elif current_price > condor["call_short_strike"]:
                    exit_reason = f"Call breach: {underlying} ₹{current_price:.2f} > ₹{condor['call_short_strike']}"

            if exit_reason is None:
                if cur_put_short >= condor["put_short_premium"] * sl_mult:
                    exit_reason = f"Put SL: sold ₹{condor['put_short_premium']:.2f}, now ₹{cur_put_short:.2f}"
                elif cur_call_short >= condor["call_short_premium"] * sl_mult:
                    exit_reason = f"Call SL: sold ₹{condor['call_short_premium']:.2f}, now ₹{cur_call_short:.2f}"

            if exit_reason is None:
                put_ok = cur_put_short <= condor["put_short_premium"] * profit_pct
                call_ok = cur_call_short <= condor["call_short_premium"] * profit_pct
                if put_ok and call_ok:
                    exit_reason = "Both wings at 75%+ profit — closing condor"

            if exit_reason is None:
                continue

            logger.info(f"[IronCondor] EXIT {underlying}: {exit_reason}")
            lot_size = condor["lot_size"]
            await self.order_manager.place_order(condor["put_short_contract"],  "BUY",  lot_size, cur_put_short,  is_spread_leg=True)
            await self.order_manager.place_order(condor["put_long_contract"],   "SELL", lot_size, cur_put_long,   is_spread_leg=True)
            await self.order_manager.place_order(condor["call_short_contract"], "BUY",  lot_size, cur_call_short, is_spread_leg=True)
            await self.order_manager.place_order(condor["call_long_contract"],  "SELL", lot_size, cur_call_long,  is_spread_leg=True)

            net_pnl = (
                (condor["put_short_premium"] - cur_put_short)
                + (condor["call_short_premium"] - cur_call_short)
                - (condor["put_long_premium"] - cur_put_long)
                - (condor["call_long_premium"] - cur_call_long)
            ) * lot_size
            to_remove.append(underlying)
            await self._notify(
                f"IRON CONDOR CLOSED\n"
                f"Underlying: {underlying}\nReason: {exit_reason}\n"
                f"Put short:  sold ₹{condor['put_short_premium']:.2f}, closed ₹{cur_put_short:.2f}\n"
                f"Call short: sold ₹{condor['call_short_premium']:.2f}, closed ₹{cur_call_short:.2f}\n"
                f"Est. Net PnL: ₹{net_pnl:,.2f}"
            )

        for sym in to_remove:
            del self._active_condors[sym]
        if to_remove:
            await self._persist_state()

    async def _exit_all_options_for(self, underlying: str) -> None:
        """Exit all open long option positions for the given underlying (EXIT signal)."""
        positions = await self._safe_get_positions()
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        for pos in positions:
            contract = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            if qty <= 0 or not contract.startswith(underlying):
                continue
            entry_premium = float(pos.get("avg_price") or 0)
            market_data = await self._get_market_data(underlying)
            atr = float(market_data.get("atr14", 0)) if market_data else 0
            exit_price = estimate_option_premium(atr, dte) if atr > 0 else entry_premium
            await self.order_manager.place_order(contract, "SELL", abs(qty), exit_price)
            self._peak_premiums.pop(contract, None)
            logger.info(f"EXIT signal: SELL {contract} @ ₹{exit_price:.2f}")

    async def _square_off_all(self) -> None:
        """Auto square-off all open positions at 15:20 IST."""
        positions = await self._safe_get_positions()
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        closed = 0

        for pos in positions:
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            contract = pos["symbol"]
            # Long positions → SELL; Short legs (qty<0) → BUY to close
            side = "SELL" if qty > 0 else "BUY"
            entry_premium = float(pos.get("avg_price") or 0)
            underlying = self._get_underlying_from_contract(contract)
            exit_price = entry_premium
            if underlying:
                market_data = await self._get_market_data(underlying)
                if market_data:
                    atr = float(market_data.get("atr14", 0))
                    if atr > 0:
                        exit_price = estimate_option_premium(atr, dte)
            await self.order_manager.place_order(contract, side, abs(qty), exit_price)
            self._peak_premiums.pop(contract, None)
            closed += 1
            logger.warning(f"Auto square-off: {side} {abs(qty)} {contract}")

        self._active_spreads.clear()
        self._active_condors.clear()
        await self._persist_state()

        if closed:
            await self._notify(f"AUTO SQUARE-OFF\nClosed {closed} option positions at 15:20 IST")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_underlying_from_contract(self, contract: str) -> Optional[str]:
        for sym in _FNO_SYMBOLS_BY_LEN:
            if contract.startswith(sym):
                return sym
        return None

    async def _has_open_option(self, underlying: str, option_type: str) -> bool:
        positions = await self._safe_get_positions()
        return any(
            pos.get("quantity", 0) > 0
            and pos.get("symbol", "").startswith(underlying)
            and pos.get("symbol", "").endswith(option_type)
            for pos in positions
        )

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

    async def _get_active_symbols(self, strategy=None) -> List[str]:
        """Return the regime-appropriate symbol pool for the given strategy."""
        redis = getattr(self, "_redis", None)
        if redis:
            class_name = strategy.__class__.__name__ if strategy else ""
            if class_name == "CreditSpreadStrategy":
                key = REDIS_TOP_SYMBOLS_CREDIT_SPREAD
            elif class_name == "IronCondorStrategy":
                key = REDIS_TOP_SYMBOLS_IRON_CONDOR
            else:
                key = REDIS_TOP_SYMBOLS_KEY
            try:
                raw = await redis.get(key)
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
