import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.middleware.error_handler import global_exception_handler
from src.api.routers import (
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
    import redis.asyncio as aioredis
    from apscheduler.triggers.interval import IntervalTrigger

    from src.core.config import settings
    from src.core.constants import FNO_SYMBOLS
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
    import src.strategies  # noqa: F401 — importing triggers @StrategyRegistry.register() decorators
    from src.strategies.base import StrategyRegistry

    # Phase 1: top 5 liquid F&O symbols
    PHASE1_SYMBOLS = list(FNO_SYMBOLS[:5])

    # ── Ensure all DB tables exist ────────────────────────────────────────────
    import src.database.models  # noqa: F401 — registers all ORM models with Base
    from src.database.base import Base
    from src.database.connection import engine as db_engine
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables verified / created.")

    redis_client = aioredis.from_url(settings.get_redis_url(), decode_responses=True)

    broker = PaperBroker(initial_balance=settings.INITIAL_CAPITAL)
    risk_mgr = RiskManager(initial_capital=settings.INITIAL_CAPITAL)

    # Pass the factory, not an instance — each DB op gets its own session
    order_repo = BaseRepository(Order, AsyncSessionLocal)
    audit_repo = BaseRepository(AuditLog, AsyncSessionLocal)
    position_repo = BaseRepository(Position, AsyncSessionLocal)
    stock_repo = BaseRepository(Stock, AsyncSessionLocal)

    order_mgr = OrderManager(broker, risk_mgr, order_repo, audit_repo)
    portfolio_mgr = PortfolioManager(broker, position_repo, stock_repo)
    notifier = EmailNotifier()

    # Strategy 1: EMA Crossover — buys CE/PE options on momentum (high-volatility regime)
    StrategyRegistry.load_strategy("EMA_CROSSOVER", "ema_crossover_v1", {
        "fast_period": 20,
        "slow_period": 50,
        "stop_loss_pct": 0.50,       # exit if option premium drops 50% from entry
        "target_pct": 1.0,            # exit if option premium doubles (2×)
        "trailing_stop_pct": 0.25,    # exit if premium falls 25% below its peak
    })

    # Strategy 2: Credit Spread — sells option spreads to collect theta (low-volatility regime)
    # Complements EMA Crossover: fires when ATR% < 1.2% (market not making explosive moves).
    # Bull Put Spread (EMA bullish): SELL ATM PE + BUY OTM PE — wins if underlying holds up.
    # Bear Call Spread (EMA bearish): SELL ATM CE + BUY OTM CE — wins if underlying holds down.
    StrategyRegistry.load_strategy("CREDIT_SPREAD", "credit_spread_v1", {
        "fast_period": 20,
        "slow_period": 50,
        "low_vol_threshold": 1.2,    # only enter spreads when ATR% < 1.2% (low vol)
        "spread_width": 2,            # hedge leg is 2 strike intervals away from short leg
        "profit_close_pct": 0.25,    # take profit when short leg decays to 25% of sold price
        "stop_loss_multiple": 2.0,   # stop loss when short leg rises to 2× sold price
        "min_dte": 7,                 # close spread at least 7 days before expiry
    })

    # Strategy 3: Iron Condor — collect theta from both sides (low-vol + flat EMA regime)
    # Fires when ATR% < 1.2% AND EMA is completely flat (spread < 0.1%) — stock going nowhere.
    # Sells OTM PE + OTM CE simultaneously; wins as long as underlying stays in the range.
    # Gets its own symbol pool from Redis (nfo:top5:condor) — most range-bound stocks.
    StrategyRegistry.load_strategy("IRON_CONDOR", "iron_condor_v1", {
        "fast_period": 20,
        "slow_period": 50,
        "low_vol_threshold": 1.2,    # same threshold as credit spread
        "flat_threshold": 0.1,        # EMA spread must be below 0.1% of price
        "short_offset": 1,            # short strikes are 1 interval away from ATM
        "hedge_offset": 2,            # hedge legs are 2 more intervals from the short strikes
        "profit_close_pct": 0.25,    # close when both short legs decay to 25% of sold price
        "stop_loss_multiple": 2.0,   # stop loss when either short leg doubles
        "min_dte": 7,                 # close at least 7 days before expiry
    })

    engine = LiveTradingEngine(broker, risk_mgr, order_mgr, portfolio_mgr, notifier)
    engine.attach_redis(redis_client)
    engine.set_symbols(PHASE1_SYMBOLS)  # fallback if Redis top5 is absent
    await engine.start()

    # Poller covers all 40 symbols; it writes top-5 to Redis after each poll
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
    app.state.redis = redis_client

    logger.info("Falcon Trader: engine + scheduler started.")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    await engine.stop()
    stop_scheduler()
    await redis_client.aclose()
    logger.info("Falcon Trader: engine + scheduler stopped.")


app = FastAPI(
    title="Falcon Quant Platform API",
    version="1.0",
    description="Automated algorithmic trading platform API",
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
    return {"status": "UP", "database": "UP", "redis": "UP"}


app.include_router(stocks_router.router, prefix="/api/v1")
app.include_router(market_data_router.router, prefix="/api/v1")
app.include_router(orders_router.router, prefix="/api/v1")
app.include_router(positions_router.router, prefix="/api/v1")
app.include_router(signals_router.router, prefix="/api/v1")
app.include_router(risk_router.router, prefix="/api/v1")
app.include_router(backtest_router.router, prefix="/api/v1")
app.include_router(strategy_router.router, prefix="/api/v1")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)
