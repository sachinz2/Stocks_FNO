import asyncio
import json
import logging
from datetime import datetime
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
from src.risk.strategy_monitor import StrategyMonitor
from src.risk.portfolio_analyzer import PortfolioAnalyzer
from src.market_data.regime_detector import MarketRegimeDetector
from src.market_data.rs_ranker import RSRanker
from src.strategies.base import StrategyRegistry

logger = logging.getLogger(__name__)

_FNO_SYMBOLS_BY_LEN = sorted(FNO_SYMBOLS, key=len, reverse=True)

_REDIS_ACTIVE_SPREADS  = "engine:active_spreads"
_REDIS_ACTIVE_CONDORS  = "engine:active_condors"
_REDIS_SINGLE_LEG_JRNL = "engine:single_leg_journals"
_REDIS_EXITED_TODAY    = "engine:exited_today"
_REDIS_ORDER_COUNT     = "engine:order_count"


class LiveTradingEngine:
    """
    Central trading engine.

    Orchestrates: strategies → IV/VIX check → risk → orders → broker → portfolio
    Driven by APScheduler (core/scheduler.py). Supports paper and live mode.
    """

    def __init__(
        self,
        broker: AbstractBroker,
        risk_manager: RiskManager,
        order_manager: OrderManager,
        portfolio_manager: PortfolioManager,
        notifier: Any = None,
        strategy_monitor: Optional[StrategyMonitor] = None,
        portfolio_analyzer: Optional[PortfolioAnalyzer] = None,
        regime_detector: Optional[MarketRegimeDetector] = None,
        rs_ranker: Optional[RSRanker] = None,
    ):
        self.broker             = broker
        self.risk_manager       = risk_manager
        self.order_manager      = order_manager
        self.portfolio_manager  = portfolio_manager
        self.notifier           = notifier
        self.strategy_monitor   = strategy_monitor
        self.portfolio_analyzer = portfolio_analyzer
        self.regime_detector    = regime_detector
        self.rs_ranker          = rs_ranker
        self.mode              = TradingMode(settings.TRADING_MODE)
        self.is_running        = False
        self._symbols: List[str] = []
        self._today_order_count: int = 0
        self._max_daily_orders: int  = getattr(settings, "MAX_DAILY_ORDERS", 30)
        self._peak_premiums:   Dict[str, float] = {}
        self._active_spreads:       Dict[str, Dict[str, Any]] = {}
        self._active_condors:       Dict[str, Dict[str, Any]] = {}
        self._exited_today:         set = set()   # symbols that had a breach/exit today — no re-entry same day
        # Maps option contract → {journal_id, underlying, strategy_name}
        # so _check_open_option_exits can write the exit to trade_journal.
        self._single_leg_journals:  Dict[str, Dict[str, Any]] = {}
        self._kite = None        # attached in live mode for real quotes + VIX
        self._ltp_poller = None  # ZerodhaLTPPoller — registers active option contracts

        logger.info(f"LiveTradingEngine initialised — mode: {self.mode.value.upper()}")

    # ── Setup ─────────────────────────────────────────────────────────────────

    def set_symbols(self, symbols: List[str]) -> None:
        self._symbols = symbols

    def attach_redis(self, redis_client: Any) -> None:
        self._redis = redis_client

    def attach_kite(self, kite: Any) -> None:
        """Attach a live KiteConnect instance for real option quotes + VIX."""
        self._kite = kite

    def attach_ltp_poller(self, poller: Any) -> None:
        """Attach ZerodhaLTPPoller so the engine can register/unregister active
        option contracts for 5-second real-time tracking."""
        self._ltp_poller = poller

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self.is_running = True
        await self._restore_state()
        logger.info(f"Trading engine STARTED — {self.mode.value.upper()} mode")

    async def stop(self) -> None:
        self.is_running = False
        await self._persist_state()
        logger.info("Trading engine STOPPED")

    # ── Scheduler callbacks ───────────────────────────────────────────────────

    async def on_market_open(self) -> None:
        logger.info("Market OPEN — 09:15 IST")
        self._today_order_count = 0
        self.risk_manager.reset_daily_state()

        if self.mode == TradingMode.LIVE:
            redis = getattr(self, "_redis", None)
            if redis:
                token = await redis.get("zerodha:access_token")
                if not token:
                    logger.critical("LIVE MODE: Zerodha access token missing at market open!")
                    self.risk_manager.activate_kill_switch("Zerodha token missing at market open")
                    await self._notify(
                        "CRITICAL: Zerodha token not found in Redis at market open.\n"
                        "Kill switch activated. Re-run auth script then deactivate kill switch."
                    )

        await self._notify(
            f"Market OPEN — {self.mode.value.upper()} | "
            f"Capital: Rs{settings.INITIAL_CAPITAL:,.0f}"
        )

    async def on_market_close(self) -> None:
        logger.info("Market CLOSE — 15:30 IST")

    # Minimum minutes after 09:15 before new entries are allowed.
    # Prevents flooding all positions in the first cycle when LTPPoller
    # still holds stale end-of-day data from the previous session.
    _ENTRY_WARMUP_MINUTES: int = 15

    async def run_signal_cycle(self) -> None:
        """Called every minute by the scheduler."""
        if not self.is_running or not is_market_open():
            return

        if is_square_off_time():
            await self._square_off_all()
            return

        vix = await self._get_cached_vix()
        active_strategies = StrategyRegistry.get_active_strategies()
        if not active_strategies:
            return

        positions = await self._safe_get_positions()
        await self._refresh_risk_state(positions)

        # Update market prices for ALL open positions (long AND short legs)
        # so unrealized PnL reflects reality, not just entry prices.
        await self._refresh_all_position_market_prices(positions)

        # Cancel orders that have been pending > 5 minutes
        await self.order_manager.expire_stale_orders()

        # Exit checks
        await self._check_spread_exits(active_strategies)
        await self._check_condor_exits(active_strategies)
        await self._check_open_option_exits(positions, active_strategies)

        # Refresh risk state after exits so sector/position checks see current positions
        positions = await self._safe_get_positions()
        await self._refresh_risk_state(positions)

        # Auto-kill check: pause strategies that show statistical deterioration
        if self.strategy_monitor:
            await self.strategy_monitor.evaluate_all()

        # Regime detection + strategy switching (runs every cycle, lightweight)
        if self.regime_detector:
            await self.regime_detector.detect()
            await self.regime_detector.enforce_regime_switching()

        # Log correlation / sector concentration warnings (non-blocking)
        if self.portfolio_analyzer and positions:
            report = self.portfolio_analyzer.get_report(positions)
            for flag in report.get("correlation_flags", []):
                logger.warning(f"PortfolioAnalyzer: {flag}")
            for alert in report.get("concentration_alerts", []):
                logger.warning(f"PortfolioAnalyzer: {alert}")

        # Entry signals — only after the warm-up window has elapsed.
        # Prevents entering all positions in the first cycle on stale data.
        now = now_ist()
        market_open_today = now.replace(hour=9, minute=15, second=0, microsecond=0)
        minutes_since_open = (now - market_open_today).total_seconds() / 60
        if minutes_since_open < self._ENTRY_WARMUP_MINUTES:
            logger.info(
                f"Market open warm-up: {self._ENTRY_WARMUP_MINUTES - int(minutes_since_open)} min "
                f"remaining before entries are allowed."
            )
            return

        for strategy_id, strategy in active_strategies.items():
            symbols = await self._get_active_symbols(strategy)
            for symbol in symbols:
                try:
                    await self._process_signal(strategy, symbol, vix=vix)
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
        if self._today_order_count == 0:
            logger.info("EOD: no trades today, skipping report.")
        else:
            # Read today's realized PnL from trade_journal (the authoritative source).
            # positions.realized_pnl is not reliably populated in paper mode.
            try:
                from datetime import date as _date
                from src.database.connection import AsyncSessionLocal
                from src.database.models.trade_journal import TradeJournal
                from src.database.repositories.base import BaseRepository
                from sqlalchemy import select as _select
                today_str = _date.today().isoformat()
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        _select(TradeJournal).where(
                            TradeJournal.exit_time.isnot(None)
                        )
                    )
                    closed_today = [
                        t for t in result.scalars().all()
                        if t.exit_time and t.exit_time.date().isoformat() == today_str
                    ]
                today_realized = sum(float(t.pnl or 0) for t in closed_today)
                closed_count   = len(closed_today)
            except Exception as e:
                logger.error(f"EOD: failed to read trade_journal: {e}")
                today_realized = 0.0
                closed_count   = 0

            positions = await self._safe_get_positions()
            open_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)

            await self._notify(
                f"EOD REPORT\n"
                f"Date: {now_ist().strftime('%d-%b-%Y')}\n"
                f"Mode: {self.mode.value.upper()}\n"
                f"Orders placed today: {self._today_order_count}\n"
                f"Closed trades today: {closed_count}\n"
                f"Realized PnL today:  Rs{today_realized:,.2f}\n"
                f"Open positions:      {len(positions)}\n"
                f"Unrealized PnL:      Rs{open_unrealized:,.2f}\n"
                f"Net PnL today:       Rs{today_realized + open_unrealized:,.2f}"
            )

        await self._persist_state()
        self._today_order_count = 0
        self._peak_premiums.clear()
        self._active_spreads.clear()
        self._active_condors.clear()
        self._exited_today.clear()

    # ── State persistence ─────────────────────────────────────────────────────

    async def _persist_state(self) -> None:
        redis = getattr(self, "_redis", None)
        if not redis:
            return
        try:
            today = now_ist().date().isoformat()
            await redis.set(_REDIS_ACTIVE_SPREADS,  json.dumps(self._active_spreads))
            await redis.set(_REDIS_ACTIVE_CONDORS,  json.dumps(self._active_condors))
            await redis.set(_REDIS_SINGLE_LEG_JRNL, json.dumps(self._single_leg_journals))
            await redis.set(_REDIS_EXITED_TODAY, json.dumps({"date": today, "symbols": list(self._exited_today)}))
            await redis.set(_REDIS_ORDER_COUNT,  json.dumps({"date": today, "count": self._today_order_count}))
        except Exception as e:
            logger.error(f"Failed to persist engine state: {e}")

    async def _restore_state(self) -> None:
        redis = getattr(self, "_redis", None)
        if not redis:
            return
        try:
            today = now_ist().date().isoformat()

            spreads_raw = await redis.get(_REDIS_ACTIVE_SPREADS)
            if spreads_raw:
                self._active_spreads = json.loads(spreads_raw)
                logger.info(f"Restored {len(self._active_spreads)} active spread(s)")
            condors_raw = await redis.get(_REDIS_ACTIVE_CONDORS)
            if condors_raw:
                self._active_condors = json.loads(condors_raw)
                logger.info(f"Restored {len(self._active_condors)} active condor(s)")
            jrnl_raw = await redis.get(_REDIS_SINGLE_LEG_JRNL)
            if jrnl_raw:
                self._single_leg_journals = json.loads(jrnl_raw)
                logger.info(f"Restored {len(self._single_leg_journals)} single-leg journal pointer(s)")

            # Restore today-only state — discard if it's from a previous day
            exited_raw = await redis.get(_REDIS_EXITED_TODAY)
            if exited_raw:
                exited_data = json.loads(exited_raw)
                if exited_data.get("date") == today:
                    self._exited_today = set(exited_data.get("symbols", []))
                    logger.info(f"Restored _exited_today: {self._exited_today}")
            count_raw = await redis.get(_REDIS_ORDER_COUNT)
            if count_raw:
                count_data = json.loads(count_raw)
                if count_data.get("date") == today:
                    self._today_order_count = count_data.get("count", 0)
                    logger.info(f"Restored today's order count: {self._today_order_count}")
        except Exception as e:
            logger.error(f"Failed to restore engine state: {e}")

        # After Redis restore, cross-check broker positions for orphans
        await self._reconcile_broker_positions()

    async def _reconcile_broker_positions(self) -> None:
        """
        Compare broker's actual positions against engine's in-memory state.
        Logs CRITICAL warnings for any option contracts held by the broker that
        the engine is not tracking — these will NOT be auto-exited.
        Called once at startup after _restore_state().
        """
        try:
            broker_positions = await self.broker.get_positions()
        except Exception as e:
            logger.warning(f"Reconcile: could not fetch broker positions: {e}")
            return

        if not broker_positions:
            return

        # Collect every contract the engine is currently tracking
        tracked: set = set()
        for s in self._active_spreads.values():
            tracked.update([s.get("short_contract"), s.get("long_contract")])
        for c in self._active_condors.values():
            tracked.update([
                c.get("put_short_contract"), c.get("put_long_contract"),
                c.get("call_short_contract"), c.get("call_long_contract"),
            ])
        for contract in self._single_leg_journals:
            tracked.add(contract)
        tracked.discard(None)

        orphans = [
            p for p in broker_positions
            if p.get("symbol") not in tracked and p.get("quantity", 0) != 0
        ]
        if not orphans:
            logger.info("Reconcile: all broker positions are accounted for in engine state.")
            return

        logger.critical(
            f"RECONCILE WARNING: {len(orphans)} broker position(s) are NOT tracked "
            "by the engine — likely from a crash or flushed Redis. "
            "These positions will NOT trigger auto-exit. Close them manually."
        )
        for p in orphans:
            logger.critical(
                f"  ORPHANED: {p.get('symbol')} qty={p.get('quantity')} "
                f"avg_price={p.get('avg_price', '?')}"
            )

    # ── Market price refresh ──────────────────────────────────────────────────

    async def _refresh_all_position_market_prices(
        self, positions: List[Dict[str, Any]]
    ) -> None:
        """
        Update market_price and unrealized_pnl for EVERY open position —
        including short legs of spreads/condors which were previously never updated.

        In live mode: fetches actual LTP from kite.ltp() for all option contracts.
        In paper mode: estimates premium from ATR, inferring OTM distance from the
                       entry price relative to the current ATM estimate.
        """
        if not positions:
            return

        expiry = get_near_month_expiry()
        dte    = max((expiry - now_ist().replace(tzinfo=None)).days, 1)

        # ── Live mode: use actual option LTP from Zerodha ─────────────────────
        if self._kite:
            try:
                import asyncio as _asyncio
                keys = [f"NFO:{p['symbol']}" for p in positions if p.get("symbol")]
                if keys:
                    quotes = await _asyncio.get_event_loop().run_in_executor(
                        None, self._kite.ltp, keys
                    )
                    for pos in positions:
                        ltp = quotes.get(f"NFO:{pos['symbol']}", {}).get("last_price", 0)
                        if ltp > 0:
                            await self.portfolio_manager.update_position_market_price(
                                pos["symbol"], ltp
                            )
                return
            except Exception as e:
                logger.debug(f"kite.ltp for open positions failed, falling back to estimate: {e}")

        # ── Paper mode: ATR-based estimate, OTM distance inferred from entry price ──
        # Build OTM-intervals map from active spread/condor metadata (most accurate)
        contract_otm: Dict[str, int] = {}
        for s in self._active_spreads.values():
            contract_otm[s.get("short_contract", "")] = 0   # short is near-ATM
            contract_otm[s.get("long_contract", "")]  = 2   # long is 2 OTM
        for c in self._active_condors.values():
            contract_otm[c.get("put_short_contract",  "")] = 1
            contract_otm[c.get("put_long_contract",   "")] = 2
            contract_otm[c.get("call_short_contract", "")] = 1
            contract_otm[c.get("call_long_contract",  "")] = 2

        for pos in positions:
            contract = pos.get("symbol", "")
            qty      = pos.get("quantity", 0)
            entry_p  = float(pos.get("avg_price") or 0)
            if qty == 0 or entry_p <= 0:
                continue

            underlying = self._get_underlying_from_contract(contract)
            if not underlying:
                continue

            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            atr = float(market_data.get("atr14", 0))
            if atr <= 0:
                continue

            # Infer OTM distance: compare entry_price vs ATM estimate at entry
            if contract in contract_otm:
                otm = contract_otm[contract]
            else:
                atm_now = estimate_option_premium(atr, dte, otm_intervals=0)
                if entry_p < atm_now * 0.55:
                    otm = 2
                elif entry_p < atm_now * 0.80:
                    otm = 1
                else:
                    otm = 0

            current_p = estimate_option_premium(atr, dte, otm_intervals=otm)
            await self.portfolio_manager.update_position_market_price(contract, current_p)

    # ── Exit management ───────────────────────────────────────────────────────

    async def _check_open_option_exits(
        self,
        positions: List[Dict[str, Any]],
        active_strategies: Dict[str, Any],
    ) -> None:
        """Evaluate exit rules for every open single-leg LONG option position."""
        expiry = get_near_month_expiry()
        dte    = (expiry - now_ist().replace(tzinfo=None)).days

        managed: set = set()
        for s in self._active_spreads.values():
            managed.update([s.get("short_contract", ""), s.get("long_contract", "")])
        for c in self._active_condors.values():
            managed.update([
                c.get("put_short_contract", ""), c.get("put_long_contract", ""),
                c.get("call_short_contract", ""), c.get("call_long_contract", ""),
            ])

        for pos in positions:
            contract = pos.get("symbol", "")
            qty      = pos.get("quantity", 0)
            entry_p  = float(pos.get("avg_price") or 0)
            if qty <= 0 or entry_p <= 0 or contract in managed:
                continue

            underlying = self._get_underlying_from_contract(contract)
            if not underlying:
                continue

            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            atr       = float(market_data.get("atr14", 0))
            current_p = estimate_option_premium(atr, dte) if atr > 0 else entry_p
            await self.portfolio_manager.update_position_market_price(contract, current_p)

            peak = self._peak_premiums.get(contract, entry_p)
            if current_p > peak:
                self._peak_premiums[contract] = current_p
                peak = current_p

            exit_reason: Optional[str] = None
            if dte < 4:
                exit_reason = f"DTE={dte} — entering illiquid expiry window"

            if exit_reason is None:
                for strategy in active_strategies.values():
                    result = strategy.manage_position(
                        {"avg_price": entry_p, "peak_premium": peak}, current_p
                    )
                    if result == "EXIT":
                        pnl_pct = (current_p - entry_p) / entry_p * 100
                        exit_reason = (
                            f"{strategy.name} entry=Rs{entry_p:.2f} "
                            f"now=Rs{current_p:.2f} ({pnl_pct:+.1f}%)"
                        )
                        break

            if exit_reason:
                logger.info(f"EXIT [{contract}]: {exit_reason}")
                db_order = await self.order_manager.place_order(
                    contract, "SELL", abs(qty), current_p, is_exit_order=True
                )
                if db_order and db_order.order_status not in ("REJECTED_BY_RISK", "FAILED"):
                    self._peak_premiums.pop(contract, None)
                    pnl = (current_p - entry_p) * abs(qty)

                    # Write exit to trade_journal so PnL appears in analytics + dashboard
                    info = self._single_leg_journals.pop(contract, None)
                    if info:
                        md = await self._get_market_data(info["underlying"])
                        await self._log_trade_close(
                            journal_id=info["journal_id"],
                            exit_price=current_p,
                            pnl=pnl,
                            exit_reason=exit_reason,
                            market_data=md,
                        )
                        await self._persist_state()

                    await self._notify(
                        f"POSITION CLOSED\nContract: {contract}\n"
                        f"Reason: {exit_reason}\n"
                        f"Entry: Rs{entry_p:.2f} -> Exit: Rs{current_p:.2f}\n"
                        f"Est. PnL: Rs{pnl:,.2f}"
                    )
                else:
                    logger.error(f"EXIT FAILED [{contract}]: order rejected or failed — will retry next cycle")

    async def _process_signal(
        self, strategy, symbol: str, vix: Optional[float] = None
    ) -> None:
        if not strategy.is_active:
            return
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

        if signal_str in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
            await self._process_credit_spread(strategy, symbol, signal_str, market_data, vix=vix)
            return
        if signal_str == "IRON_CONDOR":
            await self._process_iron_condor(strategy, symbol, market_data, vix=vix)
            return
        if signal_str == "EXIT":
            await self._exit_all_options_for(symbol)
            return
        if signal_str not in ("BUY", "SELL"):
            return

        if self._max_daily_orders > 0 and self._today_order_count >= self._max_daily_orders:
            logger.warning(f"Daily order limit ({self._max_daily_orders}) reached.")
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        option_type = "CE" if signal_str == "BUY" else "PE"
        opposite    = "PE" if option_type == "CE" else "CE"

        await self._close_option_positions(symbol, opposite, market_data)
        if await self._has_open_option(symbol, option_type):
            return

        expiry   = get_near_month_expiry()
        dte      = (expiry - now_ist().replace(tzinfo=None)).days

        # DTE range filter — keeps us in the liquid, balanced-theta window
        min_dte = getattr(strategy, "min_dte", 0)
        max_dte = getattr(strategy, "max_dte", 999)
        if not (min_dte <= dte <= max_dte):
            logger.info(
                f"[{strategy.name}] DTE={dte} outside [{min_dte},{max_dte}] "
                f"— skipping entry for {symbol}"
            )
            return

        lot_size = await self._get_lot_size(symbol)
        atr      = float(market_data.get("atr14", underlying_price * 0.01))
        iv_rank  = await self._get_iv_rank(symbol, underlying_price, atr, dte)
        strike   = get_atm_strike(underlying_price, symbol)
        option_p = estimate_option_premium(atr, dte)
        contract = build_option_symbol(symbol, strike, option_type, expiry)

        order = await self.order_manager.place_order(
            contract, "BUY", lot_size, option_p,
            strategy_name=strategy.name,
            iv_rank=iv_rank, vix=vix,
        )
        if order and order.order_status == "OPEN":
            self._today_order_count += 1
            self._peak_premiums[contract] = option_p
            journal_id = await self._log_trade_open(
                strategy=strategy.name, underlying=symbol,
                structure_type="SINGLE_LEG", contracts=[contract],
                entry_price=option_p, quantity=lot_size,
                market_data=market_data, iv_rank=iv_rank, vix=vix,
            )
            if journal_id:
                self._single_leg_journals[contract] = {
                    "journal_id":    journal_id,
                    "underlying":    symbol,
                    "strategy_name": strategy.name,
                }
                await self._persist_state()
            await self._notify(
                f"ORDER PLACED\nStrategy: {strategy.name}\n"
                f"BUY {lot_size} {contract} @ Rs{option_p:.2f}\n"
                f"Underlying: {symbol} @ Rs{underlying_price:.2f} | DTE: {dte}\n"
                f"IV Rank: {f'{iv_rank:.2f}' if iv_rank is not None else 'N/A'} | "
                f"VIX: {f'{vix:.1f}' if vix else 'N/A'}"
            )

    async def _close_option_positions(
        self, underlying: str, option_type: str, market_data: Dict
    ) -> None:
        positions = await self._safe_get_positions()
        expiry = get_near_month_expiry()
        dte    = (expiry - now_ist().replace(tzinfo=None)).days
        atr    = float(market_data.get("atr14", 0))
        for pos in positions:
            contract = pos.get("symbol", "")
            qty      = pos.get("quantity", 0)
            if qty <= 0 or not (contract.startswith(underlying) and contract.endswith(option_type)):
                continue
            entry_p = float(pos.get("avg_price") or 0)
            exit_p  = estimate_option_premium(atr, dte) if atr > 0 else entry_p
            await self.order_manager.place_order(contract, "SELL", abs(qty), exit_p, is_exit_order=True)
            self._peak_premiums.pop(contract, None)
            logger.info(f"REVERSAL EXIT: SELL {contract} @ Rs{exit_p:.2f}")

    async def _process_credit_spread(
        self,
        strategy,
        symbol: str,
        spread_type: str,
        market_data: Dict[str, Any],
        vix: Optional[float] = None,
    ) -> None:
        if symbol in self._active_spreads:
            return
        if symbol in self._exited_today:
            logger.debug(f"[CreditSpread] {symbol} skipped — already exited today, no re-entry.")
            return
        if self._max_daily_orders > 0 and self._today_order_count >= self._max_daily_orders:
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        from src.core.constants import FNO_STRIKE_INTERVALS
        from src.market_data.nse_oi import get_oi_data, pcr_allows_spread
        from src.market_data.option_chain import (
            atr_to_annualised_vol, find_delta_strike, get_entry_prices_for_spread,
        )
        interval = FNO_STRIKE_INTERVALS.get(symbol, 50)
        expiry   = get_near_month_expiry()
        dte      = (expiry - now_ist().replace(tzinfo=None)).days
        min_dte  = getattr(strategy, "min_dte", 7)
        if dte < min_dte:
            logger.info(
                f"[CreditSpread] {symbol} skipped — DTE={dte} < min_dte={min_dte}, too close to expiry"
            )
            return

        lot_size = await self._get_lot_size(symbol)
        atr      = float(market_data.get("atr14", underlying_price * 0.01))
        iv_rank  = await self._get_iv_rank(symbol, underlying_price, atr, dte)
        sigma    = atr_to_annualised_vol(atr, underlying_price)

        # VIX + IV Rank gates — only sell premium when it is worth selling
        from src.market_data.option_chain import vix_allows_selling, iv_rank_allows_selling
        if not vix_allows_selling(vix):
            logger.info(
                f"[CreditSpread] {symbol} skipped — VIX={vix:.1f} too low "
                f"(need ≥14.0 for rich premium). Not worth selling spreads."
            )
            return
        if not iv_rank_allows_selling(iv_rank):
            logger.info(
                f"[CreditSpread] {symbol} skipped — IV Rank={iv_rank:.2f} too low "
                f"(need ≥0.30). Premium too cheap."
            )
            return

        # OI/PCR sentiment check — confirm spread direction with market positioning
        redis = getattr(self, "_redis", None)
        oi_data = await get_oi_data(symbol, redis) if redis else None
        if oi_data and not pcr_allows_spread(oi_data.get("pcr"), spread_type):
            logger.info(
                f"[CreditSpread] {symbol} skipped — PCR={oi_data['pcr']:.2f} "
                f"opposes {spread_type}"
            )
            return

        if spread_type == "BULL_PUT_SPREAD":
            opt          = "PE"
            short_strike = find_delta_strike(underlying_price, -0.20, "PE", dte, sigma, interval)
            long_strike  = find_delta_strike(underlying_price, -0.10, "PE", dte, sigma, interval)
            if long_strike >= short_strike:
                long_strike = short_strike - 2 * interval
            if underlying_price <= short_strike:
                logger.info(f"[CreditSpread] {symbol} skipped — price Rs{underlying_price:.2f} already at/below short put Rs{short_strike}")
                return
        else:
            opt          = "CE"
            short_strike = find_delta_strike(underlying_price,  0.20, "CE", dte, sigma, interval)
            long_strike  = find_delta_strike(underlying_price,  0.10, "CE", dte, sigma, interval)
            if long_strike <= short_strike:
                long_strike = short_strike + 2 * interval
            if underlying_price >= short_strike:
                logger.info(f"[CreditSpread] {symbol} skipped — price Rs{underlying_price:.2f} already at/above short call Rs{short_strike}")
                return

        short_contract = build_option_symbol(symbol, short_strike, opt, expiry)
        long_contract  = build_option_symbol(symbol, long_strike,  opt, expiry)

        # Avoid selling at crowded OI strikes (high OI = frequently tested)
        from src.market_data.nse_oi import is_strike_crowded
        if is_strike_crowded(short_strike, oi_data, opt):
            logger.info(
                f"[CreditSpread] {symbol} short strike {short_strike} is crowded OI — "
                f"moving 1 interval further OTM"
            )
            if opt == "PE":
                short_strike -= interval
            else:
                short_strike += interval
            short_contract = build_option_symbol(symbol, short_strike, opt, expiry)

        short_p, long_p = await get_entry_prices_for_spread(
            symbol, short_contract, long_contract,
            kite=self._kite, redis=getattr(self, "_redis", None),
            atr=atr, dte=dte,
        )
        net_credit = round(short_p - long_p, 2)
        total_credit = net_credit * lot_size

        # Fee viability check — 2 entry + 2 exit orders × ₹20 brokerage = ₹80 minimum fees.
        # Require at least ₹350 net credit so fees (₹80–120 round trip) don't eat the trade.
        MIN_SPREAD_NET_CREDIT = 350.0
        if total_credit < MIN_SPREAD_NET_CREDIT:
            logger.info(
                f"[CreditSpread] {symbol} skipped — net credit ₹{total_credit:.0f} "
                f"too low (min ₹{MIN_SPREAD_NET_CREDIT:.0f} after fees). "
                f"SELL@{short_p} BUY@{long_p} x {lot_size} lots."
            )
            return

        # Risk/reward check: net credit must be ≥ 20% of wing width.
        # A ₹50 spread collecting only ₹5 has a 1:9 risk/reward — not viable.
        # Minimum 20% means: at worst a ₹50 wing collects ₹10, giving 1:4 risk/reward.
        spread_width_pts = abs(short_strike - long_strike)
        MIN_CREDIT_PCT_OF_WING = 0.20
        if net_credit < spread_width_pts * MIN_CREDIT_PCT_OF_WING:
            logger.info(
                f"[CreditSpread] {symbol} skipped — net credit ₹{net_credit:.2f} < "
                f"20% of wing width ({spread_width_pts} pts × 20% = ₹{spread_width_pts * MIN_CREDIT_PCT_OF_WING:.2f}). "
                f"R/R too poor."
            )
            return

        # Margin check — in live mode verify we have enough balance before placing
        spread_width   = abs(short_strike - long_strike)
        required_margin = spread_width * lot_size   # worst-case margin = max loss
        if not await self._check_available_margin(required_margin):
            logger.warning(
                f"[CreditSpread] {symbol} skipped — insufficient margin "
                f"(need ~Rs{required_margin:,.0f})"
            )
            return

        logger.info(
            f"[CreditSpread] {spread_type} {symbol} | SELL {short_contract}@Rs{short_p} "
            f"BUY {long_contract}@Rs{long_p} credit=Rs{net_credit}x{lot_size}=Rs{total_credit:.0f} "
            f"DTE={dte} IV_rank={iv_rank} PCR={oi_data['pcr'] if oi_data else 'N/A'}"
        )

        short_order = await self.order_manager.place_order(
            short_contract, "SELL", lot_size, short_p,
            is_spread_leg=False, strategy_name=strategy.name,
            iv_rank=iv_rank, vix=vix,
        )
        if not short_order or short_order.order_status != "OPEN":
            logger.warning(f"[CreditSpread] Short leg rejected: {short_contract}")
            if short_order and short_order.order_status == "REJECTED_BY_RISK":
                self._exited_today.add(symbol)  # stop retrying every minute
            return

        long_order = await self.order_manager.place_order(
            long_contract, "BUY", lot_size, long_p,
            is_spread_leg=True, strategy_name=strategy.name,
        )
        if not long_order or long_order.order_status != "OPEN":
            logger.error(f"[CreditSpread] Long leg failed: {long_contract}. Unwinding short.")
            await self.order_manager.place_order(
                short_contract, "BUY", lot_size, short_p, is_spread_leg=True
            )
            return

        self._today_order_count += 2
        spread_width = abs(short_strike - long_strike)

        journal_id = await self._log_trade_open(
            strategy=strategy.name, underlying=symbol,
            structure_type=spread_type,
            contracts=[short_contract, long_contract],
            entry_price=net_credit, quantity=lot_size,
            market_data=market_data, iv_rank=iv_rank, vix=vix,
        )
        self._active_spreads[symbol] = {
            "spread_type":    spread_type,
            "short_contract": short_contract, "long_contract":  long_contract,
            "short_strike":   short_strike,   "long_strike":    long_strike,
            "option_type":    opt,
            "short_premium":  short_p,        "long_premium":   long_p,
            "net_credit":     net_credit,     "lot_size":       lot_size,
            "journal_id":     journal_id,
            "strategy_name":  strategy.name,
        }
        if self._ltp_poller:
            self._ltp_poller.register_option_contracts([short_contract, long_contract])
        await self._persist_state()

        await self._notify(
            f"CREDIT SPREAD OPENED\n"
            f"Strategy: {strategy.name} | {spread_type}\n"
            f"Underlying: {symbol} @ Rs{underlying_price:.2f} | DTE: {dte}\n"
            f"SELL {short_contract} @ Rs{short_p:.2f} (delta~0.20)\n"
            f"BUY  {long_contract}  @ Rs{long_p:.2f}  (delta~0.10)\n"
            f"Net credit: Rs{net_credit:.2f} x {lot_size} = Rs{net_credit*lot_size:,.2f}\n"
            f"Max profit: Rs{net_credit*lot_size:,.2f} | "
            f"Max loss: Rs{(spread_width - net_credit)*lot_size:,.2f}\n"
            f"IV Rank: {f'{iv_rank:.2f}' if iv_rank is not None else 'N/A'} | "
            f"VIX: {f'{vix:.1f}' if vix else 'N/A'}"
        )

    async def _check_spread_exits(self, active_strategies: Dict[str, Any]) -> None:
        if not self._active_spreads:
            return

        expiry   = get_near_month_expiry()
        dte      = (expiry - now_ist().replace(tzinfo=None)).days
        to_close: List[str] = []

        cs_strategy = next(
            (s for s in active_strategies.values()
             if s.__class__.__name__ == "CreditSpreadStrategy"), None
        )

        for underlying, spread in self._active_spreads.items():
            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            current_price = float(market_data.get("close", 0))
            atr = float(market_data.get("atr14", 0))
            opt = spread["option_type"]

            # Try real Kite LTP first (same source as entry pricing via get_entry_prices_for_spread).
            # BS fallback uses 5-min ATR which underestimates annualised vol by ~8×, causing
            # exits to show near-zero option prices and triggering fake "max profit" exits.
            from src.market_data.option_chain import get_option_quote
            kite  = getattr(self, "_kite",  None)
            redis = getattr(self, "_redis", None)
            short_ltp = await get_option_quote(spread["short_contract"], kite, redis)
            long_ltp  = await get_option_quote(spread["long_contract"],  kite, redis)

            if short_ltp and short_ltp > 0:
                cur_short = short_ltp
            elif current_price > 0:
                cur_short = estimate_option_premium(atr, dte, underlying_price=current_price, strike=spread["short_strike"], option_type=opt)
            else:
                cur_short = spread["short_premium"]

            if long_ltp and long_ltp > 0:
                cur_long = long_ltp
            elif current_price > 0:
                cur_long = estimate_option_premium(atr, dte, underlying_price=current_price, strike=spread["long_strike"], option_type=opt)
            else:
                cur_long = spread["long_premium"]

            min_dte     = getattr(cs_strategy, "min_dte", 7) if cs_strategy else 7
            exit_reason: Optional[str] = None

            if dte < min_dte:
                exit_reason = f"DTE={dte} < {min_dte}"
            if exit_reason is None and current_price > 0:
                ss = spread["short_strike"]
                if spread["spread_type"] == "BULL_PUT_SPREAD" and current_price < ss:
                    exit_reason = f"Put breach: {underlying} Rs{current_price:.2f} < short Rs{ss}"
                elif spread["spread_type"] == "BEAR_CALL_SPREAD" and current_price > ss:
                    exit_reason = f"Call breach: {underlying} Rs{current_price:.2f} > short Rs{ss}"
            if exit_reason is None and cs_strategy:
                result = cs_strategy.manage_position(
                    {"short_premium": spread["short_premium"]}, cur_short
                )
                if result == "EXIT":
                    pnl_pct = (spread["short_premium"] - cur_short) / spread["short_premium"] * 100
                    exit_reason = (
                        f"{cs_strategy.name} short Rs{spread['short_premium']:.2f} "
                        f"-> Rs{cur_short:.2f} ({pnl_pct:+.1f}%)"
                    )

            if exit_reason is None:
                continue

            lot = spread["lot_size"]
            await self.order_manager.place_order(spread["short_contract"], "BUY",  lot, cur_short, is_spread_leg=True)
            await self.order_manager.place_order(spread["long_contract"],  "SELL", lot, cur_long,  is_spread_leg=True)

            net_pnl = (
                (spread["short_premium"] - cur_short)
                - (spread["long_premium"]  - cur_long)
            ) * lot

            self.risk_manager.release_deployed_capital(
                spread.get("strategy_name", "credit_spread_v1"),
                spread["long_premium"] * lot,
            )

            await self._log_trade_close(
                journal_id=spread.get("journal_id"),
                exit_price=round(cur_short - cur_long, 2),
                pnl=net_pnl, exit_reason=exit_reason,
                market_data=market_data,
            )
            if self._ltp_poller:
                self._ltp_poller.unregister_option_contracts(
                    [spread["short_contract"], spread["long_contract"]]
                )
            to_close.append(underlying)
            await self._notify(
                f"CREDIT SPREAD CLOSED\n"
                f"Underlying: {underlying}\nReason: {exit_reason}\n"
                f"Short: sold Rs{spread['short_premium']:.2f}, closed Rs{cur_short:.2f}\n"
                f"Long:  paid Rs{spread['long_premium']:.2f}, sold Rs{cur_long:.2f}\n"
                f"Net PnL: Rs{net_pnl:,.2f}"
            )

        for sym in to_close:
            del self._active_spreads[sym]
            self._exited_today.add(sym)
        if to_close:
            await self._persist_state()

    async def _process_iron_condor(
        self,
        strategy,
        symbol: str,
        market_data: Dict[str, Any],
        vix: Optional[float] = None,
    ) -> None:
        if symbol in self._active_condors:
            return
        if symbol in self._exited_today:
            logger.debug(f"[IronCondor] {symbol} skipped — already exited today, no re-entry.")
            return
        if self._max_daily_orders > 0 and self._today_order_count >= self._max_daily_orders:
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        from src.core.constants import FNO_STRIKE_INTERVALS
        from src.market_data.option_chain import atr_to_annualised_vol, find_delta_strike
        interval = FNO_STRIKE_INTERVALS.get(symbol, 50)
        expiry   = get_near_month_expiry()
        dte      = (expiry - now_ist().replace(tzinfo=None)).days
        min_dte  = getattr(strategy, "min_dte", 7)
        if dte < min_dte:
            logger.info(
                f"[IronCondor] {symbol} skipped — DTE={dte} < min_dte={min_dte}, too close to expiry"
            )
            return

        lot_size = await self._get_lot_size(symbol)
        atr      = float(market_data.get("atr14", underlying_price * 0.01))
        iv_rank  = await self._get_iv_rank(symbol, underlying_price, atr, dte)
        sigma    = atr_to_annualised_vol(atr, underlying_price)

        # VIX + IV Rank gates — only sell premium when it is worth selling
        from src.market_data.option_chain import vix_allows_selling, iv_rank_allows_selling
        if not vix_allows_selling(vix):
            logger.info(
                f"[IronCondor] {symbol} skipped — VIX={vix:.1f} too low "
                f"(need ≥14.0 for rich premium). Not worth selling condors."
            )
            return
        if not iv_rank_allows_selling(iv_rank):
            logger.info(
                f"[IronCondor] {symbol} skipped — IV Rank={iv_rank:.2f} too low "
                f"(need ≥0.30). Premium too cheap."
            )
            return

        # Delta-based: short ~0.20, hedge ~0.10 on each wing
        put_short_strike  = find_delta_strike(underlying_price, -0.20, "PE", dte, sigma, interval)
        put_long_strike   = find_delta_strike(underlying_price, -0.10, "PE", dte, sigma, interval)
        call_short_strike = find_delta_strike(underlying_price,  0.20, "CE", dte, sigma, interval)
        call_long_strike  = find_delta_strike(underlying_price,  0.10, "CE", dte, sigma, interval)

        if put_long_strike  >= put_short_strike:
            put_long_strike  = put_short_strike  - 2 * interval
        if call_long_strike <= call_short_strike:
            call_long_strike = call_short_strike + 2 * interval

        if underlying_price <= put_short_strike or underlying_price >= call_short_strike:
            logger.info(
                f"[IronCondor] {symbol} skipped — price Rs{underlying_price:.2f} is outside "
                f"short strikes [{put_short_strike}–{call_short_strike}], would breach immediately."
            )
            return

        psc = build_option_symbol(symbol, put_short_strike,  "PE", expiry)
        plc = build_option_symbol(symbol, put_long_strike,   "PE", expiry)
        csc = build_option_symbol(symbol, call_short_strike, "CE", expiry)
        clc = build_option_symbol(symbol, call_long_strike,  "CE", expiry)

        put_short_p  = estimate_option_premium(atr, dte, otm_intervals=1)
        put_long_p   = estimate_option_premium(atr, dte, otm_intervals=3)
        call_short_p = estimate_option_premium(atr, dte, otm_intervals=1)
        call_long_p  = estimate_option_premium(atr, dte, otm_intervals=3)
        net_credit   = round((put_short_p - put_long_p) + (call_short_p - call_long_p), 2)
        total_credit = net_credit * lot_size

        # Fee viability check — 4 entry + 4 exit orders × ₹20 brokerage = ₹160 minimum fees.
        # Require at least ₹600 net credit so fees (₹160–250 round trip) don't eat the trade.
        MIN_CONDOR_NET_CREDIT = 600.0
        if total_credit < MIN_CONDOR_NET_CREDIT:
            logger.info(
                f"[IronCondor] {symbol} skipped — net credit ₹{total_credit:.0f} "
                f"too low (min ₹{MIN_CONDOR_NET_CREDIT:.0f} after fees). "
                f"PS@{put_short_p} PL@{put_long_p} CS@{call_short_p} CL@{call_long_p} x {lot_size} lots."
            )
            return

        # Risk/reward check: each wing's net credit must be ≥ 20% of that wing's width.
        # A 50-point wing collecting only 4 points per share gives 1:11.5 risk/reward — not viable.
        put_wing_width  = abs(put_short_strike  - put_long_strike)
        call_wing_width = abs(call_short_strike - call_long_strike)
        put_wing_credit  = put_short_p  - put_long_p
        call_wing_credit = call_short_p - call_long_p
        MIN_WING_CREDIT_PCT = 0.20
        if put_wing_credit < put_wing_width * MIN_WING_CREDIT_PCT:
            logger.info(
                f"[IronCondor] {symbol} skipped — put wing credit ₹{put_wing_credit:.2f} < "
                f"20% of wing ({put_wing_width} pts × 20% = ₹{put_wing_width * MIN_WING_CREDIT_PCT:.2f})."
            )
            return
        if call_wing_credit < call_wing_width * MIN_WING_CREDIT_PCT:
            logger.info(
                f"[IronCondor] {symbol} skipped — call wing credit ₹{call_wing_credit:.2f} < "
                f"20% of wing ({call_wing_width} pts × 20% = ₹{call_wing_width * MIN_WING_CREDIT_PCT:.2f})."
            )
            return

        # Margin check — condor requires margin for the wider of the two wings
        wing_spread     = abs(put_short_strike - put_long_strike)
        required_margin = wing_spread * lot_size
        if not await self._check_available_margin(required_margin):
            logger.warning(
                f"[IronCondor] {symbol} skipped — insufficient margin "
                f"(need ~Rs{required_margin:,.0f})"
            )
            return

        legs = [
            (psc, "SELL", put_short_p,  False),
            (plc, "BUY",  put_long_p,   True),
            (csc, "SELL", call_short_p, True),
            (clc, "BUY",  call_long_p,  True),
        ]
        placed = []
        for contract, side, price, is_leg in legs:
            kwargs: Dict[str, Any] = dict(is_spread_leg=is_leg, strategy_name=strategy.name)
            if not is_leg:
                kwargs["iv_rank"] = iv_rank
                kwargs["vix"]     = vix
            order = await self.order_manager.place_order(contract, side, lot_size, price, **kwargs)
            if not order or order.order_status != "OPEN":
                logger.error(f"[IronCondor] Leg failed: {side} {contract}. Unwinding {len(placed)} leg(s).")
                if order and order.order_status == "REJECTED_BY_RISK":
                    self._exited_today.add(symbol)  # stop retrying every minute
                for (c, s, p, _) in placed:
                    rev = "BUY" if s == "SELL" else "SELL"
                    await self.order_manager.place_order(c, rev, lot_size, p, is_spread_leg=True)
                return
            placed.append((contract, side, price, is_leg))

        self._today_order_count += 4
        journal_id  = await self._log_trade_open(
            strategy=strategy.name, underlying=symbol,
            structure_type="IRON_CONDOR", contracts=[psc, plc, csc, clc],
            entry_price=net_credit, quantity=lot_size,
            market_data=market_data, iv_rank=iv_rank, vix=vix,
        )
        wing_spread = abs(put_short_strike - put_long_strike)
        self._active_condors[symbol] = {
            "put_short_contract":  psc,  "put_long_contract":   plc,
            "call_short_contract": csc,  "call_long_contract":  clc,
            "put_short_strike":    put_short_strike,  "put_long_strike":   put_long_strike,
            "call_short_strike":   call_short_strike, "call_long_strike":  call_long_strike,
            "put_short_premium":   put_short_p,  "put_long_premium":  put_long_p,
            "call_short_premium":  call_short_p, "call_long_premium": call_long_p,
            "net_credit":          net_credit,   "lot_size":          lot_size,
            "journal_id":          journal_id,
            "strategy_name":       strategy.name,
        }
        if self._ltp_poller:
            self._ltp_poller.register_option_contracts([psc, plc, csc, clc])
        await self._persist_state()

        await self._notify(
            f"IRON CONDOR OPENED\n"
            f"Strategy: {strategy.name}\n"
            f"Underlying: {symbol} @ Rs{underlying_price:.2f} | DTE: {dte}\n"
            f"PUT  wing: SELL {psc}@Rs{put_short_p:.2f}(d~-0.20) BUY {plc}@Rs{put_long_p:.2f}(d~-0.10)\n"
            f"CALL wing: SELL {csc}@Rs{call_short_p:.2f}(d~0.20)  BUY {clc}@Rs{call_long_p:.2f}(d~0.10)\n"
            f"Net credit: Rs{net_credit:.2f} x {lot_size} = Rs{net_credit*lot_size:,.2f}\n"
            f"IV Rank: {f'{iv_rank:.2f}' if iv_rank is not None else 'N/A'} | "
            f"VIX: {f'{vix:.1f}' if vix else 'N/A'}"
        )

    async def _check_condor_exits(self, active_strategies: Dict[str, Any]) -> None:
        if not self._active_condors:
            return

        expiry   = get_near_month_expiry()
        dte      = (expiry - now_ist().replace(tzinfo=None)).days
        to_close: List[str] = []

        ic_strategy = next(
            (s for s in active_strategies.values()
             if s.__class__.__name__ == "IronCondorStrategy"), None
        )

        for underlying, c in self._active_condors.items():
            market_data = await self._get_market_data(underlying)
            if not market_data:
                continue

            current_price = float(market_data.get("close", 0))
            atr = float(market_data.get("atr14", 0))

            # Try real Kite LTPs first — same fix as _check_spread_exits.
            from src.market_data.option_chain import get_option_quote
            kite  = getattr(self, "_kite",  None)
            redis = getattr(self, "_redis", None)

            def _leg_price(ltp, bs_fallback_kwargs, entry_p):
                if ltp and ltp > 0:
                    return ltp
                if current_price > 0:
                    return estimate_option_premium(atr, dte, **bs_fallback_kwargs)
                return entry_p

            ps_ltp = await get_option_quote(c["put_short_contract"],  kite, redis)
            pl_ltp = await get_option_quote(c["put_long_contract"],   kite, redis)
            cs_ltp = await get_option_quote(c["call_short_contract"], kite, redis)
            cl_ltp = await get_option_quote(c["call_long_contract"],  kite, redis)

            cur_ps = _leg_price(ps_ltp, {"underlying_price": current_price, "strike": c["put_short_strike"],  "option_type": "PE"}, c["put_short_premium"])
            cur_pl = _leg_price(pl_ltp, {"underlying_price": current_price, "strike": c["put_long_strike"],   "option_type": "PE"}, c["put_long_premium"])
            cur_cs = _leg_price(cs_ltp, {"underlying_price": current_price, "strike": c["call_short_strike"], "option_type": "CE"}, c["call_short_premium"])
            cur_cl = _leg_price(cl_ltp, {"underlying_price": current_price, "strike": c["call_long_strike"],  "option_type": "CE"}, c["call_long_premium"])

            min_dte    = getattr(ic_strategy, "min_dte",            7)   if ic_strategy else 7
            profit_pct = getattr(ic_strategy, "profit_close_pct",  0.25) if ic_strategy else 0.25
            sl_mult    = getattr(ic_strategy, "stop_loss_multiple", 2.0)  if ic_strategy else 2.0
            exit_reason: Optional[str] = None

            if dte < min_dte:
                exit_reason = f"DTE={dte} < {min_dte}"
            if exit_reason is None and current_price > 0:
                if current_price < c["put_short_strike"]:
                    exit_reason = f"Put breach: Rs{current_price:.2f} < Rs{c['put_short_strike']}"
                elif current_price > c["call_short_strike"]:
                    exit_reason = f"Call breach: Rs{current_price:.2f} > Rs{c['call_short_strike']}"
            if exit_reason is None:
                if cur_ps >= c["put_short_premium"] * sl_mult:
                    exit_reason = f"Put SL: Rs{c['put_short_premium']:.2f} -> Rs{cur_ps:.2f}"
                elif cur_cs >= c["call_short_premium"] * sl_mult:
                    exit_reason = f"Call SL: Rs{c['call_short_premium']:.2f} -> Rs{cur_cs:.2f}"
            if exit_reason is None:
                if (cur_ps <= c["put_short_premium"] * profit_pct
                        and cur_cs <= c["call_short_premium"] * profit_pct):
                    exit_reason = "Both wings at 75%+ profit — closing condor"

            if exit_reason is None:
                continue

            lot = c["lot_size"]
            await self.order_manager.place_order(c["put_short_contract"],  "BUY",  lot, cur_ps, is_spread_leg=True)
            await self.order_manager.place_order(c["put_long_contract"],   "SELL", lot, cur_pl, is_spread_leg=True)
            await self.order_manager.place_order(c["call_short_contract"], "BUY",  lot, cur_cs, is_spread_leg=True)
            await self.order_manager.place_order(c["call_long_contract"],  "SELL", lot, cur_cl, is_spread_leg=True)

            net_pnl = (
                (c["put_short_premium"]  - cur_ps)
                + (c["call_short_premium"] - cur_cs)
                - (c["put_long_premium"]   - cur_pl)
                - (c["call_long_premium"]  - cur_cl)
            ) * lot

            self.risk_manager.release_deployed_capital(
                c.get("strategy_name", "iron_condor_v1"),
                (c["put_long_premium"] + c["call_long_premium"]) * lot,
            )

            await self._log_trade_close(
                journal_id=c.get("journal_id"),
                exit_price=round(cur_ps + cur_cs - cur_pl - cur_cl, 2),
                pnl=net_pnl, exit_reason=exit_reason,
                market_data=market_data,
            )
            if self._ltp_poller:
                self._ltp_poller.unregister_option_contracts([
                    c["put_short_contract"], c["put_long_contract"],
                    c["call_short_contract"], c["call_long_contract"],
                ])
            to_close.append(underlying)
            await self._notify(
                f"IRON CONDOR CLOSED\n"
                f"Underlying: {underlying}\nReason: {exit_reason}\n"
                f"Put short:  Rs{c['put_short_premium']:.2f} -> Rs{cur_ps:.2f}\n"
                f"Call short: Rs{c['call_short_premium']:.2f} -> Rs{cur_cs:.2f}\n"
                f"Net PnL: Rs{net_pnl:,.2f}"
            )

        for sym in to_close:
            del self._active_condors[sym]
            self._exited_today.add(sym)
        if to_close:
            await self._persist_state()

    async def _exit_all_options_for(self, underlying: str) -> None:
        positions = await self._safe_get_positions()
        expiry = get_near_month_expiry()
        dte    = (expiry - now_ist().replace(tzinfo=None)).days
        for pos in positions:
            if pos.get("quantity", 0) <= 0 or not pos.get("symbol", "").startswith(underlying):
                continue
            entry_p = float(pos.get("avg_price") or 0)
            md = await self._get_market_data(underlying)
            atr = float(md.get("atr14", 0)) if md else 0
            exit_p = estimate_option_premium(atr, dte) if atr > 0 else entry_p
            await self.order_manager.place_order(pos["symbol"], "SELL", abs(pos["quantity"]), exit_p, is_exit_order=True)
            self._peak_premiums.pop(pos["symbol"], None)

    async def _square_off_all(self) -> None:
        positions = await self._safe_get_positions()
        expiry = get_near_month_expiry()
        dte    = (expiry - now_ist().replace(tzinfo=None)).days
        closed = 0
        for pos in positions:
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            contract   = pos["symbol"]
            side       = "SELL" if qty > 0 else "BUY"
            entry_p    = float(pos.get("avg_price") or 0)
            underlying = self._get_underlying_from_contract(contract)
            exit_p     = entry_p
            if underlying:
                md = await self._get_market_data(underlying)
                if md:
                    atr = float(md.get("atr14", 0))
                    if atr > 0:
                        exit_p = estimate_option_premium(atr, dte)
            await self.order_manager.place_order(contract, side, abs(qty), exit_p, is_exit_order=True)
            self._peak_premiums.pop(contract, None)
            closed += 1

        self._active_spreads.clear()
        self._active_condors.clear()
        await self._persist_state()
        if closed:
            await self._notify(f"AUTO SQUARE-OFF\nClosed {closed} position(s) at 15:20 IST")

    # ── Trade Journal helpers ─────────────────────────────────────────────────

    async def _log_trade_open(
        self,
        strategy: str,
        underlying: str,
        structure_type: str,
        contracts: List[str],
        entry_price: float,
        quantity: int,
        market_data: Dict,
        iv_rank: Optional[float],
        vix: Optional[float],
    ) -> Optional[int]:
        try:
            from src.database.connection import AsyncSessionLocal
            from src.database.models.trade_journal import TradeJournal
            from src.database.repositories.base import BaseRepository
            repo = BaseRepository(TradeJournal, AsyncSessionLocal)
            atr_pct = float(market_data.get("atr_pct", 0)) or (
                float(market_data.get("atr14", 0))
                / max(float(market_data.get("close", 1)), 1) * 100
            )
            ema_sp   = float(market_data.get("ema_spread_pct", 0))
            entry_ts = now_ist().replace(tzinfo=None)
            row = await repo.create({
                "strategy_name":  strategy,
                "underlying":     underlying,
                "structure_type": structure_type,
                "contracts":      json.dumps(contracts),
                "entry_time":     entry_ts,
                "entry_price":    entry_price,
                "quantity":       quantity,
                "regime_atr_pct": round(atr_pct, 4),
                "ema_spread_pct": round(ema_sp, 4),
                "iv_rank":        iv_rank,
                "vix_at_entry":   vix,
                "day_of_week":    entry_ts.weekday(),   # 0=Mon … 4=Fri
                "hour_of_day":    entry_ts.hour,        # IST hour
            })
            return row.id
        except Exception as e:
            logger.error(f"TradeJournal open log failed: {e}")
            return None

    async def _log_trade_close(
        self,
        journal_id: Optional[int],
        exit_price: float,
        pnl: float,
        exit_reason: str,
        market_data: Optional[Dict] = None,
        total_slippage_pts: Optional[float] = None,
    ) -> None:
        if not journal_id:
            return
        try:
            from src.database.connection import AsyncSessionLocal
            from src.database.models.trade_journal import TradeJournal
            from src.database.repositories.base import BaseRepository
            repo = BaseRepository(TradeJournal, AsyncSessionLocal)
            row  = await repo.get_by_id(journal_id)
            if not row:
                return
            exit_t    = now_ist().replace(tzinfo=None)
            hold_days = (
                exit_t.date() - row.entry_time.replace(tzinfo=None).date()
            ).days if row.entry_time else 0

            md = market_data or {}
            atr_exit = md.get("atr14") or md.get("atr_at_exit")
            vix_exit = md.get("vix") or md.get("vix_at_exit")

            # Derive regime label from ATR%: same heuristic as LTPPoller
            regime: Optional[str] = None
            atr_pct = md.get("atr_pct")
            if atr_pct is not None:
                atr_pct = float(atr_pct)
                if atr_pct >= 2.5:
                    regime = "VOLATILE"
                elif atr_pct >= 1.2:
                    regime = "TRENDING"
                else:
                    regime = "RANGE_BOUND"

            updates = {
                "exit_time":          exit_t,
                "exit_price":         exit_price,
                "pnl":                round(pnl, 2),
                "exit_reason":        exit_reason[:200],
                "hold_days":          hold_days,
                "atr_at_exit":        float(atr_exit) if atr_exit is not None else None,
                "vix_at_exit":        float(vix_exit) if vix_exit is not None else None,
                "regime_label":       regime,
                "total_slippage_pts": round(total_slippage_pts, 4) if total_slippage_pts is not None else None,
                "slippage":           round(total_slippage_pts, 4) if total_slippage_pts is not None else None,
            }
            await repo.update(row, updates)
        except Exception as e:
            logger.error(f"TradeJournal close log failed: {e}")

    # ── IV / VIX helpers ──────────────────────────────────────────────────────

    async def _check_available_margin(self, required: float) -> bool:
        """
        Verify enough margin exists before placing a multi-leg structure.

        Paper mode: simulates margin by computing total locked margin across all
        active spreads/condors (max-loss per structure = spread_width × lot_size)
        and subtracting from initial_capital. Prevents paper account from opening
        unlimited structures beyond what real margin would allow.

        Live mode: queries kite.margins() — fails-open on API error so a transient
        Zerodha glitch never blocks a legitimate exit.
        """
        if self.mode == TradingMode.LIVE and self._kite:
            try:
                loop = asyncio.get_event_loop()
                margins   = await loop.run_in_executor(None, self._kite.margins)
                available = float(margins.get("equity", {}).get("net", 0))
                if available < required:
                    logger.warning(
                        f"Margin check: available Rs{available:,.0f} < "
                        f"required Rs{required:,.0f}"
                    )
                    return False
                return True
            except Exception as e:
                logger.error(f"Margin check API call failed: {e}. Allowing trade (fail-open).")
                return True

        # ── Paper mode: simulate margin from active structures ────────────────
        capital = float(getattr(settings, "initial_capital", 300_000))
        locked = 0.0
        for s in self._active_spreads.values():
            wing = abs(s.get("short_strike", 0) - s.get("long_strike", 0))
            locked += wing * s.get("lot_size", 1)
        for c in self._active_condors.values():
            put_wing  = abs(c.get("put_short_strike",  0) - c.get("put_long_strike",  0))
            call_wing = abs(c.get("call_short_strike", 0) - c.get("call_long_strike", 0))
            locked += max(put_wing, call_wing) * c.get("lot_size", 1)

        available = capital - locked
        if available < required:
            logger.warning(
                f"Paper margin check: capital Rs{capital:,.0f} - locked Rs{locked:,.0f} = "
                f"available Rs{available:,.0f} < required Rs{required:,.0f}"
            )
            return False
        return True

    async def _get_cached_vix(self) -> Optional[float]:
        from src.market_data.option_chain import get_india_vix, fetch_and_cache_vix
        redis = getattr(self, "_redis", None)
        if not redis:
            return None
        vix = await get_india_vix(redis)
        if vix is None and self._kite:
            vix = await fetch_and_cache_vix(self._kite, redis)
        return vix

    async def _get_iv_rank(
        self, symbol: str, underlying_price: float, atr: float, dte: int
    ) -> Optional[float]:
        from src.market_data.option_chain import (
            atr_to_annualised_vol, update_iv_history, get_iv_rank
        )
        redis = getattr(self, "_redis", None)
        if not redis:
            return None
        try:
            sigma = atr_to_annualised_vol(atr, underlying_price)
            await update_iv_history(symbol, sigma, redis)
            return await get_iv_rank(symbol, redis)
        except Exception:
            return None

    # ── Misc helpers ──────────────────────────────────────────────────────────

    def _get_underlying_from_contract(self, contract: str) -> Optional[str]:
        for sym in _FNO_SYMBOLS_BY_LEN:
            if contract.startswith(sym):
                return sym
        return None

    async def _has_open_option(self, underlying: str, option_type: str) -> bool:
        positions = await self._safe_get_positions()
        return any(
            p.get("quantity", 0) > 0
            and p.get("symbol", "").startswith(underlying)
            and p.get("symbol", "").endswith(option_type)
            for p in positions
        )

    async def _refresh_risk_state(self, positions: List[Dict]) -> None:
        realized   = sum(p.get("realized_pnl",   0) for p in positions)
        unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
        self.risk_manager.update_state(positions, realized, unrealized)

    async def _safe_get_positions(self) -> List[Dict[str, Any]]:
        try:
            return await self.broker.get_positions()
        except Exception as exc:
            logger.error(f"Failed to fetch positions: {exc}")
            return []

    # Market data is considered stale if older than this many seconds.
    # Prevents entries when the LTP poller has fallen behind (e.g., Zerodha API lag).
    _MARKET_DATA_MAX_AGE_SECONDS = 90

    async def _get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        redis = getattr(self, "_redis", None)
        if not redis:
            return None
        try:
            raw = await redis.get(f"{REDIS_TICK_PREFIX}{symbol}")
            if not raw:
                return None
            data = json.loads(raw)
            # Stale data circuit breaker: reject data older than 90 seconds
            ts_str = data.get("timestamp")
            if ts_str:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    ts = _dt.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_tz.utc)
                    age = (_dt.now(_tz.utc) - ts).total_seconds()
                    if age > self._MARKET_DATA_MAX_AGE_SECONDS:
                        logger.warning(
                            f"Stale market data for {symbol}: {age:.0f}s old "
                            f"(limit {self._MARKET_DATA_MAX_AGE_SECONDS}s) — skipping."
                        )
                        return None
                except Exception:
                    pass  # malformed timestamp — allow data through
            return data
        except Exception as exc:
            logger.error(f"Redis read [{symbol}]: {exc}")
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
