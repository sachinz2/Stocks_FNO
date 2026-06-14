import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.core.constants import (
    JOB_DAILY_PNL,
    JOB_MARKET_CLOSE,
    JOB_MARKET_OPEN,
    JOB_ORDER_SYNC,
    JOB_POSITION_SYNC,
    JOB_SIGNAL_GENERATION,
)

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    return _scheduler


def start_scheduler():
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started.")


def stop_scheduler():
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def schedule_trading_jobs(engine) -> None:
    """Register all recurring trading-day jobs onto the scheduler."""
    scheduler = get_scheduler()

    # Signal generation — every 1 minute
    scheduler.add_job(
        engine.run_signal_cycle,
        IntervalTrigger(minutes=1),
        id=JOB_SIGNAL_GENERATION,
        name="Signal Generation",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # Order sync — every 30 seconds
    scheduler.add_job(
        engine.sync_orders,
        IntervalTrigger(seconds=30),
        id=JOB_ORDER_SYNC,
        name="Order Sync",
        replace_existing=True,
        misfire_grace_time=15,
    )

    # Position sync — every 1 minute
    scheduler.add_job(
        engine.sync_positions,
        IntervalTrigger(minutes=1),
        id=JOB_POSITION_SYNC,
        name="Position Sync",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # Market open — 9:15 IST, Mon–Fri
    scheduler.add_job(
        engine.on_market_open,
        CronTrigger(hour=9, minute=15, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id=JOB_MARKET_OPEN,
        name="Market Open",
        replace_existing=True,
    )

    # Market close — 15:30 IST, Mon–Fri
    scheduler.add_job(
        engine.on_market_close,
        CronTrigger(hour=15, minute=30, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id=JOB_MARKET_CLOSE,
        name="Market Close",
        replace_existing=True,
    )

    # Daily PnL report — 15:45 IST, Mon–Fri
    scheduler.add_job(
        engine.send_daily_report,
        CronTrigger(hour=15, minute=45, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id=JOB_DAILY_PNL,
        name="Daily PnL Report",
        replace_existing=True,
    )

    logger.info("All trading jobs scheduled.")
