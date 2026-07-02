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

    # F: Fast exit-check job — every 10 seconds (spread/condor SL + profit only, no new entries).
    # Reduces SL overshoot from up to 60s lag to ≤10s. Uses _exit_cycle_lock so it never
    # runs concurrently with the 1-minute signal cycle.
    scheduler.add_job(
        engine._run_exit_checks_only,
        IntervalTrigger(seconds=10),
        id="fast_exit_check",
        name="Fast Exit Check (10s)",
        replace_existing=True,
        misfire_grace_time=5,
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

    # A: Gap check — 09:16:30 IST, Mon–Fri.
    # Checks overnight positions for gap breaches 90 seconds after market open,
    # before the first 60-second signal cycle fires. Catches gap-down/gap-up moves
    # that would otherwise not be detected until 09:17.
    scheduler.add_job(
        engine._check_gap_opens,
        CronTrigger(hour=9, minute=16, second=30, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="gap_check",
        name="Gap Check (09:16:30 IST)",
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


def schedule_zerodha_auth():
    """Schedule automated Zerodha login at 8:30 AM IST every weekday."""
    from scripts.zerodha_auto_auth import run_daily_auth
    scheduler = get_scheduler()
    scheduler.add_job(
        run_daily_auth,
        CronTrigger(hour=8, minute=30, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="zerodha_daily_auth",
        name="Zerodha Daily Auth",
        replace_existing=True,
    )
    logger.info("Zerodha daily auth scheduled at 08:30 IST.")
