import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.logger import setup_logging
from src.api.middleware.error_handler import global_exception_handler
from src.api.routers import (
    analytics_router,
    backtest_router,
    logs_router,
    market_data_router,
    orders_router,
    positions_router,
    risk_router,
    signals_router,
    stocks_router,
    strategy_router,
)
from src.api.routers.admin_router import router as admin_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start trading engine and scheduler; tear down cleanly on shutdown."""
    # Must be called here (after uvicorn sets up its own handlers) so the
    # RotatingFileHandler is appended rather than skipped by the guard.
    setup_logging()

    import asyncio
    import redis.asyncio as aioredis
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger

    from src.core.config import settings
    from src.core.constants import FNO_SYMBOLS
    from src.core.enums import TradingMode
    from src.core.scheduler import (
        get_scheduler,
        schedule_trading_jobs,
        schedule_zerodha_auth,
        start_scheduler,
        stop_scheduler,
    )
    from src.database.connection import AsyncSessionLocal
    from src.database.models.audit import AuditLog
    from src.database.models.order import Order
    from src.database.models.position import Position
    from src.database.models.stock import Stock
    from src.database.repositories.base import BaseRepository
    from src.live_trading.live_trading_engine import LiveTradingEngine
    from src.market_data.ltp_poller import LTPPoller
    from src.notifications.email_service import EmailNotifier
    from src.orders.order_manager import OrderManager
    from src.paper_trading.paper_broker import PaperBroker
    from src.portfolio.portfolio_manager import PortfolioManager
    from src.risk.risk_manager import RiskManager
    from src.risk.strategy_monitor import StrategyMonitor
    from src.risk.portfolio_analyzer import PortfolioAnalyzer
    from src.market_data.regime_detector import MarketRegimeDetector
    from src.market_data.rs_ranker import RSRanker
    import src.strategies  # noqa: F401 — triggers @StrategyRegistry.register() decorators
    from src.strategies.base import StrategyRegistry
    from src.database.models.trade_journal import TradeJournal
    from src.database.models.walk_forward import WalkForwardResult  # noqa: F401 — creates table

    PHASE1_SYMBOLS = list(FNO_SYMBOLS)

    # ── DB tables ──────────────────────────────────────────────────────────────
    import src.database.models  # noqa: F401 — registers all ORM models with Base
    from src.database.base import Base
    from src.database.connection import engine as db_engine
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables verified / created.")

    redis_client = aioredis.from_url(settings.get_redis_url(), decode_responses=True)
    risk_mgr     = RiskManager(
        initial_capital=settings.INITIAL_CAPITAL,
        max_exposure_per_trade_pct=settings.MAX_EXPOSURE_PCT,
        max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT,
    )

    order_repo    = BaseRepository(Order,    AsyncSessionLocal)
    audit_repo    = BaseRepository(AuditLog, AsyncSessionLocal)
    position_repo = BaseRepository(Position, AsyncSessionLocal)
    stock_repo    = BaseRepository(Stock,    AsyncSessionLocal)

    # ── Broker: live vs paper ──────────────────────────────────────────────────
    # Data source (Zerodha WebSocket + kite) is independent of trade execution.
    # Paper mode keeps PaperBroker for orders but can still use Zerodha for
    # real-time LTP, VIX, option quotes, and IV rank — giving realistic paper
    # trading on live market data without risking real money.
    mode           = TradingMode(settings.TRADING_MODE)
    zerodha_ticker = None

    async def _provision_kite():
        """
        Build a Zerodha kite client + instrument token map from whatever
        access token currently sits in Redis. Returns (kite, tokens, token_str)
        — (None, {}, None) if no valid token is available right now. Safe to
        call repeatedly — see the self-healing job registered below for why
        this is a function rather than inline one-shot startup code.
        """
        raw = await redis_client.get("zerodha:access_token")
        if not (raw and settings.ZERODHA_API_KEY and settings.ZERODHA_API_SECRET):
            return None, {}, None
        token = raw.strip()
        from src.brokers.zerodha import ZerodhaBroker
        _data_broker = ZerodhaBroker.from_redis_token(
            settings.ZERODHA_API_KEY, settings.ZERODHA_API_SECRET, token
        )
        kite = _data_broker.kite
        tokens: dict = {}
        try:
            loop = asyncio.get_event_loop()
            # Fixed 2026-08-20: used to resolve tokens only for the static
            # FNO_SYMBOLS list. The weekly active-universe recompute job can
            # promote a symbol beyond that static list -- without this, a
            # restart between weekly runs would rebuild instrument_tokens
            # from scratch using only the static 132, losing the
            # already-resolved token for anything dynamically added and
            # leaving it unpollable until the *next* Sunday's run happened
            # to re-cover it. Resolve against the full real F&O stock
            # universe instead, a superset that always covers whatever the
            # dynamic active list currently contains.
            from src.market_data.fno_universe import extract_stock_underlyings
            nfo_instruments = await loop.run_in_executor(None, kite.instruments, "NFO")
            full_universe = set(extract_stock_underlyings(nfo_instruments))
            nse_instruments = await loop.run_in_executor(None, kite.instruments, "NSE")
            for inst in nse_instruments:
                sym = inst.get("tradingsymbol", "")
                if sym in full_universe:
                    tokens[sym] = inst["instrument_token"]
            logger.info(f"Instrument tokens loaded: {len(tokens)}/{len(full_universe)} F&O stock symbols.")
        except Exception as e:
            logger.warning(f"Instrument token fetch failed: {e}")
        return kite, tokens, token

    # ── Always try Zerodha for market data if a token exists ──────────────────
    kite_instance, instrument_tokens, access_token = await _provision_kite()
    if kite_instance:
        logger.info("Zerodha kite session ready for market data (VIX, option quotes).")
        try:
            from src.market_data.zerodha_ticker import ZerodhaTicker
            zerodha_ticker = ZerodhaTicker(
                api_key=settings.ZERODHA_API_KEY,
                access_token=access_token,
                redis_url=settings.get_redis_url(),
                symbols=set(FNO_SYMBOLS),
            )
            # Re-use already-fetched tokens to avoid a second instruments API call
            zerodha_ticker._instrument_tokens = instrument_tokens.copy()
            zerodha_ticker._token_symbol      = {v: k for k, v in instrument_tokens.items()}
            if instrument_tokens:
                zerodha_ticker.start()
                logger.info(f"ZerodhaTicker: live stream started for {len(instrument_tokens)} symbols.")
            else:
                logger.warning("ZerodhaTicker: no tokens — skipping WebSocket stream.")
                zerodha_ticker = None
        except Exception as e:
            logger.error(f"ZerodhaTicker init failed: {e}. Continuing without real-time LTP.")
            zerodha_ticker = None
    else:
        logger.warning(
            "No Zerodha access token in Redis — LTP and indicator data unavailable "
            "until the self-healing kite-provisioning job (every 3 min, see below) "
            "picks one up. Run scripts/zerodha_auto_auth.py to fix this immediately."
        )

    # Constructed here (moved ahead of the broker-selection block below) so the
    # LIVE->PaperBroker silent-fallback case immediately below can actually
    # alert someone, not just log — see its 2026-08-20 fix note.
    notifier = EmailNotifier()

    # ── Order execution broker ─────────────────────────────────────────────────
    if mode == TradingMode.LIVE:
        if not access_token:
            logger.critical(
                "LIVE mode: Zerodha access token missing. Falling back to PaperBroker."
            )
            broker = PaperBroker(initial_balance=settings.INITIAL_CAPITAL)
            # Fixed 2026-08-20 (deep review): this fallback used to be a
            # logger.critical() call and nothing else -- the app boots fine,
            # /health reports UP, and the system silently trades on paper
            # money all day with no active alert, only a log line nobody is
            # watching. TRADING_MODE=LIVE with no real broker is exactly the
            # kind of gap the self-heal watchdog exists to catch for market
            # data; it was never applied to this order-execution choice.
            try:
                await notifier.send(
                    "CRITICAL: TRADING_MODE=LIVE but no Zerodha access token was "
                    "found at startup -- falling back to PaperBroker. NO REAL "
                    "ORDERS will be placed until this is fixed and the app is "
                    "restarted. Run scripts/zerodha_auto_auth.py."
                )
            except Exception as exc:
                logger.error(f"Failed to send LIVE->PaperBroker fallback alert: {exc}")
        else:
            from src.brokers.zerodha import ZerodhaBroker
            broker = ZerodhaBroker.from_redis_token(
                settings.ZERODHA_API_KEY, settings.ZERODHA_API_SECRET, access_token
            )
            logger.info("LIVE mode: ZerodhaBroker active — real orders will be placed.")
    else:
        logger.info("PAPER mode: PaperBroker active — no real orders will be placed.")
        broker = PaperBroker(initial_balance=settings.INITIAL_CAPITAL)

    order_mgr     = OrderManager(broker, risk_mgr, order_repo, audit_repo)
    portfolio_mgr = PortfolioManager(broker, position_repo, stock_repo)

    # ── Strategies ─────────────────────────────────────────────────────────────
    StrategyRegistry.load_strategy("EMA_CROSSOVER", "ema_crossover_v1", {
        "fast_period": 20, "slow_period": 50,
        "stop_loss_pct": 0.50, "target_pct": 1.0, "trailing_stop_pct": 0.25,
    })
    StrategyRegistry.load_strategy("CREDIT_SPREAD", "credit_spread_v1", {
        "fast_period": 20, "slow_period": 50,
        "low_vol_threshold": 1.2, "spread_width": 2,
        "profit_close_pct": 0.25, "stop_loss_multiple": 2.0, "min_dte": 7,
    })
    StrategyRegistry.load_strategy("IRON_CONDOR", "iron_condor_v1", {
        "fast_period": 20, "slow_period": 50,
        "low_vol_threshold": 1.2, "flat_threshold": 0.1,
        "short_offset": 1, "hedge_offset": 2,
        "profit_close_pct": 0.25, "stop_loss_multiple": 2.0, "min_dte": 7,
    })
    StrategyRegistry.load_strategy("MOMENTUM", "momentum_v1", {
        "fast_period": 20, "slow_period": 50,
        "adx_entry_threshold": 35, "adx_exit_threshold": 22,
        "min_ema_spread_pct": 0.30,
        "stop_loss_pct": 0.50, "target_pct": 1.50, "trailing_stop_pct": 0.30,
    })

    trade_journal_repo = BaseRepository(TradeJournal, AsyncSessionLocal)
    strategy_monitor   = StrategyMonitor(trade_journal_repo)
    portfolio_analyzer = PortfolioAnalyzer()
    regime_detector    = MarketRegimeDetector(redis_client)
    rs_ranker          = RSRanker(redis_client, kite=kite_instance, instrument_tokens=instrument_tokens)

    engine = LiveTradingEngine(
        broker, risk_mgr, order_mgr, portfolio_mgr, notifier,
        strategy_monitor=strategy_monitor,
        portfolio_analyzer=portfolio_analyzer,
        regime_detector=regime_detector,
        rs_ranker=rs_ranker,
    )
    engine.attach_redis(redis_client)
    engine.set_symbols(PHASE1_SYMBOLS)
    if kite_instance:
        engine.attach_kite(kite_instance)   # enables real VIX + option quotes
    await engine.start()

    ltp_poller = LTPPoller(redis_client, kite=kite_instance, instrument_tokens=instrument_tokens)
    engine.attach_symbol_poller(ltp_poller)

    scheduler = get_scheduler()
    schedule_trading_jobs(engine)
    schedule_zerodha_auth()
    # Run once at startup too (not just the 08:00 IST daily job) so a fresh
    # deploy/restart bootstraps the first capital period immediately instead
    # of waiting for the next scheduled tick.
    from src.core.scheduler import _run_capital_period_rollover
    await _run_capital_period_rollover(risk_mgr)

    # Daily Zerodha <-> DB reconciliation (live mode only) -- 08:45 IST,
    # after the 08:30 auth job has a fresh token and before 09:15 market
    # open. Reads app.state.kite live at run time (not the startup snapshot)
    # since self-heal can rotate it in later in the day.
    #
    # Fixed 2026-08-13: originally gated on "kite exists", not TradingMode
    # -- but a real, authenticated kite client is attached regardless of
    # paper/live mode (see "Always try Zerodha for market data" above, used
    # for real VIX/option quotes even in paper mode). Without this explicit
    # mode check, this job would have run during paper trading too and
    # pulled the REAL Zerodha account's actual orders into this app's
    # `orders` table -- paper trades never touch the real account, so
    # there's nothing there to reconcile, and doing so risks corrupting the
    # paper trade history with unrelated real activity on that account.
    async def _run_daily_zerodha_sync() -> None:
        if mode != TradingMode.LIVE:
            return
        from src.live_trading.zerodha_sync import daily_zerodha_sync
        kite_now = getattr(app.state, "kite", None)
        await daily_zerodha_sync(kite_now, order_repo)

    scheduler.add_job(
        _run_daily_zerodha_sync,
        CronTrigger(hour=8, minute=45, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="daily_zerodha_sync",
        name="Daily Zerodha Reconciliation",
        replace_existing=True,
    )
    scheduler.add_job(
        ltp_poller.poll,
        IntervalTrigger(seconds=60),
        id="ltp_poll",
        name="LTP Poller (Zerodha OHLC + indicators)",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # RS Ranking: runs every 5 minutes (downloads 30d daily history — heavier)
    scheduler.add_job(
        rs_ranker.rank,
        IntervalTrigger(seconds=300),
        id="rs_rank",
        name="Relative Strength Ranker",
        replace_existing=True,
        misfire_grace_time=60,
    )

    # Weekly F&O universe liquidity recompute (2026-08-20) -- Sunday 07:00
    # IST, well outside market hours and before the Monday 08:30 daily auth
    # job. See scripts/zerodha_auto_auth.py's recompute_active_universe()
    # docstring for why this exists: the 41->132 FNO_SYMBOLS expansion the
    # same day was a one-time snapshot that would otherwise go stale the
    # same way FNO_STRIKE_INTERVALS silently did before it was made
    # self-correcting.
    async def _weekly_universe_refresh() -> None:
        raw = await redis_client.get("zerodha:access_token")
        if not raw:
            logger.warning("Weekly universe refresh: no Zerodha token available, skipping this run.")
            return
        token = raw.strip()
        try:
            from scripts.zerodha_auto_auth import recompute_active_universe
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, recompute_active_universe, token)
        except Exception as e:
            logger.error(f"Weekly universe refresh failed: {e}")
            await engine._notify(f"WARNING: weekly F&O universe refresh failed: {e}")
            return

        # Push freshly-resolved tokens into the live components immediately
        # instead of waiting for the next restart -- a symbol newly promoted
        # into the active set needs a resolvable NSE token to actually be
        # pollable/rankable this same run, not after the next deploy.
        #
        # Fixed 2026-08-20 (code review round 2): this used to run AFTER the
        # skipped-check below and return early on a skip, discarding a fully
        # valid, already-fetched tokens dict (resolve_nse_tokens() succeeds
        # or fails independently of the turnover-fetch failure that triggers
        # a skip) -- instrument tokens would then go stale for a week even
        # though the data to refresh them was sitting right there.
        if result["tokens"]:
            app.state.instrument_tokens = result["tokens"]
            kite_now = getattr(app.state, "kite", None)
            if kite_now is not None:
                ltp_poller.set_kite(kite_now, result["tokens"])
                rs_ranker.set_kite(kite_now, result["tokens"])
                # Fixed 2026-08-20 (code review): ltp_poller/rs_ranker were
                # updated here, but ZerodhaTicker (the WebSocket client --
                # the actual primary real-time tick source; LTPPoller's own
                # module docstring says today's bars are built ONLY from
                # live ticks, never historical_data()) and ZerodhaLTPPoller
                # (its REST fallback when the WebSocket is down) were left
                # frozen at their startup-time static symbol list. A symbol
                # newly promoted here would get no real-time tick coverage
                # at all if the WebSocket ever needed the REST fallback.
                #
                # Fixed 2026-08-20 (code review round 2): the ZerodhaTicker
                # update below used to mutate zt._instrument_tokens/
                # _token_symbol directly -- correct bookkeeping, but
                # subscribe()/set_mode() are only ever called from
                # ZerodhaTicker._on_connect(), so an already-open WebSocket
                # connection was never actually told about the change (5 of
                # 6 code-review finder angles independently caught this).
                # set_instrument_tokens() pushes the update to the live
                # connection immediately instead.
                zt = getattr(app.state, "zerodha_ticker", None)
                if zt is not None:
                    zt.set_instrument_tokens(result["tokens"])
                zlp = getattr(app.state, "zerodha_ltp_poller", None)
                if zlp is not None:
                    zlp.set_symbols(list(result["tokens"].keys()))

        # Fixed 2026-08-20 (code review): recompute_active_universe() refuses
        # to publish (keeps the previous active list live) when it only got
        # turnover data for a fraction of the universe -- a partial API
        # failure, not a genuine liquidity change. That's a distinct,
        # actionable outcome from "recomputed successfully, nothing changed".
        # Checked AFTER the token push above (tokens are valid either way).
        if result.get("skipped"):
            logger.error(
                f"Weekly universe refresh: skipped publishing (data coverage "
                f"{result.get('coverage', '?')}) -- kept the previous active list live."
            )
            await engine._notify(
                f"WARNING: weekly F&O universe refresh got incomplete data "
                f"(coverage {result.get('coverage', '?')}) and was skipped -- "
                "the previous active list is still live. Check Zerodha API health."
            )
            return

        added, removed = result["added"], result["removed"]
        logger.info(
            f"Weekly universe refresh: {len(result['active'])} active symbols "
            f"({len(added)} added, {len(removed)} removed)"
        )

        if added or removed:
            summary = "F&O universe weekly review\n"
            if added:
                summary += f"+ Added ({len(added)}): {', '.join(added)}\n"
            if removed:
                summary += f"- Removed ({len(removed)}): {', '.join(removed)}\n"
            await engine._notify(summary)

    scheduler.add_job(
        _weekly_universe_refresh,
        CronTrigger(day_of_week="sun", hour=7, minute=0, timezone="Asia/Kolkata"),
        id="weekly_universe_refresh",
        name="Weekly F&O Universe Liquidity Recompute",
        replace_existing=True,
    )

    # ── Zerodha REST LTP refresh (near-real-time, runs every 5 s) ─────────────
    # Always run when kite_instance is available — provides a reliable 5-second
    # LTP update via REST as a complement to (or fallback for) WebSocket.
    # zerodha_ticker may be set but fail in its background thread (403), so we
    # cannot use `not zerodha_ticker` as the condition here.
    zerodha_ltp_poller = None
    if kite_instance:
        from src.market_data.zerodha_ltp_poller import ZerodhaLTPPoller
        zerodha_ltp_poller = ZerodhaLTPPoller(kite_instance, redis_client, list(FNO_SYMBOLS))
        scheduler.add_job(
            zerodha_ltp_poller.refresh_ltp,
            IntervalTrigger(seconds=5),
            id="zerodha_ltp_rest",
            name="Zerodha LTP REST poller",
            replace_existing=True,
            misfire_grace_time=3,
        )
        logger.info("ZerodhaLTPPoller: REST-based LTP refresh every 5 s (WebSocket fallback).")
        engine.attach_ltp_poller(zerodha_ltp_poller)
    start_scheduler()

    # ── Scheduler dead-man's-switch ──────────────────────────────────────────
    # Fixed 2026-08-20 (component review): every self-heal job that already
    # exists (kite self-heal, weekly universe refresh, etc.) is ITSELF a
    # scheduled APScheduler job -- if the scheduler or the whole asyncio event
    # loop wedges (not just a Kite-specific issue), none of those can fire to
    # detect or fix it either; a hung event loop can't run more async code to
    # report that it's hung. This uses a genuinely independent OS thread (not
    # an asyncio task), which keeps running on its own via Python's normal
    # thread scheduling even while the event loop is blocked, checking a
    # heartbeat timestamp written by a lightweight dedicated job. If the
    # heartbeat goes stale, the whole process exits -- docker-compose's
    # `restart: unless-stopped` brings up a fresh one. Same "force a real
    # restart rather than try to reconnect in place" philosophy already
    # proven for the Kite WebSocket tick-staleness watchdog above.
    import threading
    import time

    _scheduler_heartbeat = {"t": time.monotonic()}
    _HEARTBEAT_INTERVAL_SECONDS = 30
    # 5x the interval: generous margin against one slow cycle (a busy signal
    # cycle, a slow broker call blocking the loop) triggering a false alarm.
    _HEARTBEAT_STALE_THRESHOLD_SECONDS = 150

    async def _scheduler_heartbeat_job() -> None:
        _scheduler_heartbeat["t"] = time.monotonic()

    scheduler.add_job(
        _scheduler_heartbeat_job,
        IntervalTrigger(seconds=_HEARTBEAT_INTERVAL_SECONDS),
        id="scheduler_heartbeat",
        name="Scheduler Dead-Man's-Switch Heartbeat",
        replace_existing=True,
        misfire_grace_time=15,
    )

    def _scheduler_watchdog_thread() -> None:
        while True:
            time.sleep(_HEARTBEAT_STALE_THRESHOLD_SECONDS)
            age = time.monotonic() - _scheduler_heartbeat["t"]
            if age > _HEARTBEAT_STALE_THRESHOLD_SECONDS:
                import os
                logger.critical(
                    f"SCHEDULER DEAD-MAN'S-SWITCH: no heartbeat for {age:.0f}s "
                    "-- the asyncio event loop/scheduler appears wedged. "
                    "Exiting process for a full restart (docker-compose will "
                    "bring up a fresh one)."
                )
                os._exit(1)

    threading.Thread(
        target=_scheduler_watchdog_thread, daemon=True, name="scheduler-deadman-watchdog",
    ).start()

    app.state.trading_engine    = engine
    app.state.redis             = redis_client
    app.state.zerodha_ticker    = zerodha_ticker
    app.state.zerodha_ltp_poller = zerodha_ltp_poller
    app.state.kite              = kite_instance
    app.state.instrument_tokens = instrument_tokens
    app.state.last_kite_token   = access_token
    app.state.last_auth_retry_attempt = None  # see _kite_self_heal's no-token retry path
    app.state.last_instrument_cache_retry_attempt = None  # see _kite_self_heal's cache-refresh retry path

    # ── Self-healing kite provisioning ───────────────────────────────────────
    # kite_instance used to be a one-time startup snapshot: if Redis had no
    # valid access token at that exact moment (e.g. a restart during the daily
    # token's TTL gap — access tokens expire after 24h, ex=86400 in
    # zerodha_auto_auth.py), LTPPoller/RSRanker/engine were permanently stuck
    # with no market data for the rest of the process's lifetime, even once
    # the next day's 08:30 auth wrote a fresh token — nothing ever rechecked.
    # Confirmed this silently broke ALL THREE strategies (zero signals, zero
    # trades) for 3 full trading days (2026-07-27 through 07-29) after one
    # Sunday-evening restart, with no ERROR-level logs to flag it. This job
    # covers both halves of that gap:
    #   1. kite_instance still None -> try provisioning again; if it works,
    #      wire it into ltp_poller/rs_ranker/engine and start the
    #      ticker/REST-poller if they never got the chance to at startup.
    #   2. kite_instance already exists but Redis has since rotated in a
    #      newer token (normal daily 08:30 auth) -> refresh it in place with
    #      set_access_token(). Every holder shares this same object, so this
    #      alone keeps them all current — no re-wiring needed for this case.
    # Ticks must be totally silent for this long, during market hours, before
    # the watchdog below forces a reconnect. Matches this job's own 180s
    # cadence — worst case detection is ~2 cycles (~6 min), still far better
    # than the 2+ hours of silent staleness confirmed live 2026-07-31.
    TICK_STALE_THRESHOLD_SECONDS = 180
    # Cooldown between fresh-login retry attempts when no token exists at all
    # (see below) -- long enough not to hammer Zerodha's login endpoint /
    # repeatedly launch a headless browser if credentials or network are
    # genuinely broken, short enough that a missed 08:30 auth job recovers
    # within one trading session instead of losing the whole day.
    AUTH_RETRY_COOLDOWN_SECONDS = 600

    async def _kite_self_heal():
        raw = await redis_client.get("zerodha:access_token")
        if not raw:
            # Fixed 2026-08-13: this used to just return here, silently
            # giving up every 3 minutes forever. If the scheduled 08:30
            # daily-auth job never fires -- confirmed live 2026-08-13: a
            # deploy restarted this process right at 08:30, wiping
            # APScheduler's in-memory state before the login could complete
            # -- NOTHING else ever puts a token back in Redis, and this job
            # would keep silently doing nothing until tomorrow's 08:30 slot.
            # That's a full trading day with zero live data if it happens on
            # a day nobody's watching the logs. Actively retry the login
            # here instead of only waiting for the next scheduled slot.
            from src.core.utils import now_ist, is_auth_retry_window, should_retry_auth
            now = now_ist()
            if not is_auth_retry_window(now):
                return
            if not should_retry_auth(app.state.last_auth_retry_attempt, now, AUTH_RETRY_COOLDOWN_SECONDS):
                return
            app.state.last_auth_retry_attempt = now
            logger.warning(
                "Kite self-heal: no access token in Redis -- the scheduled "
                "08:30 auth job appears to have been missed. Triggering a "
                f"fresh login now (retrying at most every {AUTH_RETRY_COOLDOWN_SECONDS}s)."
            )
            try:
                from scripts.zerodha_auto_auth import run_daily_auth
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, run_daily_auth)
                logger.info(
                    "Kite self-heal: fresh login succeeded -- provisioning "
                    "will pick up the new token on the next cycle."
                )
            except Exception as e:
                logger.error(f"Kite self-heal: fresh login attempt failed: {e}")
            return
        token = raw.strip()

        if app.state.kite is None:
            kite, tokens, tok = await _provision_kite()
            if kite is None:
                return
            # Fixed 2026-08-20 (code review): used to divide by len(FNO_SYMBOLS)
            # (132), but _provision_kite() resolves tokens against the full
            # real F&O stock universe (up to ~208) -- that denominator could
            # read as an impossible "over 100%" ratio. _provision_kite()
            # itself already logs the accurate "X/Y F&O stock symbols" count.
            logger.info(f"Kite provisioning recovered — {len(tokens)} tokens, wiring into live components.")
            ltp_poller.set_kite(kite, tokens)
            rs_ranker.set_kite(kite, tokens)
            engine.attach_kite(kite)
            app.state.kite              = kite
            app.state.instrument_tokens = tokens
            app.state.last_kite_token   = tok

            if app.state.zerodha_ticker is None and tokens:
                from src.market_data.zerodha_ticker import ZerodhaTicker
                zt = ZerodhaTicker(
                    api_key=settings.ZERODHA_API_KEY, access_token=tok,
                    redis_url=settings.get_redis_url(), symbols=set(FNO_SYMBOLS),
                )
                # set_instrument_tokens() is a no-op beyond bookkeeping here
                # (self._ticker is still None pre-.start()) -- used instead
                # of direct attribute mutation for consistency with the
                # weekly-refresh call site below, which DOES need the live
                # WebSocket push this same method provides once started.
                zt.set_instrument_tokens(tokens)
                zt.start()
                app.state.zerodha_ticker = zt
                logger.info(f"ZerodhaTicker: live stream started late ({len(tokens)} symbols) after kite recovery.")

            if scheduler.get_job("zerodha_ltp_rest") is None:
                from src.market_data.zerodha_ltp_poller import ZerodhaLTPPoller
                zlp = ZerodhaLTPPoller(kite, redis_client, list(FNO_SYMBOLS))
                scheduler.add_job(
                    zlp.refresh_ltp, IntervalTrigger(seconds=5),
                    id="zerodha_ltp_rest", name="Zerodha LTP REST poller",
                    replace_existing=True, misfire_grace_time=3,
                )
                engine.attach_ltp_poller(zlp)
                app.state.zerodha_ltp_poller = zlp
                logger.info("ZerodhaLTPPoller: REST-based LTP refresh started late after kite recovery.")

        elif token != app.state.last_kite_token:
            app.state.kite.set_access_token(token)
            app.state.last_kite_token = token
            logger.info("Kite access token rotated — refreshed in place on the shared client.")

        # ── Instrument-cache self-heal ─────────────────────────────────────────
        # Fixed 2026-08-14: the token-missing retry path above only catches a
        # FAILED LOGIN. run_daily_auth() stores the token BEFORE attempting the
        # separate lot-size/real-contract cache refresh (see
        # zerodha_auto_auth.py) -- so a login that succeeds followed by an
        # instrument-fetch failure (network blip, Zerodha rate-limit, timeout
        # on the ~5-10MB instrument dump) leaves a valid token in Redis with
        # empty caches, and the check above never fires since `raw` is
        # present. Since LiveTradingEngine._get_lot_size()/_resolve_contract()
        # now fail closed on a cache miss (no more computed-guess fallback),
        # this used to be merely degraded and is now a full "zero new entries
        # all day" outage with nothing watching for it. Same active-retry
        # pattern as the no-token case above, separate cooldown so the two
        # don't interfere with each other.
        from scripts.zerodha_auto_auth import instrument_caches_healthy
        if not instrument_caches_healthy():
            from src.core.utils import now_ist, is_auth_retry_window, should_retry_auth
            now = now_ist()
            if is_auth_retry_window(now) and should_retry_auth(
                app.state.last_instrument_cache_retry_attempt, now, AUTH_RETRY_COOLDOWN_SECONDS,
            ):
                app.state.last_instrument_cache_retry_attempt = now
                logger.warning(
                    "Kite self-heal: lot-size/real-contract caches are empty despite "
                    "a valid token -- the daily instrument refresh appears to have "
                    "failed independently of login. New entries are fail-closed-"
                    f"blocked until this succeeds. Retrying now (at most every "
                    f"{AUTH_RETRY_COOLDOWN_SECONDS}s)."
                )
                try:
                    from scripts.zerodha_auto_auth import refresh_instrument_caches
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, refresh_instrument_caches, token)
                    logger.info("Kite self-heal: instrument cache refresh succeeded.")
                except Exception as e:
                    logger.error(f"Kite self-heal: instrument cache refresh attempt failed: {e}")

        # ── Tick-staleness watchdog ──────────────────────────────────────────
        # A WebSocket connection can look healthy (connected, no error/disconnect
        # callback ever fires) while silently no longer delivering ticks — the
        # provisioning checks above (kite None / token rotated) don't catch this
        # since kite itself is fine; only the ticker's own data flow is stuck.
        # Confirmed live 2026-07-31: after a 403 -> token-refresh -> reconnect
        # cycle, a new connection attempt logged "connecting..." and then
        # nothing — no on_connect, no error — for 2+ hours, leaving all 41
        # symbols on the historical-data bootstrap fallback all morning with
        # no automatic recovery. Only a full process restart fixed it. This
        # force-reconnects in place instead of waiting for a human to notice.
        ticker = app.state.zerodha_ticker
        if ticker is not None:
            from src.core.utils import is_market_open
            if is_market_open():
                stale_for = ticker.seconds_since_last_tick()
                if stale_for is not None and stale_for > TICK_STALE_THRESHOLD_SECONDS:
                    # Fixed 2026-08-07: ticker.stop(); ticker.start() looked like
                    # a reconnect but didn't fix anything -- confirmed live, this
                    # exact watchdog fired every 3 min for 27+ minutes (07:06 403
                    # -> 08:30 fresh token -> silent non-connection for 99 min ->
                    # watchdog loop 10:09-10:36, all 41 symbols stuck on the
                    # historical-close bootstrap fallback the whole time) without
                    # ever recovering. KiteTicker runs on Twisted's global
                    # reactor, which can only be started once per process --
                    # stop()/start() in place leaves a dead reactor that logs
                    # "connecting..." but never processes another on_connect/
                    # on_error again. This docstring's own prior-incident note
                    # already said "only a full process restart fixed it" --
                    # this now actually does that instead of repeating the
                    # in-place reconnect that's already proven not to work.
                    # Safe because of the "PaperBroker silently lost single-leg
                    # positions across every restart" fix (2026-08-06): engine
                    # state (spreads/condors/single-leg journals, balance) now
                    # correctly rebuilds from Redis on every restart, so this
                    # exit-and-let-Docker-restart-me pattern is the same one
                    # already exercised (and verified) manually many times
                    # today, just triggered automatically instead of requiring
                    # someone to notice and SSH in. `restart: unless-stopped` on
                    # the api service in docker-compose.yml brings it back up.
                    import os
                    logger.critical(
                        f"ZerodhaTicker: no ticks for {stale_for:.0f}s during market hours "
                        "— in-place reconnect already proven insufficient for this failure "
                        "mode (dead Twisted reactor); exiting process for a full restart."
                    )
                    os._exit(1)

    scheduler.add_job(
        _kite_self_heal,
        IntervalTrigger(seconds=180),
        id="kite_self_heal",
        name="Kite provisioning self-heal",
        replace_existing=True,
        misfire_grace_time=60,
    )

    # Restore email-pause state across restarts
    if await redis_client.get("alerts:email_paused"):
        engine.notifier.paused = True
        logger.warning("Email alerts are PAUSED (persisted from previous session)")

    logger.info(
        f"Falcon Trader STARTED | Mode={mode.value.upper()} | "
        f"Capital=Rs{settings.INITIAL_CAPITAL:,.0f} | "
        f"RealTimeLTP={'yes' if zerodha_ticker else 'no'} | "
        f"Kite={'yes' if kite_instance else 'no'}"
    )

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────────
    await engine.stop()
    stop_scheduler()
    if zerodha_ticker:
        zerodha_ticker.stop()
    await redis_client.aclose()
    logger.info("Falcon Trader: clean shutdown complete.")


app = FastAPI(
    title="Falcon Quant Platform API",
    version="2.0",
    description="Automated algorithmic trading platform — NSE F&O",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_exception_handler(Exception, global_exception_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
async def health_check():
    """Real health check — tests DB query and Redis ping."""
    db_status    = "DOWN"
    redis_status = "DOWN"
    ltp_source   = "unknown"

    try:
        from src.database.connection import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_status = "UP"
    except Exception as e:
        db_status = f"DOWN: {e}"

    # Market-data source health — market:trend_stats is published by LTPPoller
    # every poll cycle (see ltp_poller.py). Today's own bars always come from
    # live ticks now (Zerodha confirmed historical_data() isn't reliable for the
    # current session — see ltp_poller.py module docstring); symbols_without_live_data
    # > 0 means a symbol has no live tick data yet (bootstrap edge case, e.g. the
    # first few seconds after market open), not a "fallback engaging" situation.
    data_source = {
        "all_symbols_live":          None,
        "symbols_without_live_data": None,
        "n_symbols":                 None,
    }

    try:
        import json
        if hasattr(app.state, "redis") and app.state.redis:
            await app.state.redis.ping()
            redis_status = "UP"
            raw = await app.state.redis.get("tick:RELIANCE")
            if raw:
                ltp_source = json.loads(raw).get("ltp_source", "unknown")
            trend_raw = await app.state.redis.get("market:trend_stats")
            if trend_raw:
                trend = json.loads(trend_raw)
                data_source["all_symbols_live"]          = trend.get("all_symbols_live")
                data_source["symbols_without_live_data"]  = trend.get("symbols_without_live_data")
                data_source["n_symbols"]                  = trend.get("n_symbols")
    except Exception as e:
        redis_status = f"DOWN: {e}"

    overall = "UP" if db_status == "UP" and redis_status == "UP" else "DEGRADED"
    source_label = {
        "zerodha_realtime":    "Zerodha WebSocket (real-time)",
        "zerodha_rest":        "Zerodha REST poll (5 s)",
        "zerodha_historical":  "Zerodha historical OHLC (bootstrap fallback)",
        "zerodha_live_ticks":  "Zerodha live ticks (real-time bars)",
    }.get(ltp_source, ltp_source)

    # Live virtual cash balance (PaperBroker only — this is the actual ledger
    # that decreases on every BUY and increases on every SELL, gating whether a
    # new order can be placed; see paper_broker.py place_order()). None in live
    # (Zerodha) mode, where real broker margin/funds apply instead.
    available_cash = None
    try:
        engine = getattr(app.state, "trading_engine", None)
        broker = getattr(engine, "broker", None)
        if broker is not None and hasattr(broker, "balance"):
            available_cash = round(float(broker.balance), 2)
    except Exception:
        pass

    return {
        "status":         overall,
        "database":       db_status,
        "redis":          redis_status,
        "ltp_source":     source_label,
        "data_source":    data_source,
        "available_cash": available_cash,
    }


app.include_router(analytics_router.router,   prefix="/api/v1")
app.include_router(stocks_router.router,       prefix="/api/v1")
app.include_router(market_data_router.router,  prefix="/api/v1")
app.include_router(orders_router.router,       prefix="/api/v1")
app.include_router(positions_router.router,    prefix="/api/v1")
app.include_router(signals_router.router,      prefix="/api/v1")
app.include_router(risk_router.router,         prefix="/api/v1")
app.include_router(backtest_router.router,     prefix="/api/v1")
app.include_router(strategy_router.router,     prefix="/api/v1")
app.include_router(logs_router.router,         prefix="/api/v1")
app.include_router(admin_router,               prefix="/api/v1")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)
