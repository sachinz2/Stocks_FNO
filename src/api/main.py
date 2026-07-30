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
    risk_mgr     = RiskManager(initial_capital=settings.INITIAL_CAPITAL)

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
            nse_instruments = await loop.run_in_executor(None, kite.instruments, "NSE")
            fno_set = set(FNO_SYMBOLS)
            for inst in nse_instruments:
                sym = inst.get("tradingsymbol", "")
                if sym in fno_set:
                    tokens[sym] = inst["instrument_token"]
            logger.info(f"Instrument tokens loaded: {len(tokens)}/{len(fno_set)} F&O symbols.")
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

    # ── Order execution broker ─────────────────────────────────────────────────
    if mode == TradingMode.LIVE:
        if not access_token:
            logger.critical(
                "LIVE mode: Zerodha access token missing. Falling back to PaperBroker."
            )
            broker = PaperBroker(initial_balance=settings.INITIAL_CAPITAL)
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
    notifier      = EmailNotifier()

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

    scheduler = get_scheduler()
    schedule_trading_jobs(engine)
    schedule_zerodha_auth()
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

    # ── Zerodha REST LTP refresh (near-real-time, runs every 5 s) ─────────────
    # Always run when kite_instance is available — provides a reliable 5-second
    # LTP update via REST as a complement to (or fallback for) WebSocket.
    # zerodha_ticker may be set but fail in its background thread (403), so we
    # cannot use `not zerodha_ticker` as the condition here.
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

    app.state.trading_engine    = engine
    app.state.redis             = redis_client
    app.state.zerodha_ticker    = zerodha_ticker
    app.state.kite              = kite_instance
    app.state.instrument_tokens = instrument_tokens
    app.state.last_kite_token   = access_token

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
    async def _kite_self_heal():
        raw = await redis_client.get("zerodha:access_token")
        if not raw:
            return
        token = raw.strip()

        if app.state.kite is None:
            kite, tokens, tok = await _provision_kite()
            if kite is None:
                return
            logger.info(
                f"Kite provisioning recovered — {len(tokens)}/{len(FNO_SYMBOLS)} "
                "tokens, wiring into live components."
            )
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
                zt._instrument_tokens = tokens.copy()
                zt._token_symbol      = {v: k for k, v in tokens.items()}
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
                logger.info("ZerodhaLTPPoller: REST-based LTP refresh started late after kite recovery.")

        elif token != app.state.last_kite_token:
            app.state.kite.set_access_token(token)
            app.state.last_kite_token = token
            logger.info("Kite access token rotated — refreshed in place on the shared client.")

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
