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
        # Tracks active credit spreads: {underlying: {spread metadata dict}}
        self._active_spreads: Dict[str, Dict[str, Any]] = {}
        # Tracks active iron condors: {underlying: {condor metadata dict}}
        self._active_condors: Dict[str, Dict[str, Any]] = {}

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

        # Check spread / condor exits before long-option exits (own exit logic)
        await self._check_spread_exits(active_strategies)
        await self._check_condor_exits(active_strategies)

        # FIX 1 + 3 + 4: Check all open option positions for exits every cycle
        await self._check_open_option_exits(positions, active_strategies)

        # Each strategy gets its own regime-matched symbol pool from Redis
        for strategy_id, strategy in active_strategies.items():
            symbols = await self._get_active_symbols(strategy)
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
        # Spreads/condors closed at square-off; clear tracking state for next day
        self._active_spreads.clear()
        self._active_condors.clear()

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

        # Contracts managed by spread/condor exit routines — skip here to avoid double handling
        spread_contracts: set = set()
        for spread in self._active_spreads.values():
            spread_contracts.add(spread.get("short_contract", ""))
            spread_contracts.add(spread.get("long_contract", ""))
        for condor in self._active_condors.values():
            spread_contracts.update([
                condor.get("put_short_contract", ""),
                condor.get("put_long_contract", ""),
                condor.get("call_short_contract", ""),
                condor.get("call_long_contract", ""),
            ])

        for pos in positions:
            contract = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            entry_premium = float(pos.get("avg_price") or 0)

            if qty == 0 or entry_premium <= 0:
                continue

            if contract in spread_contracts:
                continue  # managed by _check_spread_exits instead

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

        if signal_str in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
            await self._process_credit_spread(strategy, symbol, signal_str, market_data)
            return

        if signal_str == "IRON_CONDOR":
            await self._process_iron_condor(strategy, symbol, market_data)
            return

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

    async def _process_credit_spread(
        self,
        strategy,
        symbol: str,
        spread_type: str,
        market_data: Dict[str, Any],
    ) -> None:
        """
        Enter a credit spread for the given underlying.

        Bull Put Spread  (spread_type='BULL_PUT_SPREAD'):
          SELL ATM PE  + BUY (ATM - 2×interval) PE
          Profits when underlying stays above the short strike.

        Bear Call Spread (spread_type='BEAR_CALL_SPREAD'):
          SELL ATM CE  + BUY (ATM + 2×interval) CE
          Profits when underlying stays below the short strike.

        Both collect net credit = (short premium) - (long premium).
        """
        # Only one spread per underlying at a time
        if symbol in self._active_spreads:
            logger.info(f"[CreditSpread] Already have active spread on {symbol}, skipping.")
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        from src.core.constants import FNO_STRIKE_INTERVALS
        interval = FNO_STRIKE_INTERVALS.get(symbol, 50)
        spread_width = getattr(strategy, "spread_width", 2) * interval

        atm_strike = get_atm_strike(underlying_price, symbol)
        expiry = get_near_month_expiry()
        lot_size = await self._get_lot_size(symbol)
        atr = float(market_data.get("atr14", underlying_price * 0.01))

        # Premium estimates:  ATM ≈ ATR×4,  OTM ≈ ATR×2
        short_premium = round(atr * 4, 2)
        long_premium = round(atr * 2, 2)

        if spread_type == "BULL_PUT_SPREAD":
            short_strike = atm_strike
            long_strike = atm_strike - spread_width
            option_type = "PE"
        else:  # BEAR_CALL_SPREAD
            short_strike = atm_strike
            long_strike = atm_strike + spread_width
            option_type = "CE"

        short_contract = build_option_symbol(symbol, short_strike, option_type, expiry)
        long_contract = build_option_symbol(symbol, long_strike, option_type, expiry)

        net_credit = round(short_premium - long_premium, 2)
        dte = (expiry - now_ist().replace(tzinfo=None)).days

        logger.info(
            f"[CreditSpread] {spread_type} on {symbol} | "
            f"SELL {short_contract} @ Rs{short_premium:.2f} + "
            f"BUY {long_contract} @ Rs{long_premium:.2f} | "
            f"Net credit: Rs{net_credit:.2f} × {lot_size} | DTE: {dte}"
        )

        # Place short leg first (credit collected)
        short_order = await self.order_manager.place_order(
            short_contract, "SELL", lot_size, short_premium
        )
        if not short_order or short_order.order_status != "OPEN":
            logger.warning(f"[CreditSpread] Short leg order failed for {short_contract}")
            return

        # Place long leg (hedge, debit)
        long_order = await self.order_manager.place_order(
            long_contract, "BUY", lot_size, long_premium
        )
        if not long_order or long_order.order_status != "OPEN":
            # Short leg placed but long leg failed — close the short leg immediately
            logger.error(
                f"[CreditSpread] Long leg order failed for {long_contract}. "
                f"Closing short leg {short_contract} to avoid naked short."
            )
            await self.order_manager.place_order(
                short_contract, "BUY", lot_size, short_premium
            )
            return

        self._today_order_count += 2
        self._active_spreads[symbol] = {
            "spread_type": spread_type,
            "short_contract": short_contract,
            "long_contract": long_contract,
            "short_strike": short_strike,
            "long_strike": long_strike,
            "option_type": option_type,
            "short_premium": short_premium,
            "long_premium": long_premium,
            "net_credit": net_credit,
            "lot_size": lot_size,
        }

        await self._notify(
            f"CREDIT SPREAD OPENED\n"
            f"Strategy: {strategy.name}\n"
            f"Type: {spread_type}\n"
            f"Underlying: {symbol} @ Rs{underlying_price:.2f}\n"
            f"SELL {short_contract} @ Rs{short_premium:.2f}\n"
            f"BUY  {long_contract} @ Rs{long_premium:.2f}\n"
            f"Net credit: Rs{net_credit:.2f}/share × {lot_size} = Rs{net_credit * lot_size:,.2f}\n"
            f"Max profit: Rs{net_credit * lot_size:,.2f} | "
            f"Max loss: Rs{(spread_width - net_credit) * lot_size:,.2f} | DTE: {dte}"
        )

    async def _check_spread_exits(self, active_strategies: Dict[str, Any]) -> None:
        """
        Called every cycle before _check_open_option_exits.
        Evaluates exit conditions for all active credit spreads:
          1. DTE < min_dte (default 7) — close to avoid gamma risk near expiry
          2. Strategy profit target — short leg decayed to 25% of sold price
          3. Strategy stop loss — short leg rose to 2× sold price
          4. Strike breach — underlying has crossed the short strike
        When exiting: BUY back the short leg first, then SELL the long leg.
        """
        if not self._active_spreads:
            return

        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        to_remove: List[str] = []

        # Find credit spread strategy (for manage_position parameters)
        cs_strategy = None
        for s in active_strategies.values():
            if s.__class__.__name__ == "CreditSpreadStrategy":
                cs_strategy = s
                break

        for underlying, spread in self._active_spreads.items():
            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            current_price = float(market_data.get("close", 0))
            atr = float(market_data.get("atr14", 0))
            current_short_premium = round(atr * 4, 2) if atr > 0 else spread["short_premium"]

            exit_reason: Optional[str] = None
            min_dte = getattr(cs_strategy, "min_dte", 7) if cs_strategy else 7

            # Priority 1: DTE too close to expiry (gamma risk)
            if dte < min_dte:
                exit_reason = f"DTE={dte} < {min_dte} — close before gamma risk"

            # Priority 2: Strike breach (underlying has moved through the short strike)
            if exit_reason is None and current_price > 0:
                short_strike = spread["short_strike"]
                spread_type = spread["spread_type"]
                if spread_type == "BULL_PUT_SPREAD" and current_price < short_strike:
                    exit_reason = (
                        f"Strike breach: {underlying} @ Rs{current_price:.2f} "
                        f"< short put strike Rs{short_strike}"
                    )
                elif spread_type == "BEAR_CALL_SPREAD" and current_price > short_strike:
                    exit_reason = (
                        f"Strike breach: {underlying} @ Rs{current_price:.2f} "
                        f"> short call strike Rs{short_strike}"
                    )

            # Priority 3: Strategy SL / profit target
            if exit_reason is None and cs_strategy:
                result = cs_strategy.manage_position(
                    {"short_premium": spread["short_premium"]},
                    current_short_premium,
                )
                if result == "EXIT":
                    pnl_pct = (spread["short_premium"] - current_short_premium) / spread["short_premium"] * 100
                    exit_reason = (
                        f"strategy={cs_strategy.name} "
                        f"short: sold Rs{spread['short_premium']:.2f} "
                        f"now Rs{current_short_premium:.2f} ({pnl_pct:+.1f}%)"
                    )

            if exit_reason is None:
                continue

            logger.info(f"[CreditSpread] EXIT {underlying}: {exit_reason}")

            lot_size = spread["lot_size"]
            short_contract = spread["short_contract"]
            long_contract = spread["long_contract"]
            short_premium = spread["short_premium"]
            long_premium = spread["long_premium"]

            # Close: BUY back short leg, SELL long leg
            await self.order_manager.place_order(
                short_contract, "BUY", lot_size, current_short_premium
            )
            long_exit_price = round(atr * 2, 2) if atr > 0 else long_premium
            await self.order_manager.place_order(
                long_contract, "SELL", lot_size, long_exit_price
            )

            net_pnl = (short_premium - current_short_premium - long_premium + long_exit_price) * lot_size
            to_remove.append(underlying)

            await self._notify(
                f"CREDIT SPREAD CLOSED\n"
                f"Underlying: {underlying}\n"
                f"Reason: {exit_reason}\n"
                f"Short: {short_contract} sold @ Rs{short_premium:.2f}, bought back @ Rs{current_short_premium:.2f}\n"
                f"Long:  {long_contract} bought @ Rs{long_premium:.2f}, sold @ Rs{long_exit_price:.2f}\n"
                f"Est. Net PnL: Rs{net_pnl:,.2f}"
            )

        for sym in to_remove:
            del self._active_spreads[sym]

    async def _process_iron_condor(
        self,
        strategy,
        symbol: str,
        market_data: Dict[str, Any],
    ) -> None:
        """
        Enter an iron condor on the given underlying.

        Structure:
          PUT  wing: SELL (ATM - short_offset×interval) PE
                   + BUY  (ATM - short_offset×interval - hedge_offset×interval) PE
          CALL wing: SELL (ATM + short_offset×interval) CE
                   + BUY  (ATM + short_offset×interval + hedge_offset×interval) CE

        Net credit = (short_put_premium + short_call_premium)
                   - (long_put_premium  + long_call_premium)
        """
        if symbol in self._active_condors:
            logger.info(f"[IronCondor] Already have active condor on {symbol}, skipping.")
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        from src.core.constants import FNO_STRIKE_INTERVALS
        interval = FNO_STRIKE_INTERVALS.get(symbol, 50)
        short_offset = getattr(strategy, "short_offset", 1) * interval
        hedge_offset = getattr(strategy, "hedge_offset", 2) * interval

        atm_strike = get_atm_strike(underlying_price, symbol)
        expiry = get_near_month_expiry()
        lot_size = await self._get_lot_size(symbol)
        atr = float(market_data.get("atr14", underlying_price * 0.01))

        # Strike calculation
        put_short_strike = atm_strike - short_offset
        put_long_strike = put_short_strike - hedge_offset
        call_short_strike = atm_strike + short_offset
        call_long_strike = call_short_strike + hedge_offset

        # Premium estimates: OTM options decay with distance from ATM
        # short legs (1 interval OTM) ≈ ATR×3, long legs (3 intervals OTM) ≈ ATR×1
        put_short_premium = round(atr * 3, 2)
        put_long_premium = round(atr * 1, 2)
        call_short_premium = round(atr * 3, 2)
        call_long_premium = round(atr * 1, 2)
        net_credit = round(
            (put_short_premium - put_long_premium) + (call_short_premium - call_long_premium), 2
        )

        put_short_contract = build_option_symbol(symbol, put_short_strike, "PE", expiry)
        put_long_contract = build_option_symbol(symbol, put_long_strike, "PE", expiry)
        call_short_contract = build_option_symbol(symbol, call_short_strike, "CE", expiry)
        call_long_contract = build_option_symbol(symbol, call_long_strike, "CE", expiry)

        dte = (expiry - now_ist().replace(tzinfo=None)).days
        logger.info(
            f"[IronCondor] {symbol} @ Rs{underlying_price:.2f} | "
            f"Put wing: SELL {put_short_contract}@{put_short_premium} / BUY {put_long_contract}@{put_long_premium} | "
            f"Call wing: SELL {call_short_contract}@{call_short_premium} / BUY {call_long_contract}@{call_long_premium} | "
            f"Net credit Rs{net_credit:.2f}×{lot_size} | DTE {dte}"
        )

        # Place all 4 legs — if any fails, unwind the ones already placed
        placed = []
        legs = [
            (put_short_contract,  "SELL", lot_size, put_short_premium),
            (put_long_contract,   "BUY",  lot_size, put_long_premium),
            (call_short_contract, "SELL", lot_size, call_short_premium),
            (call_long_contract,  "BUY",  lot_size, call_long_premium),
        ]
        for contract, side, qty, price in legs:
            order = await self.order_manager.place_order(contract, side, qty, price)
            if not order or order.order_status != "OPEN":
                logger.error(f"[IronCondor] Leg failed: {side} {contract}. Unwinding {len(placed)} legs.")
                for (c, s, q, p) in placed:
                    reverse = "BUY" if s == "SELL" else "SELL"
                    await self.order_manager.place_order(c, reverse, q, p)
                return
            placed.append((contract, side, qty, price))

        self._today_order_count += 4
        self._active_condors[symbol] = {
            "put_short_contract":  put_short_contract,
            "put_long_contract":   put_long_contract,
            "call_short_contract": call_short_contract,
            "call_long_contract":  call_long_contract,
            "put_short_strike":    put_short_strike,
            "call_short_strike":   call_short_strike,
            "put_short_premium":   put_short_premium,
            "put_long_premium":    put_long_premium,
            "call_short_premium":  call_short_premium,
            "call_long_premium":   call_long_premium,
            "net_credit":          net_credit,
            "lot_size":            lot_size,
        }

        wing_spread = short_offset + hedge_offset
        max_loss_per_wing = (wing_spread - (put_short_premium - put_long_premium)) * lot_size

        await self._notify(
            f"IRON CONDOR OPENED\n"
            f"Strategy: {strategy.name}\n"
            f"Underlying: {symbol} @ Rs{underlying_price:.2f} | DTE: {dte}\n"
            f"PUT  wing: SELL {put_short_contract} @ Rs{put_short_premium:.2f} "
            f"+ BUY {put_long_contract} @ Rs{put_long_premium:.2f}\n"
            f"CALL wing: SELL {call_short_contract} @ Rs{call_short_premium:.2f} "
            f"+ BUY {call_long_contract} @ Rs{call_long_premium:.2f}\n"
            f"Net credit: Rs{net_credit:.2f}/share × {lot_size} = Rs{net_credit * lot_size:,.2f}\n"
            f"Max profit: Rs{net_credit * lot_size:,.2f} | "
            f"Max loss: ~Rs{max_loss_per_wing:,.2f}/wing"
        )

    async def _check_condor_exits(self, active_strategies: Dict[str, Any]) -> None:
        """
        Called every cycle. Evaluates exit conditions for all active iron condors.

        Exit triggers (any one fires → close all 4 legs):
          1. DTE < min_dte (default 7) — gamma risk near expiry
          2. Underlying breaches put short strike or call short strike
          3. Either short leg doubles in value (stop loss on that wing)
          4. Both short legs decay to 25% of sold value (75% profit target)
        """
        if not self._active_condors:
            return

        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days
        to_remove: List[str] = []

        ic_strategy = None
        for s in active_strategies.values():
            if s.__class__.__name__ == "IronCondorStrategy":
                ic_strategy = s
                break

        for underlying, condor in self._active_condors.items():
            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            current_price = float(market_data.get("close", 0))
            atr = float(market_data.get("atr14", 0))

            # Current premium estimates
            current_put_short = round(atr * 3, 2) if atr > 0 else condor["put_short_premium"]
            current_call_short = round(atr * 3, 2) if atr > 0 else condor["call_short_premium"]
            current_put_long = round(atr * 1, 2) if atr > 0 else condor["put_long_premium"]
            current_call_long = round(atr * 1, 2) if atr > 0 else condor["call_long_premium"]

            exit_reason: Optional[str] = None
            min_dte = getattr(ic_strategy, "min_dte", 7) if ic_strategy else 7
            profit_close_pct = getattr(ic_strategy, "profit_close_pct", 0.25) if ic_strategy else 0.25
            stop_loss_multiple = getattr(ic_strategy, "stop_loss_multiple", 2.0) if ic_strategy else 2.0

            # Priority 1: DTE too close to expiry
            if dte < min_dte:
                exit_reason = f"DTE={dte} < {min_dte} — close before gamma risk"

            # Priority 2: Strike breach (underlying moved through a short strike)
            if exit_reason is None and current_price > 0:
                if current_price < condor["put_short_strike"]:
                    exit_reason = (
                        f"Put strike breach: {underlying} @ Rs{current_price:.2f} "
                        f"< short put Rs{condor['put_short_strike']}"
                    )
                elif current_price > condor["call_short_strike"]:
                    exit_reason = (
                        f"Call strike breach: {underlying} @ Rs{current_price:.2f} "
                        f"> short call Rs{condor['call_short_strike']}"
                    )

            # Priority 3: Stop loss — either short leg doubled
            if exit_reason is None:
                if current_put_short >= condor["put_short_premium"] * stop_loss_multiple:
                    exit_reason = (
                        f"Put SL: sold Rs{condor['put_short_premium']:.2f}, "
                        f"now Rs{current_put_short:.2f} ({stop_loss_multiple}x)"
                    )
                elif current_call_short >= condor["call_short_premium"] * stop_loss_multiple:
                    exit_reason = (
                        f"Call SL: sold Rs{condor['call_short_premium']:.2f}, "
                        f"now Rs{current_call_short:.2f} ({stop_loss_multiple}x)"
                    )

            # Priority 4: Profit target — both short legs decayed to 25%
            if exit_reason is None:
                put_target_hit = current_put_short <= condor["put_short_premium"] * profit_close_pct
                call_target_hit = current_call_short <= condor["call_short_premium"] * profit_close_pct
                if put_target_hit and call_target_hit:
                    exit_reason = "Both wings at 75% profit — closing condor"

            if exit_reason is None:
                continue

            logger.info(f"[IronCondor] EXIT {underlying}: {exit_reason}")

            lot_size = condor["lot_size"]

            # Close all 4 legs: BUY back short legs, SELL long legs
            await self.order_manager.place_order(
                condor["put_short_contract"], "BUY", lot_size, current_put_short
            )
            await self.order_manager.place_order(
                condor["put_long_contract"], "SELL", lot_size, current_put_long
            )
            await self.order_manager.place_order(
                condor["call_short_contract"], "BUY", lot_size, current_call_short
            )
            await self.order_manager.place_order(
                condor["call_long_contract"], "SELL", lot_size, current_call_long
            )

            net_pnl = (
                (condor["put_short_premium"] - current_put_short)
                + (condor["call_short_premium"] - current_call_short)
                - (condor["put_long_premium"] - current_put_long)
                - (condor["call_long_premium"] - current_call_long)
            ) * lot_size
            to_remove.append(underlying)

            await self._notify(
                f"IRON CONDOR CLOSED\n"
                f"Underlying: {underlying}\n"
                f"Reason: {exit_reason}\n"
                f"Put  short: sold Rs{condor['put_short_premium']:.2f}, closed @ Rs{current_put_short:.2f}\n"
                f"Call short: sold Rs{condor['call_short_premium']:.2f}, closed @ Rs{current_call_short:.2f}\n"
                f"Est. Net PnL: Rs{net_pnl:,.2f}"
            )

        for sym in to_remove:
            del self._active_condors[sym]

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
            # Long positions (qty > 0) → SELL to close; Short legs (qty < 0) → BUY to close
            side = "SELL" if qty > 0 else "BUY"
            entry_premium = float(pos.get("avg_price") or 0)
            underlying = self._get_underlying_from_contract(contract)
            exit_price = entry_premium  # fallback
            if underlying:
                market_data = await self._get_market_data(underlying)
                if market_data:
                    atr = float(market_data.get("atr14", 0))
                    if atr > 0:
                        exit_price = round(atr * 4, 2)
            await self.order_manager.place_order(contract, side, abs(qty), exit_price)
            self._peak_premiums.pop(contract, None)
            closed += 1
            logger.warning(f"Auto square-off: {side} {abs(qty)} {contract}")

        self._active_spreads.clear()
        self._active_condors.clear()
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

    async def _get_active_symbols(self, strategy=None) -> List[str]:
        """
        Return the regime-appropriate symbol pool for the given strategy.
        Each strategy gets its own ranked list from Redis (published by LTPPoller):
          EMA Crossover  → nfo:top5          (high ATR + strong trend)
          Credit Spread  → nfo:top5:spread   (low ATR + EMA directional)
          Iron Condor    → nfo:top5:condor   (low ATR + EMA flat)
        Falls back to the fallback symbols list if Redis is empty.
        """
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
