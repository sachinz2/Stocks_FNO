"""
Daily Zerodha <-> DB reconciliation -- live mode only.

sync_orders() (order_manager.py, every 30s) already reconciles status/
fill_price for orders OUR app placed and tracked. This covers the gap
that leaves open: an order that exists at the broker with no matching DB
row at all (a crash lost the PENDING write before it was persisted, a
GTT-triggered order that bypassed OrderManager entirely, a manual trade
placed directly in the Zerodha app) never gets noticed by that path since
it only looks at orders it already knows about.

Zerodha is the source of truth once real money is involved (go-live
decision, 2026-08-13: auto-correct, don't just alert) -- this inserts any
broker order missing from our DB and corrects fill_price/status on ones
that differ, then logs Zerodha's real available capital for the day.

Entry point: daily_zerodha_sync(), meant to run once daily (see the
scheduled job wiring in api/main.py), gated to when a real `kite` client
exists (i.e. live mode; paper mode has nothing at the broker to sync
against).
"""
import logging
from typing import Any, Dict, Optional

from src.core.utils import now_ist

logger = logging.getLogger(__name__)


def _map_status(broker_status: str) -> str:
    s = (broker_status or "").upper()
    if s in ("COMPLETE", "COMPLETED", "FILLED"):
        return "COMPLETED"
    if s in ("CANCELLED", "CANCELED"):
        return "CANCELLED"
    if s == "REJECTED":
        return "REJECTED"
    return "OPEN"


async def sync_orders_from_zerodha(kite, order_repo) -> Dict[str, int]:
    """Reconcile TODAY's orders between Zerodha and our `orders` table."""
    today = now_ist().date()
    broker_orders = kite.orders()

    today_orders = []
    for o in broker_orders:
        ts = o.get("order_timestamp")
        if ts is not None and hasattr(ts, "date") and ts.date() == today:
            today_orders.append(o)

    inserted, corrected, checked = 0, 0, 0
    for b in today_orders:
        broker_id = str(b.get("order_id", "") or "")
        if not broker_id:
            continue
        checked += 1

        status     = _map_status(b.get("status", ""))
        fill_price = b.get("average_price") or None
        side       = b.get("transaction_type", "")
        symbol     = b.get("tradingsymbol", "")
        quantity   = int(b.get("quantity", 0) or 0)
        price      = b.get("price") or fill_price

        matches = await order_repo.filter(broker_order_id=broker_id)
        existing = matches[0] if matches else None

        if existing is None:
            await order_repo.create({
                "broker_order_id": broker_id,
                "symbol": symbol, "side": side, "quantity": quantity,
                "price": price, "fill_price": fill_price,
                "order_status": status,
            })
            inserted += 1
            logger.warning(
                f"[ZerodhaSync] Broker order {broker_id} ({side} {quantity} {symbol}) "
                "has NO matching DB record -- inserted. Investigate how this was placed "
                "outside the normal order-placement path."
            )
            continue

        updates: Dict[str, Any] = {}
        if fill_price is not None:
            existing_fill = float(existing.fill_price) if existing.fill_price is not None else None
            if existing_fill is None or abs(existing_fill - float(fill_price)) > 0.01:
                updates["fill_price"] = float(fill_price)
        if status and status != existing.order_status:
            updates["order_status"] = status

        if updates:
            await order_repo.update(existing, updates)
            corrected += 1
            logger.warning(
                f"[ZerodhaSync] Corrected order {existing.id} (broker {broker_id}) "
                f"from Zerodha's real record: {updates}"
            )

    return {"checked": checked, "inserted": inserted, "corrected": corrected}


async def get_zerodha_capital(kite) -> Optional[Dict[str, float]]:
    """Real available cash / used margin from Zerodha (live mode)."""
    try:
        margins = kite.margins()
        equity = margins.get("equity", {})
        available = equity.get("available", {})
        capital_left = float(available.get("live_balance", available.get("cash", equity.get("net", 0))))
        capital_in_use = float(equity.get("utilised", {}).get("debits", 0))
        return {"capital_left": capital_left, "capital_in_use": capital_in_use}
    except Exception as exc:
        logger.error(f"[ZerodhaSync] Failed to fetch margins: {exc}")
        return None


async def daily_zerodha_sync(kite, order_repo) -> None:
    """Scheduled-job entry point. No-op (not an error) when kite is None (paper mode)."""
    if kite is None:
        return
    try:
        result = await sync_orders_from_zerodha(kite, order_repo)
        logger.info(f"[ZerodhaSync] Orders reconciled: {result}")
    except Exception as exc:
        logger.error(f"[ZerodhaSync] Order reconciliation failed: {exc}")

    capital = await get_zerodha_capital(kite)
    if capital:
        logger.info(
            f"[ZerodhaSync] Zerodha capital: left=Rs{capital['capital_left']:,.2f} "
            f"in_use=Rs{capital['capital_in_use']:,.2f}"
        )
