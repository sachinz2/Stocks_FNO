"""
Expiry-to-expiry capital period tracking and compounding.

A "period" is one NSE monthly F&O expiry cycle (see
core/utils.get_capital_period_bounds). At rollover, a period's realized
P&L -- the sum of every trade_journal row that exited within its bounds --
is added to its starting_capital to become the NEXT period's
starting_capital. Profits/losses compound month over month instead of
every period starting from the same static settings.INITIAL_CAPITAL.

rollover_if_needed() is the entry point, meant to run once daily (see the
scheduled job wiring in api/main.py). It's idempotent: once a period is
closed it's never reprocessed, so calling this more than once a day (or
after a restart) is always safe.
"""
import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import select, func

from src.core.utils import get_capital_period_bounds, now_ist
from src.database.models.capital_period import CapitalPeriod
from src.database.models.trade_journal import TradeJournal

logger = logging.getLogger(__name__)

# Guards rollover_if_needed()'s check-then-insert against concurrent
# duplicate-period creation. _create_period() inserts unconditionally with
# no unique constraint, so two near-simultaneous callers (e.g. a deploy's
# startup call racing the 08:00 scheduled job tick) could both observe "no
# active period" via get_active_period() and each insert a duplicate open
# CapitalPeriod row. rollover_if_needed() is only ever called from within
# this one process (api/main.py startup, and src/core/scheduler.py's
# in-process APScheduler job -- both share the same event loop), so a plain
# in-process asyncio.Lock is sufficient; no distributed lock is needed.
_rollover_lock = asyncio.Lock()


async def get_active_period(session_factory, today: Optional[date] = None) -> Optional[CapitalPeriod]:
    """The still-open period covering `today` (None if never bootstrapped)."""
    today = today or now_ist().date()
    async with session_factory() as session:
        result = await session.execute(
            select(CapitalPeriod)
            .where(CapitalPeriod.closed.is_(False))
            .order_by(CapitalPeriod.period_start.desc())
            .limit(1)
        )
        return result.scalars().first()


async def _create_period(session_factory, period_start: date, period_end: date, starting_capital: float) -> CapitalPeriod:
    async with session_factory() as session:
        row = CapitalPeriod(
            period_start=period_start, period_end=period_end,
            starting_capital=starting_capital, closed=False,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(
            f"[CapitalPeriod] Opened {period_start} -> {period_end} "
            f"with starting_capital=Rs{starting_capital:,.2f}"
        )
        return row


async def _realized_pnl_for(session_factory, period_start: date, period_end: date) -> float:
    start_dt = datetime.combine(period_start, time.min)
    end_dt   = datetime.combine(period_end, time.max)
    async with session_factory() as session:
        result = await session.execute(
            select(func.sum(TradeJournal.pnl)).where(
                TradeJournal.exit_time.isnot(None),
                TradeJournal.exit_time >= start_dt,
                TradeJournal.exit_time <= end_dt,
            )
        )
        total = result.scalar()
        return float(total) if total is not None else 0.0


async def _close_period(session_factory, period: CapitalPeriod) -> CapitalPeriod:
    realized_pnl    = await _realized_pnl_for(session_factory, period.period_start, period.period_end)
    ending_capital  = float(period.starting_capital) + realized_pnl
    async with session_factory() as session:
        merged = await session.merge(period)
        merged.realized_pnl   = round(realized_pnl, 2)
        merged.ending_capital = round(ending_capital, 2)
        merged.closed         = True
        await session.commit()
        await session.refresh(merged)
        logger.info(
            f"[CapitalPeriod] Closed {period.period_start} -> {period.period_end}: "
            f"starting=Rs{float(period.starting_capital):,.2f} realized_pnl=Rs{realized_pnl:,.2f} "
            f"ending=Rs{ending_capital:,.2f}"
        )
        return merged


async def rollover_if_needed(session_factory, default_capital: float, risk_manager=None, today: Optional[date] = None) -> CapitalPeriod:
    """
    Ensure a CapitalPeriod exists covering `today`, closing out and
    compounding any overdue period(s) first (loops in case the job didn't
    run for more than one expiry cycle, e.g. after extended downtime).

    If risk_manager is passed, the active period's starting_capital is
    (re-)applied to risk_manager's live capital-based limits (exposure %,
    daily-loss %, capital-at-risk) on every call, not just when a rollover
    happens this call. Fixed 2026-08-13: this used to only sync on an
    actual rollover -- but risk_manager is a fresh instance on every
    process restart (constructed with the static settings.INITIAL_CAPITAL),
    and this function also runs once at every startup (see api/main.py).
    Gating the sync on "did a rollover just happen" meant any ordinary
    restart mid-period (deploy, crash, self-heal) silently reverted live
    risk limits back to the static config value until the next real
    rollover -- defeating the whole point of compounding except in the
    narrow window right after an expiry. set_capital() is idempotent
    (setting to the value it already has is a no-op in effect), so calling
    it unconditionally here is safe.
    """
    today = today or now_ist().date()

    # Serialize the whole check-then-insert/close sequence against any other
    # concurrent call in this process -- see _rollover_lock's module-level
    # comment for why this matters (startup call racing the scheduled job).
    async with _rollover_lock:
        active = await get_active_period(session_factory, today)
        if active is None:
            period_start, period_end = get_capital_period_bounds(today)
            active = await _create_period(session_factory, period_start, period_end, default_capital)

        rolled = False
        while active.period_end < today:
            closed = await _close_period(session_factory, active)
            next_start = closed.period_end + timedelta(days=1)
            _, next_end = get_capital_period_bounds(next_start)
            active = await _create_period(session_factory, next_start, next_end, float(closed.ending_capital))
            rolled = True

    if risk_manager is not None:
        risk_manager.set_capital(float(active.starting_capital))
        if rolled:
            logger.info(
                f"[CapitalPeriod] risk_manager capital updated to Rs{float(active.starting_capital):,.2f} "
                f"for the new period {active.period_start} -> {active.period_end}"
            )

    return active
