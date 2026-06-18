import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.middleware.error_handler import global_exception_handler
from src.api.routers import (
    analytics_router,
    backtest_router,
    market_data_router,
    orders_router,
    positions_router,
    risk_router,
    signals_router,
    stocks_router,
    strategy_router,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start trading engine and scheduler; tear down cleanly on shutdown."""
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
    from src.notifications.combo_notifier import ComboNotifier
    from src.orders.order_manager import OrderManager
    from src.paper_trading.paper_broker import PaperBroker
    from src.portfolio.portfolio_manager import PortfolioManager
    from src.risk.risk_manager import RiskManager
    import src.strategies  # noqa: F401 — triggers @StrategyRegistry.register() decorators
    from src.strategies.base import StrategyRegistry

    PHASE1_SYMBOLS = list(FNO_SYMBOLS[:5])

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
    mode           = TradingMode(settings.TRADING_MODE)
    zerodha_ticker = None
    kite_instance  = None

    if mode == TradingMode.LIVE:
        from src.brokers.zerodha import ZerodhaBroker

        raw_token = await redis_client.get("zerodha:access_token")
        if not raw_token:
            logger.critical(
                "LIVE mode: Zerodha access token not in Redis. "
                "Run the auth script first. Falling back to PaperBroker."
            )
            broker = PaperBroker(initial_balance=settings.INITIAL_CAPITAL)
        else:
            access_token  = raw_token.strip()
            broker        = ZerodhaBroker.from_redis_token(
                settings.ZERODHA_API_KEY, settings.ZERODHA_API_SECRET, access_token
            )
            kite_instance = broker.kite   # expose for VIX + option quotes
            logger.info("LIVE mode: ZerodhaBroker authenticated from Redis token.")

            # Start WebSocket LTP ticker (daemon thread, non-blocking)
            try:
                from src.market_data.zerodha_ticker import ZerodhaTicker
                zerodha_ticker = ZerodhaTicker(
                    api_key=settings.ZERODHA_API_KEY,
                    access_token=access_token,
                    redis_url=settings.get_redis_url(),
                    symbols=set(FNO_SYMBOLS),
                )
                loop   = asyncio.get_event_loop()
                mapped = await loop.run_in_executor(
                    None, zerodha_ticker.fetch_instrument_tokens
                )
                if mapped > 0:
                    zerodha_ticker.start()
                    logger.info(f"ZerodhaTicker: live stream started for {mapped} symbols.")
                else:
                    logger.warning("ZerodhaTicker: no tokens mapped — skipping WebSocket stream.")
                    zerodha_ticker = None
            except Exception as e:
                logger.error(f"ZerodhaTicker init failed: {e}. Continuing without real-time LTP.")
                zerodha_ticker = None
    else:
        logger.info("PAPER mode: using PaperBroker.")
        broker = PaperBroker(initial_balance=settings.INITIAL_CAPITAL)

    order_mgr     = OrderManager(broker, risk_mgr, order_repo, audit_repo)
    portfolio_mgr = PortfolioManager(broker, position_repo, stock_repo)
    notifier      = ComboNotifier()   # sends to both email + Telegram simultaneously

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

    engine = LiveTradingEngine(broker, risk_mgr, order_mgr, portfolio_mgr, notifier)
    engine.attach_redis(redis_client)
    engine.set_symbols(PHASE1_SYMBOLS)
    if kite_instance:
        engine.attach_kite(kite_instance)   # enables real VIX + option quotes
    await engine.start()

    ltp_poller = LTPPoller(redis_client)

    scheduler = get_scheduler()
    schedule_trading_jobs(engine)
    schedule_zerodha_auth()
    scheduler.add_job(
        ltp_poller.poll,
        IntervalTrigger(seconds=60),
        id="ltp_poll",
        name="LTP Poller",
        replace_existing=True,
        misfire_grace_time=30,
    )
    start_scheduler()

    app.state.trading_engine = engine
    app.state.redis          = redis_client
    app.state.zerodha_ticker = zerodha_ticker

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

    try:
        import json
        if hasattr(app.state, "redis") and app.state.redis:
            await app.state.redis.ping()
            redis_status = "UP"
            raw = await app.state.redis.get("tick:RELIANCE")
            if raw:
                ltp_source = json.loads(raw).get("ltp_source", "unknown")
    except Exception as e:
        redis_status = f"DOWN: {e}"

    overall = "UP" if db_status == "UP" and redis_status == "UP" else "DEGRADED"
    return {
        "status":     overall,
        "database":   db_status,
        "redis":      redis_status,
        "ltp_source": ltp_source,
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)
