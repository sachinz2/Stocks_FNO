"""
Admin API — reset trading data and control email alerts.
These endpoints mutate live state; use with care.
"""
import logging
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

from src.database.connection import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])

# Tables wiped by /reset (reference data is preserved)
_TRADING_TABLES = [
    "audit_logs",
    "trade_journal",
    "walk_forward_results",
    "orders",
    "positions",
    "trades",
    "signals",
]

# Redis keys cleared by /reset
_ENGINE_REDIS_KEYS = [
    "engine:active_spreads",
    "engine:active_condors",
    "engine:single_leg_journals",
    "falcon:active_spreads",
    "falcon:active_condors",
]


@router.post("/reset")
async def reset_all_data(request: Request):
    """
    Delete all trading history and reset in-memory engine state.

    Keeps reference data intact (stocks, instruments, ohlc_data, indicators).
    Also resets PaperBroker virtual balance and clears Redis engine state.
    """
    try:
        # ── 1. Truncate DB trading tables ────────────────────────────────────
        async with AsyncSessionLocal() as session:
            await session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for table in _TRADING_TABLES:
                await session.execute(text(f"DELETE FROM `{table}`"))
                logger.info(f"Reset: cleared table '{table}'")
            await session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            await session.commit()

        # ── 2. Clear Redis engine state ──────────────────────────────────────
        redis = getattr(request.app.state, "redis", None)
        if redis:
            for key in _ENGINE_REDIS_KEYS:
                await redis.delete(key)
            logger.info("Reset: cleared Redis engine state keys")

        # ── 3. Reset live trading engine in-memory state ─────────────────────
        engine = getattr(request.app.state, "trading_engine", None)
        if engine:
            engine._active_spreads.clear()
            engine._active_condors.clear()
            engine._single_leg_journals.clear()
            engine._peak_premiums.clear()
            engine._today_order_count = 0
            engine.risk_manager.reset_daily_state()
            logger.info("Reset: cleared engine in-memory state")

            # Reset PaperBroker virtual balance and positions
            broker = getattr(engine, "broker", None)
            if broker and hasattr(broker, "_positions"):
                from src.core.config import settings
                broker._positions.clear()
                broker._orders.clear()
                broker.balance = settings.INITIAL_CAPITAL
                broker.total_fees_paid = 0.0
                logger.info(f"Reset: PaperBroker balance restored to ₹{settings.INITIAL_CAPITAL:,.0f}")

        logger.warning("PLATFORM RESET performed — all trading data cleared.")
        return {
            "status": "ok",
            "message": "All trading data cleared. Platform ready for fresh start.",
            "tables_cleared": _TRADING_TABLES,
        }

    except Exception as e:
        logger.error(f"Reset failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/email-alerts/pause")
async def pause_email_alerts(request: Request):
    """Pause all email notifications until resumed."""
    notifier = _get_notifier(request)
    notifier.paused = True
    redis = getattr(request.app.state, "redis", None)
    if redis:
        await redis.set("alerts:email_paused", "1")
    logger.warning("Email alerts PAUSED by admin request")
    return {"status": "paused", "email_alerts": False}


@router.post("/email-alerts/resume")
async def resume_email_alerts(request: Request):
    """Resume email notifications."""
    notifier = _get_notifier(request)
    notifier.paused = False
    redis = getattr(request.app.state, "redis", None)
    if redis:
        await redis.delete("alerts:email_paused")
    logger.info("Email alerts RESUMED by admin request")
    return {"status": "active", "email_alerts": True}


@router.get("/email-alerts")
async def get_email_alert_status(request: Request):
    """Return current email alert state."""
    notifier = _get_notifier(request)
    return {
        "email_alerts": not notifier.paused,
        "paused": notifier.paused,
        "configured": notifier.enabled,
    }


def _get_notifier(request: Request):
    engine = getattr(request.app.state, "trading_engine", None)
    if engine and engine.notifier:
        return engine.notifier
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Trading engine or notifier not initialised."
    )
