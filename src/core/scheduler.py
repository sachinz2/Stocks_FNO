import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.core.constants import (
    JOB_DAILY_PNL,
    JOB_MARKET_CLOSE,
    JOB_MARKET_OPEN,
    JOB_ORDER_SYNC,
    JOB_SIGNAL_GENERATION,
)
from src.core.utils import now_ist

# Fixed 2026-09-03 (external review): LTPPoller.poll() (builds this cycle's
# OHLC/ATR/ADX/RVOL) and run_signal_cycle() (reads that same data to decide
# entries) used to be two fully independent 60s IntervalTrigger jobs with no
# ordering guarantee between them -- whichever job happened to be added to
# the scheduler a few milliseconds earlier got a head start that had
# nothing to do with which one actually needs to run first. Anchoring both
# to the same reference moment, with the signal cycle offset behind the LTP
# poll, guarantees fresh data every single interval rather than leaving it
# to import-order accident. 25s leaves real margin under the 60s interval
# even on a slower-than-usual poll cycle (misfire_grace_time=30 below
# already assumes normal poll cycles finish well inside that).
LTP_POLL_TO_SIGNAL_CYCLE_OFFSET_SECONDS = 25

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

    # LTP poll + signal generation are anchored to the same epoch (see
    # LTP_POLL_TO_SIGNAL_CYCLE_OFFSET_SECONDS above) so the signal cycle
    # reliably runs after this interval's LTP/indicator refresh, not just
    # whenever the two jobs happen to land relative to each other.
    _cycle_epoch = now_ist()

    ltp_poller = getattr(engine, "_symbol_poller", None)
    if ltp_poller:
        scheduler.add_job(
            ltp_poller.poll,
            IntervalTrigger(seconds=60, start_date=_cycle_epoch),
            id="ltp_poll",
            name="LTP Poller (Zerodha OHLC + indicators)",
            replace_existing=True,
            misfire_grace_time=30,
        )
    else:
        logger.warning(
            "schedule_trading_jobs: no symbol poller attached to engine -- "
            "ltp_poll job NOT scheduled here (must be registered separately)."
        )

    # Signal generation — every 1 minute, offset behind the LTP poll above.
    scheduler.add_job(
        engine.run_signal_cycle,
        IntervalTrigger(
            minutes=1,
            start_date=_cycle_epoch + timedelta(seconds=LTP_POLL_TO_SIGNAL_CYCLE_OFFSET_SECONDS),
        ),
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

    # Fixed 2026-08-21 (deep review): the cron-triggered jobs below had no
    # explicit misfire_grace_time, unlike the interval jobs above which all
    # set one -- APScheduler's own default is 1 SECOND, so a briefly-busy
    # event loop at exactly the trigger moment could silently coalesce/skip
    # a job that only fires once a day, with nothing beyond an internal
    # scheduler log line to notice. A few minutes' grace doesn't matter for
    # daily jobs like these (missing the exact second is harmless; missing
    # the whole run for the day is not).
    _DAILY_JOB_MISFIRE_GRACE_SEC = 300

    # Market open — 9:15 IST, Mon–Fri
    scheduler.add_job(
        engine.on_market_open,
        CronTrigger(hour=9, minute=15, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id=JOB_MARKET_OPEN,
        name="Market Open",
        replace_existing=True,
        misfire_grace_time=_DAILY_JOB_MISFIRE_GRACE_SEC,
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
        misfire_grace_time=_DAILY_JOB_MISFIRE_GRACE_SEC,
    )

    # Market close — 15:30 IST, Mon–Fri
    scheduler.add_job(
        engine.on_market_close,
        CronTrigger(hour=15, minute=30, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id=JOB_MARKET_CLOSE,
        name="Market Close",
        replace_existing=True,
        misfire_grace_time=_DAILY_JOB_MISFIRE_GRACE_SEC,
    )

    # Daily PnL report — 15:45 IST, Mon–Fri
    scheduler.add_job(
        engine.send_daily_report,
        CronTrigger(hour=15, minute=45, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id=JOB_DAILY_PNL,
        name="Daily PnL Report",
        replace_existing=True,
        misfire_grace_time=_DAILY_JOB_MISFIRE_GRACE_SEC,
    )

    # Capital period rollover — 08:00 IST, Mon–Fri (before market open at 09:15
    # and before the day's first signal cycle), so any expiry that passed
    # closes out and compounds into the new period's starting_capital before
    # anything gets sized against it. rollover_if_needed() is idempotent —
    # a no-op on every day that isn't the day after an expiry.
    scheduler.add_job(
        _run_capital_period_rollover,
        CronTrigger(hour=8, minute=0, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="capital_period_rollover",
        name="Capital Period Rollover",
        replace_existing=True,
        kwargs={"risk_manager": engine.risk_manager},
        misfire_grace_time=_DAILY_JOB_MISFIRE_GRACE_SEC,
    )

    logger.info("All trading jobs scheduled.")


async def _run_capital_period_rollover(risk_manager) -> None:
    from src.core.config import settings
    from src.database.connection import AsyncSessionLocal
    from src.portfolio.capital_periods import rollover_if_needed
    try:
        await rollover_if_needed(AsyncSessionLocal, settings.INITIAL_CAPITAL, risk_manager=risk_manager)
    except Exception as exc:
        logger.error(f"[CapitalPeriod] Rollover check failed: {exc}")


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
        misfire_grace_time=300,
    )
    logger.info("Zerodha daily auth scheduled at 08:30 IST.")
