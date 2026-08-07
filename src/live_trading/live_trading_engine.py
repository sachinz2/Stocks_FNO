import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.brokers.base import AbstractBroker
from src.core.config import settings
from src.core.constants import (
    FIVE_MIN_ATR_DAILY_SCALE,
    FNO_SECTORS,
    FNO_SYMBOLS,
    MAX_SECTOR_POSITIONS,
    REDIS_LOT_SIZE_PREFIX,
    REDIS_TICK_PREFIX,
    REDIS_TOP_SYMBOLS_KEY,
    REDIS_TOP_SYMBOLS_CREDIT_SPREAD,
    REDIS_TOP_SYMBOLS_IRON_CONDOR,
    REDIS_TOP_SYMBOLS_MOMENTUM,
)
from src.core.enums import SignalType, TradingMode
from src.core.utils import (
    build_option_symbol,
    estimate_option_premium,
    get_atm_strike,
    get_entry_expiry,
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

# atr_to_annualised_vol() assumes daily ATR — see FIVE_MIN_ATR_DAILY_SCALE in
# core/constants.py for why 5-min ATR must be scaled before use here. Without this
# correction, sigma ≈ 4% when true annualised vol is ≈ 28%, causing find_delta_strike()
# to place short strikes only ~1% OTM instead of ~5-6% OTM.
_5MIN_ATR_SCALE: float = FIVE_MIN_ATR_DAILY_SCALE

_REDIS_ACTIVE_SPREADS  = "engine:active_spreads"
_REDIS_ACTIVE_CONDORS  = "engine:active_condors"
_REDIS_SINGLE_LEG_JRNL = "engine:single_leg_journals"
_REDIS_EXITED_TODAY    = "engine:exited_today"
_REDIS_PROFIT_CLOSED   = "engine:profit_closed_today"
_REDIS_ORDER_COUNT     = "engine:order_count"
_REDIS_EMA_STATE       = "engine:ema_crossover_state"
_REDIS_MOMENTUM_STATE  = "engine:momentum_state"


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
        # Max simultaneous open EMA Crossover (single-leg, intraday) positions across all
        # symbols. Blocks a 3rd entry while 1-2 are already open; independent of the
        # per-symbol duplicate check and the portfolio-wide daily order count.
        self._max_concurrent_intraday: int = getattr(settings, "MAX_CONCURRENT_INTRADAY", 2)
        self._peak_premiums:   Dict[str, float] = {}
        self._active_spreads:       Dict[str, Dict[str, Any]] = {}
        self._active_condors:       Dict[str, Dict[str, Any]] = {}
        self._exited_today:         set = set()   # adverse exits today — blocks same-day re-entry
        self._profit_closed_today:  set = set()   # profit exits today — allows re-entry with lower DTE floor
        # is_square_off_time() is true for the whole 15:20-15:30 window, and
        # run_signal_cycle() calls _square_off_all() every minute in that window —
        # this stops the EOD/expiry notification from being re-sent on every cycle.
        self._eod_notified_today:  bool = False
        # Maps option contract → {journal_id, underlying, strategy_name}
        # so _check_open_option_exits can write the exit to trade_journal.
        self._single_leg_journals:  Dict[str, Dict[str, Any]] = {}
        self._kite = None        # attached in live mode for real quotes + VIX
        self._ltp_poller = None  # ZerodhaLTPPoller — registers active option contracts
        # Prevents concurrent exit checks from 1-min signal cycle + 10-s exit-only job (F)
        self._exit_cycle_lock: asyncio.Lock = asyncio.Lock()
        # Symbols flagged during _restore_state() for immediate close (C)
        self._close_on_first_cycle: set = set()
        # Holds references to fire-and-forget background tasks so the GC cannot
        # collect them before they complete (asyncio discards unreferenced tasks).
        self._background_tasks: set = set()

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
        option contracts for 5-second real-time tracking.

        Called after engine.start() / _restore_state(), so we immediately
        re-register any contracts restored from Redis so their prices are
        refreshed by the first LTP poll cycle.
        """
        self._ltp_poller = poller
        contracts: list = []
        for spread in self._active_spreads.values():
            for key in ("short_contract", "long_contract"):
                c = spread.get(key)
                if c:
                    contracts.append(c)
        for cond in self._active_condors.values():
            for key in ("put_short_contract", "put_long_contract",
                        "call_short_contract", "call_long_contract"):
                c = cond.get(key)
                if c:
                    contracts.append(c)
        for contract in self._single_leg_journals:
            if contract:
                contracts.append(contract)
        if contracts:
            poller.register_option_contracts(contracts)
            logger.info(
                f"LTP poller attached — re-registered {len(contracts)} contract(s) "
                f"from {len(self._active_spreads)} spread(s) + "
                f"{len(self._active_condors)} condor(s) + "
                f"{len(self._single_leg_journals)} single-leg(s) restored from Redis"
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self.is_running = True
        await self._restore_state()
        await self._restore_ema_state()
        await self._restore_momentum_state()
        logger.info(f"Trading engine STARTED — {self.mode.value.upper()} mode")

    async def stop(self) -> None:
        self.is_running = False
        await self._persist_state()
        logger.info("Trading engine STOPPED")

    # ── Scheduler callbacks ───────────────────────────────────────────────────

    async def on_market_open(self) -> None:
        logger.info("Market OPEN — 09:15 IST")
        self._today_order_count = 0
        self._eod_notified_today = False
        self.risk_manager.reset_daily_state()

        # Rebuild per-strategy deployed capital from overnight multi-day positions so
        # the per-strategy budget check (risk layer 5) stays accurate next morning.
        # Uses max loss (width - net credit), matching add/release at entry/exit in
        # _process_credit_spread / _process_iron_condor / _check_spread_exits /
        # _check_condor_exits — not just the long/hedge legs' premium, which
        # understates real capital at risk (a wide spread's long leg can cost
        # almost the same regardless of width; real risk scales with width).
        for _sym, _s in self._active_spreads.items():
            _width = abs(_s.get("short_strike", 0) - _s.get("long_strike", 0))
            _ml = (_width - _s.get("net_credit", 0)) * _s.get("lot_size", 0)
            if _ml > 0:
                self.risk_manager.add_deployed_capital(
                    _s.get("strategy_name", "credit_spread_v1"), _ml
                )
        for _sym, _c in self._active_condors.items():
            _put_wing  = abs(_c.get("put_short_strike", 0)  - _c.get("put_long_strike", 0))
            _call_wing = abs(_c.get("call_short_strike", 0) - _c.get("call_long_strike", 0))
            _ml = (max(_put_wing, _call_wing) - _c.get("net_credit", 0)) * _c.get("lot_size", 0)
            if _ml > 0:
                self.risk_manager.add_deployed_capital(
                    _c.get("strategy_name", "iron_condor_v1"), _ml
                )

        # Auto-refresh event calendar every Monday so earnings/RBI dates stay current.
        if now_ist().weekday() == 0:  # 0 = Monday
            _cal_task = asyncio.create_task(self._refresh_event_calendar())
            self._background_tasks.add(_cal_task)
            _cal_task.add_done_callback(self._background_tasks.discard)

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

        # Exit checks — lock prevents interleaving with the 10-second exit-only job (F)
        async with self._exit_cycle_lock:
            await self._check_spread_exits(active_strategies)
            await self._check_condor_exits(active_strategies)
        await self._check_open_option_exits(positions, active_strategies)
        await self._log_portfolio_delta()

        # Refresh risk state after exits so sector/position checks see current positions
        positions = await self._safe_get_positions()
        await self._refresh_risk_state(positions)

        # Auto-kill check: pause strategies that show statistical deterioration
        if self.strategy_monitor:
            await self.strategy_monitor.evaluate_all()

        # Regime detection + strategy switching (runs every cycle, lightweight)
        regime = None
        if self.regime_detector:
            regime = await self.regime_detector.detect()
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
                    await self._process_signal(strategy, symbol, vix=vix, regime=regime)
                except Exception as exc:
                    logger.error(f"Signal error [{strategy_id}:{symbol}]: {exc}")

        # Persist EMA crossover's / Momentum's per-symbol confirmation progress every
        # cycle so a restart mid-confirmation doesn't silently reset it back to zero.
        await self._persist_ema_state()
        await self._persist_momentum_state()

    async def sync_orders(self) -> None:
        try:
            await self.order_manager.sync_orders()
        except Exception as exc:
            logger.error(f"Order sync failed: {exc}")

    async def send_daily_report(self) -> None:
        expiry = get_near_month_expiry()
        dte    = (expiry - now_ist().replace(tzinfo=None)).days

        # Read today's realized PnL from trade_journal (the authoritative source).
        try:
            from datetime import date as _date, datetime as _datetime
            from src.database.connection import AsyncSessionLocal
            from src.database.models.trade_journal import TradeJournal
            from sqlalchemy import select as _select
            today = _date.today()
            today_start = _datetime(today.year, today.month, today.day, 0, 0, 0)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    _select(TradeJournal).where(
                        TradeJournal.exit_time.isnot(None),
                        TradeJournal.exit_time >= today_start,
                    )
                )
                closed_today = result.scalars().all()
            today_realized = sum(float(t.pnl or 0) for t in closed_today)
            closed_count   = len(closed_today)
        except Exception as e:
            logger.error(f"EOD: failed to read trade_journal: {e}")
            today_realized = 0.0
            closed_count   = 0

        positions      = await self._safe_get_positions()
        open_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)

        # ── Overnight positions summary ───────────────────────────────────────
        overnight_spreads = len(self._active_spreads)
        overnight_condors = len(self._active_condors)
        overnight_lines   = []
        if overnight_spreads:
            for sym, s in self._active_spreads.items():
                overnight_lines.append(
                    f"  SPREAD {sym}: {s.get('spread_type','?')} | "
                    f"credit ₹{s.get('net_credit',0):.2f} x {s.get('lot_size',0)}"
                )
        if overnight_condors:
            for sym, c in self._active_condors.items():
                overnight_lines.append(
                    f"  CONDOR {sym}: credit ₹{c.get('net_credit',0):.2f} x {c.get('lot_size',0)}"
                )

        report_lines = [
            f"EOD REPORT — {now_ist().strftime('%d-%b-%Y')} | {self.mode.value.upper()}",
            f"Orders today:    {self._today_order_count}",
            f"Closed trades:   {closed_count}",
            f"Realized PnL:    ₹{today_realized:,.2f}",
            f"Open positions:  {len(positions)}",
            f"Unrealized PnL:  ₹{open_unrealized:,.2f}",
            f"Net PnL today:   ₹{today_realized + open_unrealized:,.2f}",
        ]
        if overnight_lines:
            report_lines.append(
                f"\nOVERNIGHT HOLD ({overnight_spreads} spread(s) + {overnight_condors} condor(s)) "
                f"— DTE {dte} days remaining:"
            )
            report_lines.extend(overnight_lines)
            report_lines.append("Exit conditions (SL / profit / DTE<7) checked every minute tomorrow.")
        else:
            report_lines.append("No overnight positions.")

        if self._today_order_count > 0 or overnight_spreads or overnight_condors:
            await self._notify("\n".join(report_lines))
        else:
            logger.info("EOD: no trades today and no open positions, skipping report.")

        await self._persist_state()
        self._today_order_count = 0
        self._peak_premiums.clear()
        # _active_spreads and _active_condors are intentionally NOT cleared here —
        # credit spreads and iron condors are multi-day theta strategies that must
        # carry overnight. They are managed by _check_spread_exits / _check_condor_exits
        # and will only close when: SL hit, 75% profit reached, DTE < 7, or expiry day.
        self._exited_today.clear()         # reset adverse-exit blocks for next trading day
        self._profit_closed_today.clear()  # reset profit-close re-entry tracking for next day

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
            await redis.set(_REDIS_EXITED_TODAY,  json.dumps({"date": today, "symbols": list(self._exited_today)}))
            await redis.set(_REDIS_PROFIT_CLOSED, json.dumps({"date": today, "symbols": list(self._profit_closed_today)}))
            await redis.set(_REDIS_ORDER_COUNT,   json.dumps({"date": today, "count": self._today_order_count}))
        except Exception as e:
            logger.error(f"Failed to persist engine state: {e}")

    def _find_ema_strategy(self):
        """Return the loaded EMACrossoverStrategy instance, if any."""
        for inst in StrategyRegistry.get_active_strategies().values():
            if inst.__class__.__name__ == "EMACrossoverStrategy":
                return inst
        return None

    async def _persist_ema_state(self) -> None:
        """
        Persist EMA Crossover's per-symbol crossover-confirmation state every cycle.

        Every deploy/restart re-creates the strategy instance from scratch, wiping
        prev_fast_ema/prev_slow_ema/_pending_signal/_pending_count/_pending_bar_key —
        a crossover that was 1 bar away from confirming has to start over from zero.
        During active development (frequent deploys), this can silently prevent the
        strategy from ever completing a 2-bar confirmation. Persisted separately from
        _persist_state() (position/order state) because this needs saving every
        cycle regardless of whether any position actually changed.
        """
        redis = getattr(self, "_redis", None)
        if not redis:
            return
        strategy = self._find_ema_strategy()
        if not strategy:
            return
        try:
            await redis.set(_REDIS_EMA_STATE, json.dumps({
                "prev_fast_ema":   strategy.prev_fast_ema,
                "prev_slow_ema":   strategy.prev_slow_ema,
                "pending_signal":  strategy._pending_signal,
                "pending_count":   strategy._pending_count,
                "pending_bar_key": strategy._pending_bar_key,
            }))
        except Exception as e:
            logger.debug(f"Failed to persist EMA crossover state: {e}")

    async def _restore_ema_state(self) -> None:
        redis = getattr(self, "_redis", None)
        if not redis:
            return
        strategy = self._find_ema_strategy()
        if not strategy:
            return
        try:
            raw = await redis.get(_REDIS_EMA_STATE)
            if not raw:
                return
            state = json.loads(raw)
            strategy.prev_fast_ema    = state.get("prev_fast_ema", {})
            strategy.prev_slow_ema    = state.get("prev_slow_ema", {})
            strategy._pending_signal  = state.get("pending_signal", {})
            strategy._pending_count   = state.get("pending_count", {})
            strategy._pending_bar_key = state.get("pending_bar_key", {})
            logger.info(
                f"Restored EMA crossover state: {len(strategy.prev_fast_ema)} symbol(s) tracked, "
                f"{len(strategy._pending_signal)} pending confirmation(s)"
            )
        except Exception as e:
            logger.warning(f"Failed to restore EMA crossover state: {e}")

    def _find_momentum_strategy(self):
        """Return the loaded MomentumStrategy instance, if any."""
        for inst in StrategyRegistry.get_active_strategies().values():
            if inst.__class__.__name__ == "MomentumStrategy":
                return inst
        return None

    async def _persist_momentum_state(self) -> None:
        """
        Persist Momentum's per-symbol trend-confirmation state every cycle —
        same rationale as _persist_ema_state() (a restart mid-confirmation
        would otherwise silently reset it to zero). Momentum's condition is
        stateless per-cycle (no prev_fast_ema/prev_slow_ema — it doesn't need
        to detect a sign change, just whether ADX/spread currently qualify),
        so only the pending-confirmation trackers need saving, not EMA values.
        """
        redis = getattr(self, "_redis", None)
        if not redis:
            return
        strategy = self._find_momentum_strategy()
        if not strategy:
            return
        try:
            await redis.set(_REDIS_MOMENTUM_STATE, json.dumps({
                "pending_signal":  strategy._pending_signal,
                "pending_count":   strategy._pending_count,
                "pending_bar_key": strategy._pending_bar_key,
            }))
        except Exception as e:
            logger.debug(f"Failed to persist Momentum state: {e}")

    async def _restore_momentum_state(self) -> None:
        redis = getattr(self, "_redis", None)
        if not redis:
            return
        strategy = self._find_momentum_strategy()
        if not strategy:
            return
        try:
            raw = await redis.get(_REDIS_MOMENTUM_STATE)
            if not raw:
                return
            state = json.loads(raw)
            strategy._pending_signal  = state.get("pending_signal", {})
            strategy._pending_count   = state.get("pending_count", {})
            strategy._pending_bar_key = state.get("pending_bar_key", {})
            logger.info(
                f"Restored Momentum state: {len(strategy._pending_signal)} pending confirmation(s)"
            )
        except Exception as e:
            logger.warning(f"Failed to restore Momentum state: {e}")

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

            # Discard stale spreads/condors from a previous expiry cycle.
            # NOTE: compare each position's OWN stored expiry to today directly — do NOT
            # compare against get_near_month_expiry(). Credit-spread/condor entries
            # deliberately roll to the NEXT month whenever near-month DTE < _ENTRY_MIN_DTE
            # (21) for adequate theta runway (see _process_credit_spread /
            # _process_iron_condor), so a held position's expiry routinely differs from
            # the near-month contract by design — that is not staleness. Previously this
            # compared to current_expiry = get_near_month_expiry(), which force-closed
            # every spread/condor holding a next-month contract on every restart —
            # including ones weeks away from expiry. A restored position only needs a
            # forced close if it has drifted inside the exit DTE floor (7) since entry,
            # mirroring the same floor the regular exit cycle enforces below.
            today_dt         = now_ist().replace(tzinfo=None)
            today_str        = today_dt.date().isoformat()
            _RESTORE_MIN_DTE = 7

            truly_stale_spreads: List[str] = []
            for sym, s in self._active_spreads.items():
                stored = s.get("expiry_date", "")
                if not stored:
                    truly_stale_spreads.append(sym)
                elif stored < today_str:
                    # Expiry already passed — position should have been closed already
                    logger.error(
                        f"Spread {sym}: stored expiry {stored} is IN THE PAST — "
                        "was not closed properly. Discarding from tracking."
                    )
                    truly_stale_spreads.append(sym)
                else:
                    dte = (datetime.fromisoformat(stored) - today_dt).days
                    if dte < _RESTORE_MIN_DTE:
                        logger.warning(
                            f"Spread {sym}: expiry {stored} is {dte} day(s) out "
                            f"(< {_RESTORE_MIN_DTE}) — near-expiry restore. Flagging for immediate close."
                        )
                        self._close_on_first_cycle.add(sym)
            for sym in truly_stale_spreads:
                del self._active_spreads[sym]

            truly_stale_condors: List[str] = []
            for sym, c in self._active_condors.items():
                stored = c.get("expiry_date", "")
                if not stored:
                    truly_stale_condors.append(sym)
                elif stored < today_str:
                    logger.error(
                        f"Condor {sym}: stored expiry {stored} is IN THE PAST — "
                        "was not closed properly. Discarding from tracking."
                    )
                    truly_stale_condors.append(sym)
                else:
                    dte = (datetime.fromisoformat(stored) - today_dt).days
                    if dte < _RESTORE_MIN_DTE:
                        logger.warning(
                            f"Condor {sym}: expiry {stored} is {dte} day(s) out "
                            f"(< {_RESTORE_MIN_DTE}) — near-expiry restore. Flagging for immediate close."
                        )
                        self._close_on_first_cycle.add(sym)
            for sym in truly_stale_condors:
                del self._active_condors[sym]
            jrnl_raw = await redis.get(_REDIS_SINGLE_LEG_JRNL)
            if jrnl_raw:
                self._single_leg_journals = json.loads(jrnl_raw)
                today = now_ist().date().isoformat()
                self._single_leg_journals = {
                    k: v for k, v in self._single_leg_journals.items()
                    if v.get("date", today) == today
                }
                logger.info(f"Restored {len(self._single_leg_journals)} single-leg journal(s) for today")

            # Restore today-only state — discard if it's from a previous day
            exited_raw = await redis.get(_REDIS_EXITED_TODAY)
            if exited_raw:
                exited_data = json.loads(exited_raw)
                if exited_data.get("date") == today:
                    self._exited_today = set(exited_data.get("symbols", []))
                    logger.info(f"Restored _exited_today: {self._exited_today}")
            profit_closed_raw = await redis.get(_REDIS_PROFIT_CLOSED)
            if profit_closed_raw:
                profit_data = json.loads(profit_closed_raw)
                if profit_data.get("date") == today:
                    self._profit_closed_today = set(profit_data.get("symbols", []))
                    logger.info(f"Restored _profit_closed_today: {self._profit_closed_today}")
            count_raw = await redis.get(_REDIS_ORDER_COUNT)
            if count_raw:
                count_data = json.loads(count_raw)
                if count_data.get("date") == today:
                    self._today_order_count = count_data.get("count", 0)
                    logger.info(f"Restored today's order count: {self._today_order_count}")
        except Exception as e:
            logger.error(f"Failed to restore engine state: {e}")

        # In paper mode, PaperBroker._positions is in-memory and lost on restart.
        # Reconstruct it from the restored spread/condor/single-leg state so that:
        #   a) _safe_get_positions() returns correct data for risk manager
        #   b) _reconcile_broker_positions() sees broker positions matching engine state
        if hasattr(self.broker, "_positions"):
            await self._rebuild_paper_broker_positions()
            await self._reconcile_paper_broker_balance()

        # After Redis restore, cross-check broker positions for orphans
        await self._reconcile_broker_positions()

    async def _reconcile_paper_broker_balance(self) -> None:
        """
        Reconcile PaperBroker.balance after restart.

        broker.balance is pure in-memory (see paper_broker.py __init__) and
        resets to settings.INITIAL_CAPITAL on every process start — it has no
        idea about realized P&L from trades closed before this restart, or
        about the cash tied up in positions _rebuild_paper_broker_positions()
        just repopulated above. Left alone, "Available Cash" would silently
        show the full starting capital after every restart regardless of
        actual trading history — caught this via the dashboard cash metric
        showing exactly Rs300,000 right after a restart despite Rs65k+ of
        real accumulated realized P&L in trade_journal.

        Reconstructed from first principles:
          balance = initial_capital + total_realized_pnl (all-time, from
                    trade_journal) - cash tied up in currently open positions
                    (quantity * avg_price: negative qty means a short/credit
                    position, so this naturally adds back the premium already
                    received for it rather than treating it as spent).
        """
        if not hasattr(self.broker, "balance"):
            return
        try:
            from src.core.config import settings
            from src.database.connection import AsyncSessionLocal
            from src.database.models.trade_journal import TradeJournal
            from sqlalchemy import select as _select, func as _func

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    _select(_func.sum(TradeJournal.pnl)).where(TradeJournal.exit_time.isnot(None))
                )
                total_realized = float(result.scalar() or 0.0)

            deployed_cash = sum(
                pos["quantity"] * pos["avg_price"] for pos in self.broker._positions.values()
            )
            self.broker.balance = round(settings.INITIAL_CAPITAL + total_realized - deployed_cash, 2)
            logger.info(
                f"Reconciled PaperBroker balance after restart: "
                f"₹{settings.INITIAL_CAPITAL:,.0f} + ₹{total_realized:,.2f} realized "
                f"- ₹{deployed_cash:,.2f} in open positions = ₹{self.broker.balance:,.2f}"
            )
        except Exception as e:
            logger.error(f"Failed to reconcile PaperBroker balance after restart: {e}")

    async def _rebuild_paper_broker_positions(self) -> None:
        """
        Reconstruct PaperBroker._positions from restored spread/condor/
        single-leg state.

        PaperBroker keeps positions in an in-memory dict that is lost on container
        restart. Without this rebuild, _safe_get_positions() returns empty after
        restart, causing the risk manager to think there are 0 open positions and
        potentially allowing duplicate entries for the same underlying.

        Fixed 2026-08-06: this only ever rebuilt spread/condor legs, never
        single-leg positions from _single_leg_journals (ema_crossover_v1/
        momentum_v1) -- the identical "forgot the single-leg case" bug found
        and fixed the same day in the /positions dashboard endpoint, except
        here it has real functional consequences rather than cosmetic ones:
        _check_open_option_exits() iterates broker.get_positions() to decide
        what to evaluate for exit, so any single-leg position open across a
        restart silently stopped receiving stop-loss/target/DTE exit checks
        entirely (confirmed live: today's two open momentum_v1 positions
        had zero exit evaluation for the ~50 minutes between the first
        post-entry restart and this fix landing).
        """
        broker = self.broker
        broker._positions.clear()

        for sym, spread in self._active_spreads.items():
            lot = spread.get("lot_size", 0)
            for contract, qty, price in [
                (spread.get("short_contract", ""), -lot, spread.get("short_premium", 0.0)),
                (spread.get("long_contract",  ""),  lot, spread.get("long_premium",  0.0)),
            ]:
                if contract and lot:
                    broker._positions[contract] = {
                        "symbol": contract, "quantity": qty, "avg_price": float(price)
                    }

        for sym, cond in self._active_condors.items():
            lot = cond.get("lot_size", 0)
            for contract, qty, price in [
                (cond.get("put_short_contract",  ""), -lot, cond.get("put_short_premium",  0.0)),
                (cond.get("put_long_contract",   ""),  lot, cond.get("put_long_premium",   0.0)),
                (cond.get("call_short_contract", ""), -lot, cond.get("call_short_premium", 0.0)),
                (cond.get("call_long_contract",  ""),  lot, cond.get("call_long_premium",  0.0)),
            ]:
                if contract and lot:
                    broker._positions[contract] = {
                        "symbol": contract, "quantity": qty, "avg_price": float(price)
                    }

        single_leg_count = 0
        if self._single_leg_journals:
            from src.database.connection import AsyncSessionLocal
            from src.database.models.trade_journal import TradeJournal
            from src.database.repositories.base import BaseRepository
            journal_repo = BaseRepository(TradeJournal, AsyncSessionLocal)
            for contract, info in self._single_leg_journals.items():
                if not contract:
                    continue
                journal = await journal_repo.get_by_id(info["journal_id"])
                if journal is None or journal.entry_price is None:
                    logger.error(
                        f"Rebuild: single-leg journal for {contract} (id={info.get('journal_id')}) "
                        "not found in trade_journal — cannot restore its broker position; "
                        "it will be invisible to exit checks until manually corrected."
                    )
                    continue
                broker._positions[contract] = {
                    "symbol": contract,
                    "quantity": journal.quantity or 0,
                    "avg_price": float(journal.entry_price),
                }
                single_leg_count += 1

        # Rebuild margin_blocked for restored short legs too — this bypasses
        # place_order() entirely, so PaperBroker's own margin bookkeeping
        # (see SHORT_MARGIN_MULTIPLE_OF_PREMIUM in paper_broker.py) never runs
        # for these restored positions. Without this, every restart would
        # silently forget that margin is held against them, understating how
        # much of the account is actually committed.
        if hasattr(broker, "margin_blocked"):
            broker.margin_blocked.clear()
            from src.paper_trading.paper_broker import SHORT_MARGIN_MULTIPLE_OF_PREMIUM
            for contract, pos in broker._positions.items():
                if pos["quantity"] < 0:
                    broker.margin_blocked[contract] = (
                        abs(pos["quantity"]) * pos["avg_price"] * SHORT_MARGIN_MULTIPLE_OF_PREMIUM
                    )

        total = len(broker._positions)
        if total:
            logger.info(
                f"Rebuilt PaperBroker positions after restart: "
                f"{len(self._active_spreads)} spread(s) + {len(self._active_condors)} condor(s) "
                f"+ {single_leg_count} single-leg(s) → {total} contract legs"
            )

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
            f"RECONCILE: {len(orphans)} orphaned position(s) found — engine lost tracking "
            "(likely Redis flush or crash). Auto-closing to prevent unmonitored exposure."
        )
        from src.market_data.option_chain import get_option_quote
        kite  = getattr(self, "_kite",  None)
        redis = getattr(self, "_redis", None)
        expiry = get_near_month_expiry()
        dte    = (expiry - now_ist().replace(tzinfo=None)).days
        closed_orphans = 0
        for p in orphans:
            contract = p.get("symbol", "")
            qty      = p.get("quantity", 0)
            avg_p    = float(p.get("avg_price") or 0)
            side     = "BUY" if qty < 0 else "SELL"   # reverse to close

            # Attempt live price first, fall back to avg entry price
            exit_p = await get_option_quote(contract, kite, redis) or avg_p
            logger.critical(
                f"  ORPHAN CLOSE: {side} {abs(qty)} {contract} @ ₹{exit_p:.2f}"
            )
            try:
                await self.order_manager.place_order(
                    contract, side, abs(qty), exit_p, is_exit_order=True,
                    product_override=p.get("product"),
                )
                closed_orphans += 1
            except Exception as e:
                logger.error(f"  ORPHAN CLOSE FAILED for {contract}: {e}")

        await self._notify(
            f"ORPHAN POSITION ALERT\n"
            f"Found {len(orphans)} untracked position(s) on startup (Redis likely flushed).\n"
            f"Auto-closed {closed_orphans}/{len(orphans)} position(s).\n"
            + "\n".join(
                f"  {p.get('symbol')} qty={p.get('quantity')} avg=₹{p.get('avg_price','?')}"
                for p in orphans
            )
        )

    # ── Portfolio delta monitoring ─────────────────────────────────────────────

    async def _log_portfolio_delta(self) -> None:
        """
        Compute and log the aggregate directional exposure of all open spreads/condors.
        Bullish structures (BULL_PUT_SPREAD) add positive delta; bearish ones subtract.
        Iron condors are roughly delta-neutral (both wings offset).
        Logged each signal cycle for monitoring — does not block entries yet.
        """
        if not self._active_spreads and not self._active_condors:
            return
        bullish = sum(
            1 for s in self._active_spreads.values()
            if s.get("spread_type") == "BULL_PUT_SPREAD"
        )
        bearish = sum(
            1 for s in self._active_spreads.values()
            if s.get("spread_type") == "BEAR_CALL_SPREAD"
        )
        condors = len(self._active_condors)
        net_bias = bullish - bearish
        logger.info(
            f"[PortfolioDelta] spreads={len(self._active_spreads)} "
            f"(bullish={bullish}, bearish={bearish}, net_bias={net_bias:+d}) "
            f"condors={condors} (delta-neutral)"
        )

    # ── Gap check + fast exit job + GTT backstop ─────────────────────────────

    async def _check_gap_opens(self) -> None:
        """
        A: Called at 09:16:30 IST — catches overnight gap breaches before the
        first 60-second signal cycle fires (~09:17).

        A stock can gap 5–8% overnight on news/results. Without this check the
        first exit cycle would only fire at 09:17, by which point the loss from
        holding a breached short strike is already locked in.

        The existing breach detection in _check_spread_exits / _check_condor_exits
        handles the actual exit logic — we just call it 90 seconds early.
        """
        if not self._active_spreads and not self._active_condors:
            return
        logger.info(
            f"[GapCheck] Scanning {len(self._active_spreads)} spread(s) and "
            f"{len(self._active_condors)} condor(s) for overnight gap breaches..."
        )
        async with self._exit_cycle_lock:
            active_strategies = StrategyRegistry.get_active_strategies()
            await self._check_spread_exits(active_strategies)
            await self._check_condor_exits(active_strategies)

    async def _run_exit_checks_only(self) -> None:
        """
        F: 10-second exit-monitoring job.

        Runs _check_spread_exits and _check_condor_exits every 10 seconds so
        that stop-losses and profit targets are caught within 10 seconds of
        the trigger price being reached, not up to 60 seconds.

        Skips silently if the main signal cycle (or gap check) already holds the
        lock — the 1-minute cycle is the primary and should not be blocked.
        """
        if not self.is_running or not is_market_open():
            return
        if is_square_off_time():
            return
        if not self._active_spreads and not self._active_condors:
            return
        if self._exit_cycle_lock.locked():
            return  # main cycle is running — skip this tick
        async with self._exit_cycle_lock:
            active_strategies = StrategyRegistry.get_active_strategies()
            await self._check_spread_exits(active_strategies)
            await self._check_condor_exits(active_strategies)

    async def _place_gtt_backstop(
        self,
        contract: str,
        lot_size: int,
        entry_price: float,
        trigger_mult: float = 2.5,
    ) -> Optional[int]:
        """
        I: Place a Zerodha GTT buy-back order on a short leg.

        Fires automatically at the exchange if the option price rises to
        trigger_mult × entry_price. Acts as a server-independent emergency stop
        — protects the position even if the server crashes entirely.

        Only active in LIVE mode (paper broker has no GTT support).
        Returns the GTT trigger_id or None.
        """
        if self.mode != TradingMode.LIVE:
            return None
        kite = getattr(self, "_kite", None)
        if not kite:
            # Fixed 2026-08-07: this LIVE-mode branch previously returned
            # silently, same as the "not LIVE mode" no-op case above -- but
            # this one means we're actually in live trading with a short
            # leg open and NO kite client to place the exchange-level
            # backstop with. That's a real gap, not an expected/normal
            # condition, and deserves the same loud alert as the exception
            # path below.
            logger.critical(f"[GTT] No kite client available — backstop NOT placed for {contract}.")
            await self._notify(
                f"WARNING: GTT backstop NOT placed\nContract: {contract}\n"
                f"Reason: no kite client available\n"
                f"This position has NO server-independent exchange-level stop-loss."
            )
            return None
        trigger_price = round(entry_price * trigger_mult, 2)
        try:
            loop   = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: kite.place_gtt(
                    trigger_type="single",
                    tradingsymbol=contract,
                    exchange="NFO",
                    trigger_values=[trigger_price],
                    last_price=entry_price,
                    orders=[{
                        "transaction_type": "BUY",
                        "quantity":         lot_size,
                        "order_type":       "MARKET",
                        "product":          "NRML",
                        "price":            0,
                    }],
                ),
            )
            gtt_id = result.get("trigger_id")
            logger.info(
                f"[GTT] Backstop on {contract}: trigger ₹{trigger_price:.2f} "
                f"({trigger_mult}× entry ₹{entry_price:.2f}) → GTT #{gtt_id}"
            )
            return gtt_id
        except Exception as e:
            # Fixed 2026-08-07: this only ever logged a WARNING -- easy to
            # miss in a log file nobody's actively watching, for a feature
            # whose entire purpose is "protect the position even if nobody
            # is watching" (server-independent, exchange-level backstop).
            # A silent failure here means the operator believes a short
            # option leg has exchange-level protection when it doesn't,
            # with no way to find out except by manually checking Zerodha's
            # GTT dashboard. Now escalates to a real notification, same as
            # every other genuinely dangerous state (unwind failures etc.).
            logger.critical(f"[GTT] Failed to place backstop on {contract}: {e}")
            await self._notify(
                f"WARNING: GTT backstop FAILED\nContract: {contract}\n"
                f"Error: {e}\n"
                f"This position has NO server-independent exchange-level stop-loss. "
                f"Consider closing manually or monitoring closely."
            )
            return None

    async def _cancel_gtt(self, gtt_id: Optional[int], contract: str = "") -> None:
        """I: Cancel a GTT backstop after normal exit. No-op if gtt_id is None."""
        if not gtt_id or self.mode != TradingMode.LIVE:
            return
        kite = getattr(self, "_kite", None)
        if not kite:
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: kite.delete_gtt(gtt_id))
            logger.info(f"[GTT] Cancelled backstop #{gtt_id} ({contract})")
        except Exception as e:
            # GTT may have already fired (SL hit at exchange level) — not an error
            logger.debug(f"[GTT] Could not cancel #{gtt_id} ({contract}): {e}")

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
                    quotes = await _asyncio.get_running_loop().run_in_executor(
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
            contract_otm[s.get("short_contract", "")] = 1   # short is ~1 interval OTM (~0.20 delta)
            contract_otm[s.get("long_contract", "")]  = 3   # long is ~3 intervals OTM (~0.10 delta)
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

            atr = float(market_data.get("atr14", 0))
            from src.market_data.option_chain import get_option_quote
            _live_p = await get_option_quote(contract, getattr(self, "_kite", None), getattr(self, "_redis", None))
            if _live_p and _live_p > 0:
                current_p = _live_p
            elif atr > 0:
                current_p = estimate_option_premium(atr, dte)
            else:
                current_p = entry_p
            await self.portfolio_manager.update_position_market_price(contract, current_p)

            peak = self._peak_premiums.get(contract, entry_p)
            if current_p > peak:
                self._peak_premiums[contract] = current_p
                peak = current_p

            exit_reason: Optional[str] = None
            if dte < 4:
                exit_reason = f"DTE={dte} — entering illiquid expiry window"

            # Resolve the strategy that actually OPENED this position (via the
            # strategy_name recorded in _single_leg_journals at entry) rather
            # than assuming EMA Crossover — that assumption held only while it
            # was the sole single-leg strategy. Now that momentum_v1 also opens
            # single-leg positions, using the wrong strategy's manage_position()
            # would apply the wrong SL/TP/trailing thresholds to it.
            owner_info = self._single_leg_journals.get(contract, {})
            owner_strategy = active_strategies.get(owner_info.get("strategy_name", ""))
            if owner_strategy is None:
                # Fallback for a position somehow missing its journal entry —
                # matches the old EMA-only assumption as a safe default.
                owner_strategy = next(
                    (s for s in active_strategies.values()
                     if s.__class__.__name__ == "EMACrossoverStrategy"), None
                )

            if exit_reason is None and owner_strategy is not None:
                result = owner_strategy.manage_position(
                    {
                        "avg_price": entry_p,
                        "peak_premium": peak,
                        "current_adx": market_data.get("adx14"),
                        # For the VOLATILE crash-catching reversal exit (see
                        # EMACrossoverStrategy.manage_position() /
                        # MomentumStrategy.adx_exit_threshold_volatile) — both
                        # unused by strategies that don't check them, so this
                        # is a no-op addition for anything else.
                        "current_ema_fast": market_data.get("ema20"),
                        "current_ema_slow": market_data.get("ema50"),
                        "is_call":          contract.endswith("CE"),
                        "entry_regime":     owner_info.get("entry_regime"),
                    },
                    current_p,
                )
                if result == "EXIT":
                    pnl_pct = (current_p - entry_p) / entry_p * 100
                    exit_reason = (
                        f"{owner_strategy.name} entry=Rs{entry_p:.2f} "
                        f"now=Rs{current_p:.2f} ({pnl_pct:+.1f}%)"
                    )

            if exit_reason:
                await self._execute_single_leg_exit(contract, qty, entry_p, current_p, exit_reason)

    async def _execute_single_leg_exit(
        self, contract: str, qty: int, entry_p: float, current_p: float, exit_reason: str,
    ) -> bool:
        """
        Shared exit-execution logic for a single-leg position: places the
        SELL order, journals the close, releases deployed capital, and
        notifies. Factored out of _check_open_option_exits() so
        close_single_leg_position() (manual/admin close) can reuse the exact
        same path instead of a raw DB edit — PaperBroker's position ledger
        and balance live in the running process's memory, not the DB, so a
        SQL-only "fix" wouldn't actually close the position from the
        engine's point of view. Returns True if the exit succeeded.
        """
        logger.info(f"EXIT [{contract}]: {exit_reason}")
        # Peeked (not popped) before the order call: a CLOSING order's
        # product type must match the ORIGINAL position's at Zerodha (see
        # ZerodhaBroker._product_for()), so this needs the same
        # strategy_name the entry used, not just entries.
        _owner_strategy = self._single_leg_journals.get(contract, {}).get("strategy_name")
        db_order = await self.order_manager.place_order(
            contract, "SELL", abs(qty), current_p, is_exit_order=True, strategy_name=_owner_strategy,
        )
        if not (db_order and db_order.order_status not in ("REJECTED_BY_RISK", "FAILED")):
            logger.error(f"EXIT FAILED [{contract}]: order rejected or failed — will retry next cycle")
            return False

        self._peak_premiums.pop(contract, None)
        # Fixed 2026-08-06: was getattr(db_order, "avg_price", ...) -- the
        # Order model's real column is fill_price (see database/models/
        # order.py); "avg_price" doesn't exist on it at all, so this always
        # silently fell back to current_p (the pre-slippage quoted price)
        # instead of the real PaperBroker fill. Confirmed live: POWERGRID's
        # exit today recorded pnl=+190 (from the quote) while the real fill
        # (with slippage + fees on both legs) worked out to roughly -770 --
        # every single-leg trade_journal.pnl this system has ever recorded
        # was blind to execution slippage entirely, not just this one.
        _fill_p = getattr(db_order, "fill_price", None) or current_p
        _slip   = abs(_fill_p - current_p)
        pnl = (_fill_p - entry_p) * abs(qty)

        # Write exit to trade_journal so PnL appears in analytics + dashboard
        info = self._single_leg_journals.pop(contract, None)
        if info:
            md = await self._get_market_data(info["underlying"])
            await self._log_trade_close(
                journal_id=info["journal_id"],
                exit_price=_fill_p,
                pnl=pnl,
                exit_reason=exit_reason,
                market_data=md,
                total_slippage_pts=round(_slip, 4) if _slip > 0 else None,
            )
            self.risk_manager.release_deployed_capital(
                info.get("strategy_name", "ema_crossover_v1"), entry_p * abs(qty),
            )
            await self._persist_state()

        await self._notify(
            f"POSITION CLOSED\nContract: {contract}\n"
            f"Reason: {exit_reason}\n"
            f"Entry: Rs{entry_p:.2f} -> Exit: Rs{current_p:.2f}\n"
            f"Est. PnL: Rs{pnl:,.2f}"
        )
        return True

    async def close_single_leg_position(self, contract: str, reason: str) -> bool:
        """
        Manually force-close an open single-leg position outside the normal
        per-cycle exit-rule evaluation — e.g. to correct a position that
        references an invalid contract symbol (wrong strike interval, etc.)
        and can therefore never receive a real quote or fire a normal exit
        condition against real data. Uses the same real-quote-first,
        ATR-estimate-fallback pricing as every other exit path. Returns True
        if a matching open position was found and closed.
        """
        info = self._single_leg_journals.get(contract)
        if not info:
            return False
        positions = await self._safe_get_positions()
        pos = next((p for p in positions if p.get("symbol") == contract), None)
        if not pos:
            return False
        qty     = pos.get("quantity", 0)
        entry_p = float(pos.get("avg_price") or 0)

        market_data = await self._get_market_data(info["underlying"]) or {}
        atr = float(market_data.get("atr14", 0))
        expiry = get_near_month_expiry()
        dte = (expiry - now_ist().replace(tzinfo=None)).days

        from src.market_data.option_chain import get_option_quote
        _live_p = await get_option_quote(contract, getattr(self, "_kite", None), getattr(self, "_redis", None))
        if _live_p and _live_p > 0:
            current_p = _live_p
        elif atr > 0:
            current_p = estimate_option_premium(atr, dte)
        else:
            current_p = entry_p

        return await self._execute_single_leg_exit(contract, qty, entry_p, current_p, reason)

    async def _process_signal(
        self, strategy, symbol: str, vix: Optional[float] = None,
        regime: Optional[str] = None,
    ) -> None:
        if not strategy.is_active:
            return
        market_data = await self._get_market_data(symbol)
        if not market_data:
            return

        # Skip entirely while this symbol is still on the historical-data
        # bootstrap fallback (ltp_source == "zerodha_historical" — see
        # ltp_poller._enrich()) rather than genuine live ticks. Added
        # 2026-08-06: confirmed live that on 2026-08-03 this fallback lasted
        # from market open (09:15) through ~09:33 (18 min — longer than
        # _ENTRY_WARMUP_MINUTES=15), during which every symbol's "EMA" was
        # frozen on the prior trading day's closing price. Calling
        # generate_signal() during that window doesn't just risk a bad entry
        # (already blocked by the warmup for most of it) -- for stateful
        # strategies (EMACrossoverStrategy/MomentumStrategy track
        # prev_fast_ema/prev_slow_ema and pending-confirmation bar counts
        # across cycles) it silently seeds that state from frozen, non-moving
        # data. A real crossover was verified via Zerodha's own historical
        # data to have happened at 09:25:00 that morning on MARUTI/NESTLEIND
        # (both in ema_crossover_v1's pool) and was never detected -- this
        # closes that gap by not touching strategy state at all until data
        # is genuinely live, so the first real comparison is against a true
        # previous value instead of a stale frozen one.
        if market_data.get("ltp_source") == "zerodha_historical":
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

        # VOLATILE (VIX>20) crash-catching gate — added 2026-07-31. VIX is a
        # fear/uncertainty gauge, not a directional one: a spike overwhelmingly
        # correlates with sharp selloffs, not bull sprints, so only the PE
        # (bearish) side is allowed while VOLATILE is active. CE/long-call
        # entries stay blocked, same as before this regime was added to
        # REGIME_STRATEGY_MAP. See EMACrossoverStrategy.manage_position()'s
        # EMA-reversal exit and MomentumStrategy.adx_exit_threshold_volatile
        # for the matching tightened exit these positions get.
        if regime == "VOLATILE" and signal_str == "BUY":
            logger.info(
                f"[{strategy.name}] {symbol} BUY skipped — VOLATILE regime "
                "only allows PE (bearish/crash-catching) entries, no long calls."
            )
            return

        if self._max_daily_orders > 0 and self._today_order_count >= self._max_daily_orders:
            logger.warning(f"Daily order limit ({self._max_daily_orders}) reached.")
            return

        underlying_price = float(market_data.get("close", 0))
        if underlying_price <= 0:
            return

        option_type = "CE" if signal_str == "BUY" else "PE"
        opposite    = "PE" if option_type == "CE" else "CE"

        await self._close_option_positions(symbol, opposite, market_data, owner_strategy_name=strategy.name)
        if await self._has_open_option(symbol, option_type):
            return

        # Portfolio-wide cap on simultaneous single-leg long-option positions —
        # shared across ALL strategies that emit BUY/SELL (ema_crossover_v1 and,
        # since 2026-07-30, momentum_v1 too), since _single_leg_journals holds
        # every currently-open single-leg position regardless of which strategy
        # opened it. Intentionally shared, not per-strategy: this caps total
        # naked-long-option concentration, which is the same risk whichever
        # strategy generated it.
        _open_intraday = len(self._single_leg_journals)
        if _open_intraday >= self._max_concurrent_intraday:
            logger.info(
                f"[{strategy.name}] {symbol} skipped — {_open_intraday}/"
                f"{self._max_concurrent_intraday} intraday positions already open "
                "(concurrency cap)."
            )
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

        # RVOL filter — require above-average volume (RVOL > 1.3) for momentum entries.
        # Low-volume breakouts have higher false-positive rates and wider bid-ask spreads.
        #
        # Fixed 2026-07-30: this was silently a no-op almost all session, every day,
        # from the live-tick redesign (2026-07-27) until now. RVOL's 20-period rolling
        # volume average is computed over a blended historical+live-tick bar series
        # (see ltp_poller._enrich()), but WebSocket ticks arrived in MODE_LTP (price
        # only, no volume — see zerodha_ticker.py), so every live-tick bar carried
        # volume=0. Once ~20 live bars existed (~100 min into the session), the entire
        # rolling window was zero-volume and rvol computed to exactly 0.0 — and since
        # this check only blocks when `_rvol > 0`, a value of exactly 0 always passed.
        # ZerodhaTicker now subscribes in MODE_QUOTE (real volume_traded per tick,
        # accumulated into per-bar volume by update_intraday_bar() in core/utils.py),
        # so rvol carries a genuine non-zero signal once live bars accumulate.
        # Fixed 2026-08-06: `if _rvol > 0` treated "insufficient data to
        # compute a real 20-period average yet" (rvol_valid=False, e.g. the
        # first ~100 min of every session) the SAME as "confirmed above
        # threshold" -- both silently passed. A volume-confirmation filter
        # that can't confirm volume shouldn't wave the entry through; it
        # should block until it can, like every other unconfirmed-data case.
        _rvol = float(market_data.get("rvol", 0))
        _rvol_valid = bool(market_data.get("rvol_valid", False))
        if not _rvol_valid:
            logger.info(
                f"[{strategy.name}] {symbol} skipped — RVOL not yet computable "
                "(insufficient volume history; cannot confirm breakout strength)"
            )
            return
        if _rvol < 1.3:
            logger.info(
                f"[{strategy.name}] {symbol} skipped — RVOL={_rvol:.2f} < 1.3 "
                "(below-average volume; weak breakout confirmation)"
            )
            return

        # ADX filter — require strong trend (ADX > 25) for EMA crossover momentum plays.
        # A crossover in a low-ADX environment is likely noise rather than a trend change.
        # Fixed 2026-08-06: same fail-open-on-missing-data issue as RVOL above.
        _adx_ema = float(market_data.get("adx14", 0))
        _adx_ema_valid = bool(market_data.get("adx_valid", False))
        if not _adx_ema_valid:
            logger.info(
                f"[{strategy.name}] {symbol} skipped — ADX not yet computable "
                "(insufficient history; cannot confirm trend strength)"
            )
            return
        if _adx_ema < 25:
            logger.info(
                f"[{strategy.name}] {symbol} skipped — ADX={_adx_ema:.1f} < 25 "
                "(trend not strong enough for momentum entry)"
            )
            return

        # Relative Strength filter (wired in 2026-07-30 — RSRanker was computing
        # and publishing ranks every cycle but nothing ever consulted them).
        # Trend-following works best on stocks already outperforming NIFTY (see
        # rs_ranker.py docstring: "Only the top-N by RS score are eligible for
        # trading"). Applied to BUY only — RSRanker ranks strongest-to-weakest,
        # which is the correct gate for "buy calls on strength," but it has no
        # symmetric "weakest stocks" ranking that would justify gating SELL
        # (put-buying) entries the same way, so SELL is intentionally left
        # ungated here. Fails open (allows the trade) whenever ranks aren't
        # available yet (e.g. first few minutes after market open, before
        # RSRanker's first cycle has completed) rather than blocking on an
        # empty/placeholder list.
        if signal_str == "BUY" and self.rs_ranker:
            try:
                _rs_ranks = await self.rs_ranker.get_ranks()
            except Exception:
                _rs_ranks = []
            if _rs_ranks:
                _rs_top_syms = {e["symbol"] for e in _rs_ranks[:10]}
                if symbol not in _rs_top_syms:
                    logger.info(
                        f"[{strategy.name}] {symbol} skipped — not in RS top-10 "
                        "vs NIFTY (relative strength too weak for a long entry)"
                    )
                    return

        # Multi-timeframe confirmation — 15-min EMA direction must agree with 5-min signal.
        # A 5-min crossover against the 15-min trend is counter-trend and fails more often.
        _redis_mtf = getattr(self, "_redis", None)
        if _redis_mtf:
            try:
                _raw15 = await _redis_mtf.get(f"tick15:{symbol}")
                if _raw15:
                    _d15       = json.loads(_raw15)
                    _ema20_15  = float(_d15.get("ema20", 0))
                    _ema50_15  = float(_d15.get("ema50", 0))
                    if _ema20_15 > 0 and _ema50_15 > 0:
                        _tf15_bull = _ema20_15 > _ema50_15
                        _tf5_bull  = signal_str == "BUY"
                        if _tf15_bull != _tf5_bull:
                            logger.info(
                                f"[{strategy.name}] {symbol} skipped — "
                                f"15-min EMA trend ({'bullish' if _tf15_bull else 'bearish'}) "
                                f"contradicts 5-min signal ({signal_str})"
                            )
                            return
            except Exception:
                pass  # MTF data unavailable — proceed without filter

        lot_size = await self._get_lot_size(symbol)
        atr      = float(market_data.get("atr14", underlying_price * 0.01))
        iv_rank  = await self._get_iv_rank(symbol, underlying_price, atr, dte)
        strike   = get_atm_strike(underlying_price, symbol)
        contract = build_option_symbol(symbol, strike, option_type, expiry)

        # Fixed 2026-08-06: this unconditionally used the crude ATR-heuristic
        # estimate at entry, while every exit path (_check_open_option_exits,
        # _close_option_positions, _exit_all_options_for) already tries a real
        # Zerodha quote first and only falls back to the same estimate — an
        # asymmetry that made recorded P&L largely reflect "how far off was
        # the entry guess from the real market price" rather than genuine
        # price movement of the same instrument. Confirmed live 2026-08-03
        # through 08-05: momentum_v1 showed repeated +190%-283% "wins" that
        # closed within ~1 minute of entry (a real option premium tripling in
        # 60s isn't plausible) alongside -30%-50% "losses" over longer holds
        # — both artifacts of the estimate-vs-real gap, not real trading
        # outcomes. credit_spread_v1/iron_condor_v1 entries never had this
        # bug (get_entry_prices_for_spread() already tries a real quote
        # first) — this brings single-leg entries in line with that same,
        # already-correct pattern.
        from src.market_data.option_chain import get_option_quote
        _real_p = await get_option_quote(contract, getattr(self, "_kite", None), getattr(self, "_redis", None))
        option_p = _real_p if (_real_p and _real_p > 0) else estimate_option_premium(atr, dte)

        order = await self.order_manager.place_order(
            contract, "BUY", lot_size, option_p,
            strategy_name=strategy.name,
            iv_rank=iv_rank, vix=vix,
        )
        if order and order.order_status == "OPEN":
            self._today_order_count += 1
            self._peak_premiums[contract] = option_p
            # Fixed 2026-08-06: was entry_price=option_p (the pre-slippage
            # quoted price) -- record the real PaperBroker fill instead, same
            # fix as the exit-side fill_price bug above, so entry_price
            # reflects what was actually paid rather than what was quoted a
            # moment before the simulated bid-ask slippage was applied.
            _entry_fill = getattr(order, "fill_price", None) or option_p
            journal_id = await self._log_trade_open(
                strategy=strategy.name, underlying=symbol,
                structure_type="SINGLE_LEG", contracts=[contract],
                entry_price=_entry_fill, quantity=lot_size,
                market_data=market_data, iv_rank=iv_rank, vix=vix,
            )
            if journal_id:
                self._single_leg_journals[contract] = {
                    "journal_id":    journal_id,
                    "underlying":    symbol,
                    "strategy_name": strategy.name,
                    "date":          now_ist().date().isoformat(),
                    # Regime at entry (not current regime at exit time) — the
                    # VOLATILE-specific tightened exit logic in manage_position()
                    # keys off this, so a crash-catching entry keeps its faster
                    # reversal exit for its whole life even if VIX later calms
                    # down mid-day.
                    "entry_regime":  regime,
                }
                if self._ltp_poller:
                    self._ltp_poller.register_option_contracts([contract])
                await self._persist_state()
            await self._notify(
                f"ORDER PLACED\nStrategy: {strategy.name}\n"
                f"BUY {lot_size} {contract} @ Rs{option_p:.2f}\n"
                f"Underlying: {symbol} @ Rs{underlying_price:.2f} | DTE: {dte}\n"
                f"IV Rank: {f'{iv_rank:.2f}' if iv_rank is not None else 'N/A'} | "
                f"VIX: {f'{vix:.1f}' if vix else 'N/A'}"
            )

    async def _close_option_positions(
        self, underlying: str, option_type: str, market_data: Dict,
        owner_strategy_name: Optional[str] = None,
    ) -> None:
        """
        Reversal exit: closes an opposite-direction option on `underlying` before
        a fresh same-underlying entry opens. If owner_strategy_name is given, only
        closes positions actually opened by THAT strategy (per _single_leg_journals)
        — a position with no journal entry at all is closed regardless, as a safe
        fallback for legacy positions predating this check.

        Without this guard, one strategy's fresh opposite-direction signal would
        silently close ANOTHER strategy's open position on the same underlying,
        bypassing that strategy's own SL/TP/trailing-stop logic entirely. This
        was harmless while ema_crossover_v1 was the only strategy using this path
        (a strategy reversing its own position), but became a real cross-strategy
        interaction risk once momentum_v1 started sharing it too — e.g. momentum_v1
        holding a PE that's tracking toward its trailing stop could get force-closed
        by an unrelated ema_crossover_v1 CE signal on the same stock, crystallizing
        momentum_v1's P&L at a moment momentum_v1's own exit rules never chose.
        """
        from src.market_data.option_chain import get_option_quote
        positions = await self._safe_get_positions()
        expiry = get_near_month_expiry()
        dte    = (expiry - now_ist().replace(tzinfo=None)).days
        atr    = float(market_data.get("atr14", 0))
        opposite_type = option_type  # the type being closed (PE or CE)
        for pos in positions:
            contract = pos.get("symbol", "")
            qty      = pos.get("quantity", 0)
            if qty <= 0 or not (contract.startswith(underlying) and contract.endswith(option_type)):
                continue
            # Looked up unconditionally now (was only computed inside the
            # owner_strategy_name filter branch) -- needed either way to
            # pass the correct strategy_name to place_order(), since a
            # CLOSING order's product type must match the ORIGINAL
            # position's at Zerodha (see ZerodhaBroker._product_for()).
            _owner = self._single_leg_journals.get(contract, {}).get("strategy_name")
            if owner_strategy_name is not None and _owner is not None and _owner != owner_strategy_name:
                continue
            entry_p = float(pos.get("avg_price") or 0)
            _live_p = await get_option_quote(contract, getattr(self, "_kite", None), getattr(self, "_redis", None))
            exit_p = _live_p if (_live_p and _live_p > 0) else (estimate_option_premium(atr, dte) if atr > 0 else entry_p)
            _rev_order = await self.order_manager.place_order(
                contract, "SELL", abs(qty), exit_p, is_exit_order=True, strategy_name=_owner,
            )
            self._peak_premiums.pop(contract, None)
            # Fixed 2026-08-07: sixth instance of the fill_price bug fixed
            # earlier today (single-leg/spread/condor exits, square-off,
            # exit_all_options_for) -- pnl was computed from exit_p (the
            # quote/estimate fed INTO place_order()), and place_order()'s
            # return value was discarded entirely, never reading the real
            # PaperBroker fill.
            _rev_fill_p = getattr(_rev_order, "fill_price", None) or exit_p
            logger.info(f"REVERSAL EXIT: SELL {contract} @ Rs{_rev_fill_p:.2f}")
            jrnl = self._single_leg_journals.pop(contract, None)
            if jrnl:
                pnl = (_rev_fill_p - entry_p) * abs(qty)
                await self._log_trade_close(
                    journal_id=jrnl.get("journal_id"),
                    exit_price=_rev_fill_p,
                    pnl=pnl,
                    exit_reason=f"Reversal exit ({opposite_type} signal)",
                    market_data=market_data,
                )
                self.risk_manager.release_deployed_capital(
                    jrnl.get("strategy_name", "ema_crossover_v1"), entry_p * abs(qty),
                )
                await self._persist_state()

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
        if symbol in self._active_condors:
            return  # already have a condor on this underlying — conflicting structures
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
        now      = now_ist()
        dte      = (expiry - now.replace(tzinfo=None)).days
        min_dte  = getattr(strategy, "min_dte", 7)
        if dte < min_dte:
            logger.info(
                f"[CreditSpread] {symbol} skipped — DTE={dte} < min_dte={min_dte}, too close to expiry"
            )
            return

        # DTE floor logic:
        #   Fresh entries need DTE ≥ 21 — enough theta runway for 2+ weeks of decay before
        #   the min_dte=7 exit fires. At DTE 25 → enter; at DTE 18 → already in position.
        #   Re-entries after a profitable same-day close use a lower floor (DTE ≥ 14) since
        #   we proved the position was directionally correct and collected premium once already.
        #   If the near-month contract itself is short on runway for a *fresh* entry, roll
        #   straight to next month instead of going dark until the 7-day exit rollover —
        #   otherwise every symbol is unenterable for ~2 weeks before each monthly expiry.
        _ENTRY_MIN_DTE   = 21
        _REENTRY_MIN_DTE = 14
        if symbol in self._profit_closed_today:
            if dte < _REENTRY_MIN_DTE:
                logger.info(
                    f"[CreditSpread] {symbol} skipped re-entry — DTE={dte} < {_REENTRY_MIN_DTE} "
                    "re-entry floor (profit closed today)."
                )
                return
            logger.info(
                f"[CreditSpread] {symbol} re-entering after same-day profit close (DTE={dte})"
            )
        elif dte < _ENTRY_MIN_DTE:
            expiry = get_entry_expiry(_ENTRY_MIN_DTE)
            dte    = (expiry - now.replace(tzinfo=None)).days
            logger.info(
                f"[CreditSpread] {symbol} near-month DTE < {_ENTRY_MIN_DTE} — "
                f"rolling fresh entry to next month's expiry (DTE={dte})"
            )

        # No new entries after 14:30 IST — at least one exit-check cycle before
        # the 15:20 EOD window, and avoids rushed afternoon entries.
        _ENTRY_CUTOFF_HOUR, _ENTRY_CUTOFF_MIN = 14, 30
        if (now.replace(tzinfo=None).hour, now.replace(tzinfo=None).minute) >= (_ENTRY_CUTOFF_HOUR, _ENTRY_CUTOFF_MIN):
            logger.debug(f"[CreditSpread] {symbol} skipped — past entry cutoff 14:30 IST.")
            return

        # D: SL frequency circuit breaker — block re-entry after 2+ adverse exits in 5 days.
        # Prevents repeatedly entering a stock that's in a sustained adverse trend.
        _redis_cb = getattr(self, "_redis", None)
        if _redis_cb:
            _sl_freq = await _redis_cb.get(f"sl_freq:{symbol}")
            if _sl_freq and int(_sl_freq) >= 2:
                logger.info(
                    f"[CreditSpread] {symbol} blocked — {_sl_freq} adverse SL exit(s) "
                    "in last 5 days (circuit breaker). Will unblock automatically."
                )
                return

        # H: Sector concentration check — max 2 open structures per sector.
        # Prevents correlated sector blow-ups (e.g. all pharma positions hit simultaneously).
        _sym_sector = FNO_SECTORS.get(symbol)
        if _sym_sector:
            _sector_count = sum(
                1 for s in list(self._active_spreads) + list(self._active_condors)
                if FNO_SECTORS.get(s) == _sym_sector
            )
            if _sector_count >= MAX_SECTOR_POSITIONS:
                logger.info(
                    f"[CreditSpread] {symbol} ({_sym_sector}) skipped — "
                    f"{_sector_count}/{MAX_SECTOR_POSITIONS} positions already open in sector."
                )
                return

        lot_size   = await self._get_lot_size(symbol)
        atr        = float(market_data.get("atr14", underlying_price * 0.01))
        iv_rank    = await self._get_iv_rank(symbol, underlying_price, atr, dte)
        _atr_sigma = atr_to_annualised_vol(atr * _5MIN_ATR_SCALE, underlying_price)
        sigma      = await self._get_live_sigma(symbol, underlying_price, dte, interval, expiry, _atr_sigma)

        # VIX + IV Rank gates — only sell premium when it is worth selling
        from src.market_data.option_chain import vix_allows_selling, iv_rank_allows_selling
        if not vix_allows_selling(vix):
            logger.info(
                f"[CreditSpread] {symbol} skipped — VIX={vix:.1f} too low "
                f"(need ≥12.0 for rich premium). Not worth selling spreads."
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

        # VWAP trend confirmation — session_vwap (added 2026-07-30) is a genuine
        # intraday VWAP built from only today's live-tick bars, matching what the
        # log messages below actually claim ("intraday bearish/bullish momentum").
        # Before this, `vwap` (still available, now used only as a fallback) was a
        # multi-day cumulative figure over ~750 5-min bars — a medium-term anchor,
        # not the intraday one this check was described as. Falls back to the
        # multi-day vwap only in the bootstrap edge case (no live bars yet).
        price_vwap = market_data.get("session_vwap")
        vwap = float(price_vwap if price_vwap else market_data.get("vwap", underlying_price))
        if vwap > 0:
            _vwap_buffer = 0.005  # 0.5% buffer — ignore tiny VWAP deviations
            if spread_type == "BULL_PUT_SPREAD" and underlying_price < vwap * (1 - _vwap_buffer):
                logger.info(
                    f"[CreditSpread] {symbol} BULL_PUT skipped — price Rs{underlying_price:.2f} "
                    f"below VWAP Rs{vwap:.2f} (intraday bearish momentum)"
                )
                return
            if spread_type == "BEAR_CALL_SPREAD" and underlying_price > vwap * (1 + _vwap_buffer):
                logger.info(
                    f"[CreditSpread] {symbol} BEAR_CALL skipped — price Rs{underlying_price:.2f} "
                    f"above VWAP Rs{vwap:.2f} (intraday bullish momentum)"
                )
                return

        # Market breadth filter — align spread direction with broad market sentiment.
        # breadth > 0.65: market advancing → skip BEAR_CALL (would contradict trend)
        # breadth < 0.35: market declining → skip BULL_PUT (would contradict trend)
        _breadth_cs = None
        _redis_cs   = getattr(self, "_redis", None)
        if _redis_cs:
            try:
                _b_raw = await _redis_cs.get("market:breadth")
                if _b_raw:
                    _breadth_cs = json.loads(_b_raw).get("breadth")
            except Exception:
                pass
        if _breadth_cs is not None:
            if spread_type == "BEAR_CALL_SPREAD" and _breadth_cs > 0.65:
                logger.info(
                    f"[CreditSpread] {symbol} BEAR_CALL skipped — "
                    f"market breadth {_breadth_cs:.1%} > 65% (broad market advancing)"
                )
                return
            if spread_type == "BULL_PUT_SPREAD" and _breadth_cs < 0.35:
                logger.info(
                    f"[CreditSpread] {symbol} BULL_PUT skipped — "
                    f"market breadth {_breadth_cs:.1%} < 35% (broad market declining)"
                )
                return

        # ADX filter — credit spreads need a moderate directional trend (ADX 15–30).
        # ADX < 15: no trend, stock is ranging → condor territory not spread territory.
        # ADX > 30: trend too strong → risk of blowthrough on short strike.
        # Fixed 2026-08-06: `if _adx_cs > 0` treated "can't confirm ADX yet"
        # (insufficient history) as "assume we're in the safe [15,30] zone" --
        # block instead, consistent with every other unconfirmed-data case.
        _adx_cs = float(market_data.get("adx14", 0))
        _adx_cs_valid = bool(market_data.get("adx_valid", False))
        if not _adx_cs_valid:
            logger.info(
                f"[CreditSpread] {symbol} skipped — ADX not yet computable "
                "(insufficient history; cannot confirm trend is in the safe zone)"
            )
            return
        if _adx_cs < 15:
            logger.info(
                f"[CreditSpread] {symbol} skipped — ADX={_adx_cs:.1f} < 15 "
                "(no trend; condor regime)"
            )
            return
        if _adx_cs > 30:
            logger.info(
                f"[CreditSpread] {symbol} skipped — ADX={_adx_cs:.1f} > 30 "
                "(trend too strong; blowthrough risk)"
            )
            return

        # Event/earnings calendar filter — block entries within 5 trading days.
        # Earnings and RBI events cause IV crush and gap risk that destroys spread edge.
        from src.market_data.event_calendar import has_event_within_days as _has_event
        if await _has_event(symbol, getattr(self, "_redis", None), days=5):
            logger.info(
                f"[CreditSpread] {symbol} skipped — earnings or NSE event within 5 days"
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
            # Re-validate spread geometry after OI bump
            if spread_type == "BULL_PUT_SPREAD":
                if long_strike >= short_strike:
                    long_strike = short_strike - 2 * interval
            else:  # BEAR_CALL_SPREAD
                if long_strike <= short_strike:
                    long_strike = short_strike + 2 * interval
            long_contract = build_option_symbol(symbol, long_strike, opt, expiry)

        short_p, long_p = await get_entry_prices_for_spread(
            symbol, short_contract, long_contract,
            kite=self._kite, redis=getattr(self, "_redis", None),
            atr=atr, dte=dte,
        )
        net_credit = round(short_p - long_p, 2)
        total_credit = net_credit * lot_size

        # HV/IV ratio filter — only sell premium when implied vol exceeds realized vol by ≥10%.
        # _atr_sigma (ATR-based HV proxy) represents realized/historical volatility.
        # sigma is now live ATM IV used for strike selection; _atr_sigma is the HV baseline here.
        # If the market isn't pricing options richer than what stocks actually move,
        # the edge of premium selling disappears.
        if short_p > 0 and _atr_sigma > 0:
            from src.market_data.option_chain import implied_vol as _iv_fn
            _T = max(dte, 1) / 365.0
            _market_iv = _iv_fn(short_p, underlying_price, short_strike, _T, opt)
            if _market_iv is not None and _market_iv > 0:
                _iv_hv_ratio = _market_iv / _atr_sigma
                if _iv_hv_ratio < 1.1:
                    logger.info(
                        f"[CreditSpread] {symbol} skipped — IV/HV={_iv_hv_ratio:.2f} < 1.10: "
                        f"market IV ({_market_iv:.1%}) not rich enough vs realized HV ({_atr_sigma:.1%})"
                    )
                    return
                logger.debug(
                    f"[CreditSpread] {symbol} IV/HV={_iv_hv_ratio:.2f} "
                    f"(IV={_market_iv:.1%} / HV={_atr_sigma:.1%}) — premium selling edge confirmed"
                )

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

        # Max-loss figure for the per-strategy capital budget check (risk layer 5)
        # — passed explicitly since the short leg below is a SELL (see
        # RiskManager.validate_trade's capital_at_risk docstring for why the
        # default computation can't see it).
        _capital_at_risk = (spread_width - net_credit) * lot_size

        logger.info(
            f"[CreditSpread] {spread_type} {symbol} | SELL {short_contract}@Rs{short_p} "
            f"BUY {long_contract}@Rs{long_p} credit=Rs{net_credit}x{lot_size}=Rs{total_credit:.0f} "
            f"DTE={dte} IV_rank={iv_rank} PCR={oi_data['pcr'] if oi_data else 'N/A'}"
        )

        short_order = await self.order_manager.place_order(
            short_contract, "SELL", lot_size, short_p,
            is_spread_leg=False, strategy_name=strategy.name,
            iv_rank=iv_rank, vix=vix, capital_at_risk=_capital_at_risk,
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
            from src.market_data.option_chain import get_option_quote as _gq
            _unwind_p = await _gq(short_contract, getattr(self, "_kite", None), getattr(self, "_redis", None)) or short_p
            _unwind_order = await self.order_manager.place_order(
                short_contract, "BUY", lot_size, _unwind_p,
                is_spread_leg=True, is_exit_order=True,
            )
            _bad_uw = {"REJECTED", "REJECTED_BY_RISK", "CANCELLED", "FAILED"}
            if _unwind_order is None or getattr(_unwind_order, "order_status", "") in _bad_uw:
                logger.critical(
                    f"[CreditSpread] UNWIND FAILED for {short_contract} — "
                    f"naked short may remain open. MANUAL INTERVENTION REQUIRED."
                )
                await self._notify(
                    f"CRITICAL: CreditSpread unwind FAILED\n"
                    f"Contract: {short_contract}\nSymbol: {symbol}\n"
                    f"Action required: manually close BUY {lot_size} {short_contract}"
                )
                # Do NOT add to _exited_today so operator can investigate
            else:
                self._exited_today.add(symbol)
            return

        self._today_order_count += 2

        journal_id = await self._log_trade_open(
            strategy=strategy.name, underlying=symbol,
            structure_type=spread_type,
            contracts=[short_contract, long_contract],
            entry_price=net_credit, quantity=lot_size,
            market_data=market_data, iv_rank=iv_rank, vix=vix,
        )
        # Track deployed capital by actual max loss (width - net credit), not just
        # the long/hedge leg's premium — found this was previously never called at
        # entry at all (only reconciled once daily at market open from overnight
        # positions — see on_market_open), so the per-strategy budget check
        # (risk_manager.validate_trade layer 5) was silently a no-op for every
        # same-day credit spread entry. Max loss is also a far more accurate risk
        # figure than hedge-leg premium: a wide spread's long leg can cost almost
        # the same regardless of width, while the real capital at risk scales
        # directly with width. Reuses _capital_at_risk computed before the short
        # leg above (same max-loss figure already passed as the risk-layer-5
        # budget check).
        self.risk_manager.add_deployed_capital(strategy.name, _capital_at_risk)
        self._active_spreads[symbol] = {
            "spread_type":    spread_type,
            "short_contract": short_contract, "long_contract":  long_contract,
            "short_strike":   short_strike,   "long_strike":    long_strike,
            "option_type":    opt,
            "short_premium":  short_p,        "long_premium":   long_p,
            "net_credit":     net_credit,     "lot_size":       lot_size,
            "journal_id":     journal_id,
            "strategy_name":  strategy.name,
            "expiry_date":    expiry.isoformat(),
            "entry_date":     now_ist().replace(tzinfo=None).date().isoformat(),
            "entry_vix":      vix or 0.0,    # E: stored for VIX spike threshold adjustment
            "gtt_id":         None,           # I: filled below after GTT placement
        }
        if self._ltp_poller:
            self._ltp_poller.register_option_contracts([short_contract, long_contract])

        # I: place exchange-level GTT backstop on the short leg (live mode only).
        # If server crashes entirely, this fires at Zerodha when price hits 2.5× entry.
        _gtt_id = await self._place_gtt_backstop(short_contract, lot_size, short_p)
        if _gtt_id:
            self._active_spreads[symbol]["gtt_id"] = _gtt_id

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
        profit_closes: List[str] = []
        adverse_closes: List[str] = []

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

            # C: near-expiry restore — force close immediately on first cycle after restart
            if underlying in self._close_on_first_cycle:
                exit_reason = f"Near-expiry forced close (restored expiry={spread.get('expiry_date')})"
            elif dte < min_dte:
                exit_reason = f"DTE={dte} < {min_dte}"

            if exit_reason is None and current_price > 0:
                ss = spread["short_strike"]
                if spread["spread_type"] == "BULL_PUT_SPREAD" and current_price < ss:
                    exit_reason = f"Put breach: {underlying} Rs{current_price:.2f} < short Rs{ss}"
                elif spread["spread_type"] == "BEAR_CALL_SPREAD" and current_price > ss:
                    exit_reason = f"Call breach: {underlying} Rs{current_price:.2f} > short Rs{ss}"

            # E: VIX spike → tighten thresholds before normal manage_position check.
            # If VIX has risen 50%+ from entry, IV expansion works against short options —
            # take 60% profit early and use 1.5× SL instead of waiting for full 2× SL.
            if exit_reason is None:
                _entry_vix = spread.get("entry_vix", 0.0)
                if _entry_vix > 0:
                    _cur_vix = await self._get_cached_vix()
                    if _cur_vix and _cur_vix >= _entry_vix * 1.5:
                        if cur_short >= spread["short_premium"] * 1.5:
                            exit_reason = (
                                f"VIX spike SL (entry {_entry_vix:.1f}→now {_cur_vix:.1f}): "
                                f"short ₹{cur_short:.2f} ≥ 1.5× — exiting early"
                            )
                        elif cur_short <= spread["short_premium"] * 0.40:
                            pnl_pct = (spread["short_premium"] - cur_short) / spread["short_premium"] * 100
                            exit_reason = (
                                f"VIX spike profit (entry {_entry_vix:.1f}→now {_cur_vix:.1f}): "
                                f"60% captured ({pnl_pct:.1f}%) — exiting early"
                            )

            # DTE-tiered profit target — accept less profit as gamma risk rises near expiry.
            # Base tier (DTE > 21) comes from the strategy's own profit_close_pct so tuning
            # that parameter actually changes behavior (it previously didn't — the engine
            # hardcoded 0.25 here, matching iron condor's already-correct pattern below).
            # DTE 15–21: 65% profit; DTE 8–14: 55% profit; DTE ≤ 7 is caught by min_dte above.
            if exit_reason is None:
                _dte_profit_pct = getattr(cs_strategy, "profit_close_pct", 0.25) if cs_strategy else 0.25
                if dte <= 14:
                    _dte_profit_pct = max(_dte_profit_pct, 0.45)
                elif dte <= 21:
                    _dte_profit_pct = max(_dte_profit_pct, 0.35)
                if cur_short <= spread["short_premium"] * _dte_profit_pct:
                    _captured = round((1 - cur_short / spread["short_premium"]) * 100, 1)
                    exit_reason = (
                        f"DTE-tiered profit (DTE={dte}): "
                        f"{_captured}% captured — target {100 - int(_dte_profit_pct * 100)}%"
                    )

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

            # Delta-based exit — short leg delta > 0.40 signals the strike is under threat.
            if exit_reason is None and atr > 0 and current_price > 0:
                try:
                    from src.market_data.option_chain import atr_to_annualised_vol, bs_delta
                    _sig = atr_to_annualised_vol(atr * _5MIN_ATR_SCALE, current_price)
                    if _sig > 0:
                        _T = max(dte, 1) / 365.0
                        _delta = bs_delta(
                            current_price, spread["short_strike"], _T, _sig, spread["option_type"]
                        )
                        if _delta is not None and abs(_delta) > 0.40:
                            exit_reason = (
                                f"Delta breach: short δ={_delta:.2f} (|δ|>0.40, "
                                f"strike {spread['short_strike']} at risk)"
                            )
                except Exception as _de:
                    logger.debug(f"[DeltaExit] {underlying}: delta check error — {_de}")

            if exit_reason is None:
                continue

            lot = spread["lot_size"]
            # is_exit_order=True is required here, not just is_spread_leg=True --
            # risk_manager.validate_trade() only bypasses the kill switch/circuit
            # breaker for is_exit_order (layer 0); is_spread_leg alone still hits
            # that check (intentional for entry hedge legs, wrong for a genuine
            # exit). Without this, a tripped kill switch or daily-loss circuit
            # breaker would block the system from closing an existing credit
            # spread via its normal exit path -- exactly when it most needs to
            # (found 2026-08-06).
            exit_short = await self.order_manager.place_order(spread["short_contract"], "BUY",  lot, cur_short, is_spread_leg=True, is_exit_order=True)
            exit_long  = await self.order_manager.place_order(spread["long_contract"],  "SELL", lot, cur_long,  is_spread_leg=True, is_exit_order=True)

            _bad = {"REJECTED", "REJECTED_BY_RISK", "CANCELLED", "FAILED"}
            if exit_short is None or exit_long is None or \
               getattr(exit_short, "order_status", "") in _bad or \
               getattr(exit_long,  "order_status", "") in _bad:
                logger.warning(
                    f"[CreditSpread] Exit orders for {underlying} partially rejected — "
                    f"keeping position in tracking to retry next cycle."
                )
                continue

            net_pnl = (
                (spread["short_premium"] - cur_short)
                - (spread["long_premium"]  - cur_long)
            ) * lot

            # Must match the max-loss figure add_deployed_capital() used at entry
            # (see _process_credit_spread) — not just the long leg's premium.
            _spread_width = abs(spread["short_strike"] - spread["long_strike"])
            self.risk_manager.release_deployed_capital(
                spread.get("strategy_name", "credit_spread_v1"),
                (_spread_width - spread["net_credit"]) * lot,
            )

            # Fixed 2026-08-06: was "avg_price" (doesn't exist on the Order
            # model -- see database/models/order.py, the real column is
            # fill_price), so _slippage was always silently 0 here. net_pnl
            # above is deliberately quote-based, not fill-based, so this
            # fix only corrects the total_slippage_pts diagnostic field
            # below, not the recorded pnl itself.
            _short_fill = getattr(exit_short, "fill_price", None) or cur_short
            _long_fill  = getattr(exit_long,  "fill_price", None) or cur_long
            _slippage   = abs(_short_fill - cur_short) + abs(_long_fill - cur_long)
            await self._log_trade_close(
                journal_id=spread.get("journal_id"),
                exit_price=round(cur_short - cur_long, 2),
                pnl=net_pnl, exit_reason=exit_reason,
                market_data=market_data,
                total_slippage_pts=round(_slippage, 4) if _slippage > 0 else None,
            )
            if self._ltp_poller:
                self._ltp_poller.unregister_option_contracts(
                    [spread["short_contract"], spread["long_contract"]]
                )
            await self._notify(
                f"CREDIT SPREAD CLOSED\n"
                f"Underlying: {underlying}\nReason: {exit_reason}\n"
                f"Short: sold Rs{spread['short_premium']:.2f}, closed Rs{cur_short:.2f}\n"
                f"Long:  paid Rs{spread['long_premium']:.2f}, sold Rs{cur_long:.2f}\n"
                f"Net PnL: Rs{net_pnl:,.2f}"
            )

            # D: increment SL frequency counter for adverse exits (circuit breaker)
            _is_adverse = any(
                kw in exit_reason for kw in ("breach", "Breach", "SL:", " SL ", "spike SL")
            )
            if _is_adverse:
                _r = getattr(self, "_redis", None)
                if _r:
                    _sl_key   = f"sl_freq:{underlying}"
                    _sl_count = int(await _r.incr(_sl_key))
                    if _sl_count == 1:
                        await _r.expire(_sl_key, 5 * 86400)
                    logger.info(
                        f"[CircuitBreaker] {underlying}: adverse exit #{_sl_count} "
                        f"in 5-day window"
                    )

            # I: cancel GTT backstop now that position is closed normally
            await self._cancel_gtt(spread.get("gtt_id"), spread.get("short_contract", ""))

            # C: remove from near-expiry set
            self._close_on_first_cycle.discard(underlying)

            # Route to profit or adverse bucket for re-entry eligibility — by realized
            # P&L, not by scanning exit_reason text. A plain DTE-floor timeout ("DTE=6
            # < 7") matches no adverse keyword even when the position lost money, which
            # previously let losing trades qualify for the lenient same-day re-entry
            # floor meant for genuine wins.
            if net_pnl < 0:
                adverse_closes.append(underlying)
            else:
                profit_closes.append(underlying)

        for sym in adverse_closes:
            del self._active_spreads[sym]
            self._exited_today.add(sym)
        for sym in profit_closes:
            del self._active_spreads[sym]
            self._profit_closed_today.add(sym)   # allow same-day re-entry with lower DTE floor
            logger.info(f"[CreditSpread] {sym} → profit_closed_today (re-entry eligible at DTE≥14)")
        if adverse_closes or profit_closes:
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
        if symbol in self._active_spreads:
            return  # don't stack condor on top of existing spread for same underlying
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
        now      = now_ist()
        dte      = (expiry - now.replace(tzinfo=None)).days
        min_dte  = getattr(strategy, "min_dte", 7)
        if dte < min_dte:
            logger.info(
                f"[IronCondor] {symbol} skipped — DTE={dte} < min_dte={min_dte}, too close to expiry"
            )
            return

        # Same DTE floor logic as credit spreads:
        #   Fresh entries need DTE ≥ 21. Re-entries after same-day profit close need DTE ≥ 14.
        #   If the near-month contract is short on runway for a fresh entry, roll straight to
        #   next month rather than going dark until the 7-day exit rollover kicks in.
        _ENTRY_MIN_DTE   = 21
        _REENTRY_MIN_DTE = 14
        if symbol in self._profit_closed_today:
            if dte < _REENTRY_MIN_DTE:
                logger.info(
                    f"[IronCondor] {symbol} skipped re-entry — DTE={dte} < {_REENTRY_MIN_DTE} "
                    "re-entry floor (profit closed today)."
                )
                return
            logger.info(
                f"[IronCondor] {symbol} re-entering after same-day profit close (DTE={dte})"
            )
        elif dte < _ENTRY_MIN_DTE:
            expiry = get_entry_expiry(_ENTRY_MIN_DTE)
            dte    = (expiry - now.replace(tzinfo=None)).days
            logger.info(
                f"[IronCondor] {symbol} near-month DTE < {_ENTRY_MIN_DTE} — "
                f"rolling fresh entry to next month's expiry (DTE={dte})"
            )

        # No new entries after 14:30 IST — at least one exit-check cycle before
        # the 15:20 EOD window, and avoids rushed afternoon entries.
        _ENTRY_CUTOFF_HOUR, _ENTRY_CUTOFF_MIN = 14, 30
        if (now.replace(tzinfo=None).hour, now.replace(tzinfo=None).minute) >= (_ENTRY_CUTOFF_HOUR, _ENTRY_CUTOFF_MIN):
            logger.debug(f"[IronCondor] {symbol} skipped — past entry cutoff 14:30 IST.")
            return

        # D: SL frequency circuit breaker
        _redis_cb_ic = getattr(self, "_redis", None)
        if _redis_cb_ic:
            _sl_freq_ic = await _redis_cb_ic.get(f"sl_freq:{symbol}")
            if _sl_freq_ic and int(_sl_freq_ic) >= 2:
                logger.info(
                    f"[IronCondor] {symbol} blocked — {_sl_freq_ic} adverse SL exit(s) "
                    "in last 5 days (circuit breaker). Will unblock automatically."
                )
                return

        # H: Sector concentration check
        _sym_sector_ic = FNO_SECTORS.get(symbol)
        if _sym_sector_ic:
            _sector_count_ic = sum(
                1 for s in list(self._active_spreads) + list(self._active_condors)
                if FNO_SECTORS.get(s) == _sym_sector_ic
            )
            if _sector_count_ic >= MAX_SECTOR_POSITIONS:
                logger.info(
                    f"[IronCondor] {symbol} ({_sym_sector_ic}) skipped — "
                    f"{_sector_count_ic}/{MAX_SECTOR_POSITIONS} positions already open in sector."
                )
                return

        lot_size   = await self._get_lot_size(symbol)
        atr        = float(market_data.get("atr14", underlying_price * 0.01))
        iv_rank    = await self._get_iv_rank(symbol, underlying_price, atr, dte)
        _atr_sigma = atr_to_annualised_vol(atr * _5MIN_ATR_SCALE, underlying_price)
        sigma      = await self._get_live_sigma(symbol, underlying_price, dte, interval, expiry, _atr_sigma)

        # VIX + IV Rank gates — only sell premium when it is worth selling
        from src.market_data.option_chain import vix_allows_selling, iv_rank_allows_selling
        if not vix_allows_selling(vix):
            logger.info(
                f"[IronCondor] {symbol} skipped — VIX={vix:.1f} too low "
                f"(need ≥12.0 for rich premium). Not worth selling condors."
            )
            return
        if not iv_rank_allows_selling(iv_rank):
            logger.info(
                f"[IronCondor] {symbol} skipped — IV Rank={iv_rank:.2f} too low "
                f"(need ≥0.30). Premium too cheap."
            )
            return

        # OI/PCR neutrality check — iron condors need a non-directional market
        from src.market_data.nse_oi import get_oi_data, is_strike_crowded
        redis = getattr(self, "_redis", None)
        oi_data = await get_oi_data(symbol, redis) if redis else None
        pcr = market_data.get("pcr") or (oi_data.get("pcr") if oi_data else None)
        if pcr is not None and (pcr < 0.7 or pcr > 1.4):
            logger.info(
                f"[IronCondor] {symbol} skipped — PCR={pcr:.2f} is extreme "
                f"(need 0.7–1.4 for neutral condor). Market too directional."
            )
            return

        # Market breadth filter — iron condors need a neutral market.
        # When breadth is extreme (very bullish or very bearish), range-bound
        # structures are at risk from a one-sided move.
        _breadth_ic = None
        _redis_ic2  = getattr(self, "_redis", None)
        if _redis_ic2:
            try:
                _b_raw_ic = await _redis_ic2.get("market:breadth")
                if _b_raw_ic:
                    _breadth_ic = json.loads(_b_raw_ic).get("breadth")
            except Exception:
                pass
        if _breadth_ic is not None and (_breadth_ic < 0.35 or _breadth_ic > 0.65):
            logger.info(
                f"[IronCondor] {symbol} skipped — market breadth {_breadth_ic:.1%} "
                "outside neutral zone 35–65% (market too directional for condor)"
            )
            return

        # ADX filter — iron condors require low trend strength (ADX < 20).
        # ADX ≥ 20 indicates a developing trend that could breach either wing.
        # Fixed 2026-08-06: `if _adx_ic > 0` treated "can't confirm ADX yet"
        # as "assume the market is calm enough" -- block instead, consistent
        # with every other unconfirmed-data case.
        _adx_ic = float(market_data.get("adx14", 0))
        _adx_ic_valid = bool(market_data.get("adx_valid", False))
        if not _adx_ic_valid:
            logger.info(
                f"[IronCondor] {symbol} skipped — ADX not yet computable "
                "(insufficient history; cannot confirm market is range-bound)"
            )
            return
        if _adx_ic >= 20:
            logger.info(
                f"[IronCondor] {symbol} skipped — ADX={_adx_ic:.1f} >= 20 "
                "(market trending; range-bound thesis invalid)"
            )
            return

        # Event/earnings calendar filter — block entries within 5 trading days.
        from src.market_data.event_calendar import has_event_within_days as _has_event_ic
        if await _has_event_ic(symbol, getattr(self, "_redis", None), days=5):
            logger.info(
                f"[IronCondor] {symbol} skipped — earnings or NSE event within 5 days"
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

        # Crowded-strike avoidance for both short legs
        if is_strike_crowded(put_short_strike, oi_data, "PE"):
            logger.info(
                f"[IronCondor] {symbol} put short strike {put_short_strike} is crowded OI — "
                f"moving 1 interval further OTM"
            )
            put_short_strike -= interval
            if put_long_strike >= put_short_strike:
                put_long_strike = put_short_strike - 2 * interval
        if is_strike_crowded(call_short_strike, oi_data, "CE"):
            logger.info(
                f"[IronCondor] {symbol} call short strike {call_short_strike} is crowded OI — "
                f"moving 1 interval further OTM"
            )
            call_short_strike += interval
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

        # Fetch real Kite LTPs for all 4 legs — same as credit spread entry.
        # ATR estimates are CE/PE-blind (put_short_p == call_short_p always), which
        # causes immediate SL fires when the exit check sees real market prices.
        from src.market_data.option_chain import get_entry_prices_for_spread
        _kite  = getattr(self, "_kite",  None)
        _redis = getattr(self, "_redis", None)
        put_short_p, put_long_p = await get_entry_prices_for_spread(
            symbol, psc, plc, _kite, _redis, atr, dte,
            short_otm_intervals=1, long_otm_intervals=3,
        )
        call_short_p, call_long_p = await get_entry_prices_for_spread(
            symbol, csc, clc, _kite, _redis, atr, dte,
            short_otm_intervals=1, long_otm_intervals=3,
        )
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
        wing_spread     = max(abs(put_short_strike - put_long_strike), abs(call_short_strike - call_long_strike))
        required_margin = wing_spread * lot_size
        if not await self._check_available_margin(required_margin):
            logger.warning(
                f"[IronCondor] {symbol} skipped — insufficient margin "
                f"(need ~Rs{required_margin:,.0f})"
            )
            return

        # Max-loss figure for the per-strategy capital budget check (risk layer 5)
        # — passed explicitly on the first (SELL) leg only, same rationale as
        # _process_credit_spread's capital_at_risk.
        _capital_at_risk = (wing_spread - net_credit) * lot_size

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
                kwargs["capital_at_risk"] = _capital_at_risk
            order = await self.order_manager.place_order(contract, side, lot_size, price, **kwargs)
            if not order or order.order_status != "OPEN":
                logger.error(f"[IronCondor] Leg failed: {side} {contract}. Unwinding {len(placed)} leg(s).")
                if order and order.order_status == "REJECTED_BY_RISK":
                    self._exited_today.add(symbol)  # risk manager blocked it — stop retrying today
                from src.market_data.option_chain import get_option_quote as _gq
                _bad_uw = {"REJECTED", "REJECTED_BY_RISK", "CANCELLED", "FAILED"}
                _failed_unwinds = []
                for (c, s, p, _) in placed:
                    rev = "BUY" if s == "SELL" else "SELL"
                    _unwind_p = await _gq(c, getattr(self, "_kite", None), getattr(self, "_redis", None)) or p
                    _uw = await self.order_manager.place_order(
                        c, rev, lot_size, _unwind_p, is_spread_leg=True, is_exit_order=True,
                    )
                    if _uw is None or getattr(_uw, "order_status", "") in _bad_uw:
                        _failed_unwinds.append(f"{rev} {c}")
                if _failed_unwinds:
                    logger.critical(
                        f"[IronCondor] UNWIND FAILED for {symbol} legs: {_failed_unwinds} — "
                        f"naked position(s) may remain open. MANUAL INTERVENTION REQUIRED."
                    )
                    await self._notify(
                        f"CRITICAL: IronCondor unwind FAILED\n"
                        f"Symbol: {symbol}\nFailed legs: {', '.join(_failed_unwinds)}\n"
                        f"Action required: manually close the listed contracts"
                    )
                return
            placed.append((contract, side, price, is_leg))

        self._today_order_count += 4
        journal_id  = await self._log_trade_open(
            strategy=strategy.name, underlying=symbol,
            structure_type="IRON_CONDOR", contracts=[psc, plc, csc, clc],
            entry_price=net_credit, quantity=lot_size,
            market_data=market_data, iv_rank=iv_rank, vix=vix,
        )
        # Track deployed capital by actual max loss, not just the long/hedge legs'
        # premium — same fix and rationale as _process_credit_spread. Max loss for
        # an iron condor is bounded by whichever wing is wider (price can't be
        # simultaneously above the call strikes and below the put strikes, so only
        # one wing is ever actually breached), minus total net credit collected.
        # Reuses _capital_at_risk computed before the legs loop above (same
        # max-loss figure already passed as the risk-layer-5 budget check).
        self.risk_manager.add_deployed_capital(strategy.name, _capital_at_risk)
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
            "expiry_date":         expiry.isoformat(),
            "entry_date":          now_ist().replace(tzinfo=None).date().isoformat(),
            "entry_vix":           vix or 0.0,  # E: stored for VIX spike threshold adjustment
            "put_short_gtt_id":    None,         # I: filled below
            "call_short_gtt_id":   None,         # I: filled below
        }
        if self._ltp_poller:
            self._ltp_poller.register_option_contracts([psc, plc, csc, clc])

        # I: GTT backstops on both short legs (live mode only)
        _put_gtt  = await self._place_gtt_backstop(psc, lot_size, put_short_p)
        _call_gtt = await self._place_gtt_backstop(csc, lot_size, call_short_p)
        if _put_gtt:
            self._active_condors[symbol]["put_short_gtt_id"]  = _put_gtt
        if _call_gtt:
            self._active_condors[symbol]["call_short_gtt_id"] = _call_gtt

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
        to_close_adverse_c: List[str] = []
        to_close_profit_c:  List[str] = []

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

            # E: VIX spike — tighten condor thresholds dynamically.
            # If VIX has risen 50%+ from entry, IV expansion inflates option prices
            # and the range-bound thesis is weakening. Exit earlier.
            _entry_vix_c = c.get("entry_vix", 0.0)
            if _entry_vix_c > 0:
                _cur_vix_c = await self._get_cached_vix()
                if _cur_vix_c and _cur_vix_c >= _entry_vix_c * 1.5:
                    profit_pct = max(profit_pct, 0.40)   # take 60% profit early
                    sl_mult    = min(sl_mult,    1.5)     # tighter SL during IV expansion
                    logger.debug(
                        f"[IronCondor] {underlying}: VIX {_entry_vix_c:.1f}→{_cur_vix_c:.1f} "
                        f"(+{(_cur_vix_c/_entry_vix_c - 1)*100:.0f}%) — thresholds tightened"
                    )

            # DTE-tiered profit target — lower the hurdle as expiry approaches.
            # DTE > 21: 75% profit (0.25); DTE 15–21: 65% (0.35); DTE ≤ 14: 55% (0.45).
            if dte <= 14:
                profit_pct = max(profit_pct, 0.45)
            elif dte <= 21:
                profit_pct = max(profit_pct, 0.35)

            exit_reason: Optional[str] = None

            # C: near-expiry restore — force close on first cycle after restart
            if underlying in self._close_on_first_cycle:
                exit_reason = f"Near-expiry forced close (restored expiry={c.get('expiry_date')})"
            elif dte < min_dte:
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
                _put_hit  = cur_ps <= c["put_short_premium"]  * profit_pct
                _call_hit = cur_cs <= c["call_short_premium"] * profit_pct
                if _put_hit or _call_hit:
                    # Close entire condor when EITHER short decays to target.
                    # If one wing is at 75% profit the stock has moved toward that wing's
                    # short strike — keeping the trade open exposes the OTHER wing to breach.
                    _which = "put" if _put_hit else "call"
                    profit_label = "75%+" if profit_pct <= 0.25 else "60%+ (VIX spike early exit)"
                    exit_reason = f"{_which.capitalize()} wing at {profit_label} profit — closing condor"

            # G: Regime shift post-entry — iron condors need RANGE_BOUND/LOW_VOL.
            # If regime has shifted to TRENDING or VOLATILE after entry, the neutrality
            # thesis is broken. Exit after holding at least 1 day (avoids same-day noise).
            if exit_reason is None:
                _r = getattr(self, "_redis", None)
                if _r:
                    try:
                        _regime_raw = await _r.get("market:regime")
                        if _regime_raw:
                            _regime = json.loads(_regime_raw).get("regime", "")
                            if _regime in ("TRENDING", "VOLATILE"):
                                _entry_date = c.get("entry_date", "")
                                _today_str  = now_ist().replace(tzinfo=None).date().isoformat()
                                if _entry_date and _entry_date < _today_str:
                                    exit_reason = (
                                        f"Regime shift: {_regime} post-entry — "
                                        "condor range assumption broken, exiting"
                                    )
                    except Exception:
                        pass

            # Delta-based exit — if either short leg delta exceeds 0.40, that wing is in danger.
            if exit_reason is None and atr > 0 and current_price > 0:
                try:
                    from src.market_data.option_chain import atr_to_annualised_vol, bs_delta
                    _sig_c = atr_to_annualised_vol(atr * _5MIN_ATR_SCALE, current_price)
                    if _sig_c > 0:
                        _T_c   = max(dte, 1) / 365.0
                        _ps_d  = bs_delta(current_price, c["put_short_strike"],  _T_c, _sig_c, "PE")
                        _cs_d  = bs_delta(current_price, c["call_short_strike"], _T_c, _sig_c, "CE")
                        if _ps_d is not None and abs(_ps_d) > 0.40:
                            exit_reason = (
                                f"Delta breach: put short δ={_ps_d:.2f} (|δ|>0.40, "
                                f"put strike {c['put_short_strike']} at risk)"
                            )
                        elif _cs_d is not None and abs(_cs_d) > 0.40:
                            exit_reason = (
                                f"Delta breach: call short δ={_cs_d:.2f} (|δ|>0.40, "
                                f"call strike {c['call_short_strike']} at risk)"
                            )
                except Exception as _de:
                    logger.debug(f"[DeltaExit] {underlying}: condor delta check error — {_de}")

            if exit_reason is None:
                continue

            lot = c["lot_size"]
            # is_exit_order=True required -- same fix and rationale as
            # _check_spread_exits (found 2026-08-06): is_spread_leg alone still
            # hits the kill-switch/circuit-breaker check in
            # risk_manager.validate_trade(), which would block closing an
            # existing iron condor exactly when a tripped kill switch makes
            # that most urgent.
            exit_ps = await self.order_manager.place_order(c["put_short_contract"],  "BUY",  lot, cur_ps, is_spread_leg=True, is_exit_order=True)
            exit_pl = await self.order_manager.place_order(c["put_long_contract"],   "SELL", lot, cur_pl, is_spread_leg=True, is_exit_order=True)
            exit_cs = await self.order_manager.place_order(c["call_short_contract"], "BUY",  lot, cur_cs, is_spread_leg=True, is_exit_order=True)
            exit_cl = await self.order_manager.place_order(c["call_long_contract"],  "SELL", lot, cur_cl, is_spread_leg=True, is_exit_order=True)

            _bad = {"REJECTED", "REJECTED_BY_RISK", "CANCELLED", "FAILED"}
            if any(
                o is None or getattr(o, "order_status", "") in _bad
                for o in (exit_ps, exit_pl, exit_cs, exit_cl)
            ):
                logger.warning(
                    f"[IronCondor] Exit orders for {underlying} partially rejected — "
                    f"keeping position in tracking to retry next cycle."
                )
                continue

            net_pnl = (
                (c["put_short_premium"]  - cur_ps)
                + (c["call_short_premium"] - cur_cs)
                - (c["put_long_premium"]   - cur_pl)
                - (c["call_long_premium"]  - cur_cl)
            ) * lot

            # Must match the max-loss figure add_deployed_capital() used at entry
            # (see _process_iron_condor) — not just the long legs' premium.
            _put_wing  = abs(c["put_short_strike"]  - c["put_long_strike"])
            _call_wing = abs(c["call_short_strike"] - c["call_long_strike"])
            self.risk_manager.release_deployed_capital(
                c.get("strategy_name", "iron_condor_v1"),
                (max(_put_wing, _call_wing) - c["net_credit"]) * lot,
            )

            # Fixed 2026-08-06: same "avg_price" -> "fill_price" fix as the
            # credit spread exit above -- net_pnl is deliberately quote-based
            # here too, so this only corrects the total_slippage_pts
            # diagnostic (previously always silently 0).
            _ps_fill = getattr(exit_ps, "fill_price", None) or cur_ps
            _pl_fill = getattr(exit_pl, "fill_price", None) or cur_pl
            _cs_fill = getattr(exit_cs, "fill_price", None) or cur_cs
            _cl_fill = getattr(exit_cl, "fill_price", None) or cur_cl
            _slippage = (abs(_ps_fill - cur_ps) + abs(_pl_fill - cur_pl)
                         + abs(_cs_fill - cur_cs) + abs(_cl_fill - cur_cl))
            await self._log_trade_close(
                journal_id=c.get("journal_id"),
                exit_price=round(cur_ps + cur_cs - cur_pl - cur_cl, 2),
                pnl=net_pnl, exit_reason=exit_reason,
                market_data=market_data,
                total_slippage_pts=round(_slippage, 4) if _slippage > 0 else None,
            )
            if self._ltp_poller:
                self._ltp_poller.unregister_option_contracts([
                    c["put_short_contract"], c["put_long_contract"],
                    c["call_short_contract"], c["call_long_contract"],
                ])
            await self._notify(
                f"IRON CONDOR CLOSED\n"
                f"Underlying: {underlying}\nReason: {exit_reason}\n"
                f"Put short:  Rs{c['put_short_premium']:.2f} -> Rs{cur_ps:.2f}\n"
                f"Call short: Rs{c['call_short_premium']:.2f} -> Rs{cur_cs:.2f}\n"
                f"Net PnL: Rs{net_pnl:,.2f}"
            )

            # D: increment SL frequency counter for adverse exits (circuit breaker)
            _is_adverse_c = any(
                kw in exit_reason for kw in ("breach", "Breach", "SL:", " SL ", "spike SL")
            )
            if _is_adverse_c:
                _r_c = getattr(self, "_redis", None)
                if _r_c:
                    _sl_key_c   = f"sl_freq:{underlying}"
                    _sl_count_c = int(await _r_c.incr(_sl_key_c))
                    if _sl_count_c == 1:
                        await _r_c.expire(_sl_key_c, 5 * 86400)
                    logger.info(
                        f"[CircuitBreaker] {underlying}: adverse exit #{_sl_count_c} "
                        f"in 5-day window"
                    )

            # I: cancel GTT backstops on both short legs
            await self._cancel_gtt(c.get("put_short_gtt_id"),  c.get("put_short_contract", ""))
            await self._cancel_gtt(c.get("call_short_gtt_id"), c.get("call_short_contract", ""))

            # C: remove from near-expiry set
            self._close_on_first_cycle.discard(underlying)

            # Route to profit or adverse bucket for re-entry eligibility — by realized
            # P&L (see matching comment in _check_spread_exits for why text-matching
            # the exit reason was wrong).
            if net_pnl < 0:
                to_close_adverse_c.append(underlying)
            else:
                to_close_profit_c.append(underlying)

        for sym in to_close_adverse_c:
            del self._active_condors[sym]
            self._exited_today.add(sym)
        for sym in to_close_profit_c:
            del self._active_condors[sym]
            self._profit_closed_today.add(sym)
            logger.info(f"[IronCondor] {sym} → profit_closed_today (re-entry eligible at DTE≥14)")
        if to_close_adverse_c or to_close_profit_c:
            await self._persist_state()

    async def _exit_all_options_for(self, underlying: str) -> None:
        """
        Closes every open single-leg position on `underlying`. Reachable via
        _process_signal() when a strategy's generate_signal() returns
        SignalType.EXIT -- currently dead code in practice: none of the 4
        active strategies' generate_signal() methods return "EXIT" (only
        their manage_position() methods do, which route through the
        already-correct _check_open_option_exits() path instead). Kept
        correct anyway since SignalType.EXIT is part of the formal signal
        contract (src/core/enums.py) -- a future strategy using it from
        generate_signal() would otherwise silently inherit two bugs found
        2026-08-07: pnl computed from the pre-slippage quote/estimate
        instead of the real fill (same class as three other exit paths
        fixed earlier that day), AND no trade_journal write at all, which
        is worse -- a position closed this way would show as permanently
        open (exit_time/exit_price/pnl all NULL) forever, not just
        mispriced.
        """
        from src.market_data.option_chain import get_option_quote
        positions = await self._safe_get_positions()
        expiry = get_near_month_expiry()
        dte    = (expiry - now_ist().replace(tzinfo=None)).days
        for pos in positions:
            if pos.get("quantity", 0) <= 0 or not pos.get("symbol", "").startswith(underlying):
                continue
            contract = pos["symbol"]
            entry_p = float(pos.get("avg_price") or 0)
            md = await self._get_market_data(underlying)
            atr = float(md.get("atr14", 0)) if md else 0
            _live_p = await get_option_quote(contract, getattr(self, "_kite", None), getattr(self, "_redis", None))
            if _live_p and _live_p > 0:
                exit_p = _live_p
            elif atr > 0:
                exit_p = estimate_option_premium(atr, dte)
            else:
                exit_p = entry_p
            # Peeked (not popped) before the order call: a CLOSING order's
            # product type must match the ORIGINAL position's at Zerodha
            # (see ZerodhaBroker._product_for()).
            _ex_owner_strategy = self._single_leg_journals.get(contract, {}).get("strategy_name")
            _ex_order = await self.order_manager.place_order(
                contract, "SELL", abs(pos["quantity"]), exit_p,
                is_exit_order=True, strategy_name=_ex_owner_strategy,
            )
            self._peak_premiums.pop(contract, None)
            _ex_fill_p = getattr(_ex_order, "fill_price", None) or exit_p

            _jrnl_info = self._single_leg_journals.pop(contract, None)
            if _jrnl_info:
                _pnl = round((_ex_fill_p - entry_p) * abs(pos["quantity"]), 2)
                await self._log_trade_close(
                    journal_id=_jrnl_info.get("journal_id"),
                    exit_price=_ex_fill_p,
                    pnl=_pnl,
                    exit_reason=f"{_jrnl_info.get('strategy_name', 'unknown')} EXIT signal",
                    market_data=md,
                )
                self.risk_manager.release_deployed_capital(
                    _jrnl_info.get("strategy_name", "ema_crossover_v1"), entry_p * abs(pos["quantity"]),
                )
        if underlying in self._active_spreads:
            del self._active_spreads[underlying]
        if underlying in self._active_condors:
            del self._active_condors[underlying]
        self._exited_today.add(underlying)
        await self._persist_state()

    async def _square_off_all(self) -> None:
        """
        EOD square-off at 15:20 IST.

        Strategy:
        - EMA crossover single-leg long options: ALWAYS close (overnight gap risk).
        - Credit spreads and iron condors: hold overnight unless DTE ≤ 1 (expiry day).
          Theta decay is non-linear — the DTE 20→7 window is where we collect premium.
          Closing nightly would destroy the entire edge of the strategy.
        - Expiry day (DTE ≤ 1): force-close everything to avoid assignment/gamma risk.
        """
        from src.market_data.option_chain import get_option_quote

        expiry      = get_near_month_expiry()
        dte         = (expiry - now_ist().replace(tzinfo=None)).days
        is_expiry   = dte <= 1
        kite        = getattr(self, "_kite", None)
        redis       = getattr(self, "_redis", None)

        # Build set of contracts belonging to active multi-leg positions
        spread_condor_contracts: set = set()
        if not is_expiry:
            for s in self._active_spreads.values():
                for key in ("short_contract", "long_contract"):
                    if s.get(key):
                        spread_condor_contracts.add(s[key])
            for c in self._active_condors.values():
                for key in ("put_short_contract", "put_long_contract",
                            "call_short_contract", "call_long_contract"):
                    if c.get(key):
                        spread_condor_contracts.add(c[key])

        positions = await self._safe_get_positions()
        closed_ema = 0
        closed_expiry = 0
        _exit_prices: Dict[str, float] = {}   # contract -> exit price, for expiry-day journal below

        for pos in positions:
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            contract = pos["symbol"]

            # On normal days, skip spread/condor legs — they hold overnight
            if not is_expiry and contract in spread_condor_contracts:
                continue

            side    = "SELL" if qty > 0 else "BUY"
            entry_p = float(pos.get("avg_price") or 0)
            exit_p  = entry_p

            # Try live price first
            live_p = await get_option_quote(contract, kite, redis)
            if live_p and live_p > 0:
                exit_p = live_p
            else:
                underlying = self._get_underlying_from_contract(contract)
                if underlying:
                    md = await self._get_market_data(underlying)
                    if md:
                        atr = float(md.get("atr14", 0))
                        if atr > 0:
                            exit_p = estimate_option_premium(atr, max(dte, 1))

            # Peeked (not popped) before the order call: a CLOSING order's
            # product type must match the ORIGINAL position's at Zerodha
            # (see ZerodhaBroker._product_for()). Empty for spread/condor
            # legs on expiry day (not in _single_leg_journals -- that dict
            # is EMA/momentum-only), which correctly resolves to the
            # default NRML they were actually entered under.
            _sq_owner_strategy = self._single_leg_journals.get(contract, {}).get("strategy_name")
            _sq_order = await self.order_manager.place_order(
                contract, side, abs(qty), exit_p, is_exit_order=True, strategy_name=_sq_owner_strategy,
            )
            self._peak_premiums.pop(contract, None)
            # Fixed 2026-08-07: same fill_price bug as _execute_single_leg_exit
            # / credit-spread / iron-condor exits (fixed earlier today) --
            # this fourth exit path also computed pnl from the pre-slippage
            # quote/estimate (exit_p) and didn't even capture place_order()'s
            # return value, discarding the real PaperBroker fill entirely.
            # EOD square-off runs every trading day (single-leg positions
            # "ALWAYS close" per this method's own docstring), so this was a
            # frequently-triggered instance of the exact same inaccuracy.
            _sq_fill_p = getattr(_sq_order, "fill_price", None) or exit_p
            _exit_prices[contract] = _sq_fill_p

            # Write exit to trade journal so EOD/expiry closes appear in PnL analytics
            _jrnl_info = self._single_leg_journals.pop(contract, None)
            if _jrnl_info:
                _entry_p = float(pos.get("avg_price") or 0)
                _signed  = 1 if qty > 0 else -1
                _pnl     = round((_sq_fill_p - _entry_p) * abs(qty) * _signed, 2)
                _reason  = "Expiry day force-close" if is_expiry else "EOD square-off"
                await self._log_trade_close(
                    journal_id=_jrnl_info.get("journal_id"),
                    exit_price=_sq_fill_p,
                    pnl=_pnl,
                    exit_reason=_reason,
                )
                self.risk_manager.release_deployed_capital(
                    _jrnl_info.get("strategy_name", "ema_crossover_v1"),
                    _entry_p * abs(qty),
                )

            if is_expiry:
                closed_expiry += 1
            else:
                closed_ema += 1

        if is_expiry:
            # Cancel GTT backstops before clearing so exchange-level orders don't
            # fire after our positions are already closed by the square-off above.
            for _s in self._active_spreads.values():
                await self._cancel_gtt(_s.get("gtt_id"), _s.get("short_contract", ""))
            for _c in self._active_condors.values():
                await self._cancel_gtt(_c.get("put_short_gtt_id"),  _c.get("put_short_contract",  ""))
                await self._cancel_gtt(_c.get("call_short_gtt_id"), _c.get("call_short_contract", ""))

            # Log trade_journal close for spread/condor structures force-closed above.
            # Their legs aren't in _single_leg_journals (that dict is EMA-only), so the
            # per-leg loop above can't log them — without this, these trades would keep
            # exit_time/pnl = NULL forever despite being genuinely closed by real orders.
            for _s in self._active_spreads.values():
                _short_x = _exit_prices.get(_s.get("short_contract", ""), _s.get("short_premium", 0))
                _long_x  = _exit_prices.get(_s.get("long_contract", ""),  _s.get("long_premium", 0))
                _lot     = _s.get("lot_size", 0)
                _net     = ((_s.get("short_premium", 0) - _short_x) - (_s.get("long_premium", 0) - _long_x)) * _lot
                _width   = abs(_s.get("short_strike", 0) - _s.get("long_strike", 0))
                self.risk_manager.release_deployed_capital(
                    _s.get("strategy_name", "credit_spread_v1"),
                    (_width - _s.get("net_credit", 0)) * _lot,
                )
                await self._log_trade_close(
                    journal_id=_s.get("journal_id"),
                    exit_price=round(_short_x - _long_x, 2),
                    pnl=round(_net, 2),
                    exit_reason=f"Expiry day force-close (DTE={dte})",
                )
            for _c in self._active_condors.values():
                _ps_x = _exit_prices.get(_c.get("put_short_contract", ""),  _c.get("put_short_premium", 0))
                _pl_x = _exit_prices.get(_c.get("put_long_contract", ""),   _c.get("put_long_premium", 0))
                _cs_x = _exit_prices.get(_c.get("call_short_contract", ""), _c.get("call_short_premium", 0))
                _cl_x = _exit_prices.get(_c.get("call_long_contract", ""),  _c.get("call_long_premium", 0))
                _lot_c = _c.get("lot_size", 0)
                _net_c = (
                    (_c.get("put_short_premium", 0)  - _ps_x)
                    + (_c.get("call_short_premium", 0) - _cs_x)
                    - (_c.get("put_long_premium", 0)   - _pl_x)
                    - (_c.get("call_long_premium", 0)  - _cl_x)
                ) * _lot_c
                _put_wing_x  = abs(_c.get("put_short_strike", 0)  - _c.get("put_long_strike", 0))
                _call_wing_x = abs(_c.get("call_short_strike", 0) - _c.get("call_long_strike", 0))
                self.risk_manager.release_deployed_capital(
                    _c.get("strategy_name", "iron_condor_v1"),
                    (max(_put_wing_x, _call_wing_x) - _c.get("net_credit", 0)) * _lot_c,
                )
                await self._log_trade_close(
                    journal_id=_c.get("journal_id"),
                    exit_price=round(_ps_x + _cs_x - _pl_x - _cl_x, 2),
                    pnl=round(_net_c, 2),
                    exit_reason=f"Expiry day force-close (DTE={dte})",
                )

            # Full expiry-day clear — all positions force-closed
            self._active_spreads.clear()
            self._active_condors.clear()
            await self._persist_state()
            if closed_expiry and not self._eod_notified_today:
                self._eod_notified_today = True
                await self._notify(
                    f"EXPIRY SQUARE-OFF (DTE={dte})\n"
                    f"Force-closed {closed_expiry} position(s) to avoid assignment risk."
                )
        else:
            # Normal day — only EMA crossover legs were closed
            await self._persist_state()
            held_spreads = len(self._active_spreads)
            held_condors = len(self._active_condors)
            parts = []
            if closed_ema:
                parts.append(f"Closed {closed_ema} EMA crossover leg(s).")
            if held_spreads or held_condors:
                parts.append(
                    f"Holding overnight: {held_spreads} spread(s) + {held_condors} condor(s) "
                    f"(DTE={dte}). Exit conditions (SL/profit/DTE<7) active tomorrow."
                )
            if parts and not self._eod_notified_today:
                self._eod_notified_today = True
                await self._notify("EOD POSITION UPDATE\n" + "\n".join(parts))

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
                loop = asyncio.get_running_loop()
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
            sigma = atr_to_annualised_vol(atr * _5MIN_ATR_SCALE, underlying_price)
            await update_iv_history(symbol, sigma, redis)
            return await get_iv_rank(symbol, redis)
        except Exception:
            return None

    async def _get_live_sigma(
        self,
        symbol: str,
        underlying_price: float,
        dte: int,
        interval: int,
        expiry,
        atr_sigma: float,
    ) -> float:
        """
        Fetch live implied vol from ATM option prices for delta-based strike selection.

        Averages CE and PE ATM implied vols to cancel out put-call parity skew.
        Falls back to ATR-derived sigma if kite is unavailable (paper mode) or
        the quote fetch fails for any reason.
        """
        from src.market_data.option_chain import get_option_quote, implied_vol as _iv
        from src.core.utils import build_option_symbol

        kite  = getattr(self, "_kite",  None)
        redis = getattr(self, "_redis", None)
        if kite is None:
            return atr_sigma  # paper mode or pre-login — use ATR sigma

        try:
            atm   = round(underlying_price / interval) * interval
            T     = max(dte, 1) / 365.0
            ce_c  = build_option_symbol(symbol, atm, "CE", expiry)
            pe_c  = build_option_symbol(symbol, atm, "PE", expiry)
            ce_p  = await get_option_quote(ce_c, kite, redis)
            pe_p  = await get_option_quote(pe_c, kite, redis)

            ivs: list = []
            if ce_p and ce_p > 0:
                iv = _iv(ce_p, underlying_price, atm, T, "CE")
                if iv and 0.05 <= iv <= 3.0:
                    ivs.append(iv)
            if pe_p and pe_p > 0:
                iv = _iv(pe_p, underlying_price, atm, T, "PE")
                if iv and 0.05 <= iv <= 3.0:
                    ivs.append(iv)

            if ivs:
                live_iv = sum(ivs) / len(ivs)
                logger.debug(
                    f"[LiveIV] {symbol}: ATM IV={live_iv:.1%} "
                    f"(ATR HV={atr_sigma:.1%}, ratio={live_iv/atr_sigma:.2f})"
                )
                return live_iv

        except Exception as exc:
            logger.debug(f"[LiveIV] {symbol}: fallback to ATR sigma — {exc}")

        return atr_sigma

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

        # In paper mode broker.get_positions() has unrealized_pnl=0.
        # Compute it from the engine's in-memory active positions instead.
        if unrealized == 0.0 and (self._active_spreads or self._active_condors):
            redis = getattr(self, "_redis", None)
            if redis:
                unrealized = await self._compute_engine_unrealized_pnl(redis)

        self.risk_manager.update_state(positions, realized, unrealized)

    async def _compute_engine_unrealized_pnl(self, redis) -> float:
        """Sum unrealized PnL across active spreads and condors using Redis option price cache."""

        async def _get_price(contract: str) -> float:
            if not contract:
                return 0.0
            try:
                v = await redis.get(f"optltp:{contract}")
                if v:
                    return float(v)
                v = await redis.get(f"optq:{contract}")
                if v:
                    return float(v)
            except Exception:
                pass
            return 0.0

        total = 0.0
        for s in self._active_spreads.values():
            lot = s.get("lot_size", 0)
            if not lot:
                continue
            cur_short = await _get_price(s.get("short_contract", ""))
            cur_long  = await _get_price(s.get("long_contract", ""))
            if cur_short > 0 and cur_long > 0:
                total += (
                    (s.get("short_premium", 0) - cur_short)
                    + (cur_long - s.get("long_premium", 0))
                ) * lot

        for c in self._active_condors.values():
            lot = c.get("lot_size", 0)
            if not lot:
                continue
            cur_ps = await _get_price(c.get("put_short_contract",  ""))
            cur_pl = await _get_price(c.get("put_long_contract",   ""))
            cur_cs = await _get_price(c.get("call_short_contract", ""))
            cur_cl = await _get_price(c.get("call_long_contract",  ""))
            if all(p > 0 for p in (cur_ps, cur_pl, cur_cs, cur_cl)):
                total += (
                    (c.get("put_short_premium",  0) - cur_ps)
                    + (c.get("call_short_premium", 0) - cur_cs)
                    - (cur_pl - c.get("put_long_premium",  0))
                    - (cur_cl - c.get("call_long_premium", 0))
                ) * lot

        return total

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
                        # LTPPoller stores naive local-time (IST). Compare against
                        # naive local-time now() — do NOT mix with UTC-aware now().
                        age = (_dt.now() - ts).total_seconds()
                    else:
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
            elif class_name == "MomentumStrategy":
                key = REDIS_TOP_SYMBOLS_MOMENTUM
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

    async def _refresh_event_calendar(self) -> None:
        """Auto-refresh earnings/event calendar from NSE (runs every Monday at open)."""
        try:
            from src.market_data.calendar_refresh import refresh_calendar
            redis = getattr(self, "_redis", None)
            ok = await refresh_calendar(redis)
            if ok:
                logger.info("[EventCalendar] Weekly calendar refresh complete")
            else:
                logger.warning("[EventCalendar] Weekly refresh returned empty — keeping existing calendar")
        except Exception as exc:
            logger.error(f"[EventCalendar] Weekly refresh failed: {exc}")
