import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from src.brokers.base import AbstractBroker
from src.core.utils import now_ist
from src.risk.risk_manager import RiskManager
from src.database.repositories.base import BaseRepository
from src.database.models.order import Order
from src.database.models.audit import AuditLog

logger = logging.getLogger(__name__)

# Orders that stay OPEN longer than this are stale and should be cancelled
ORDER_EXPIRY_MINUTES = 5
# Price adjustment (%) when retrying a stale limit order — moved toward the
# market so the retry is more likely to actually fill (BUY: higher, SELL: lower).
RETRY_PRICE_ADJUSTMENT = 0.015   # 1.5% toward the market
# Maximum seconds to wait for any single broker API call
BROKER_TIMEOUT_SEC = 15
# How many recent ORDER_RECEIVED audit rows to scan when reconstructing a
# stale order's original placement context (strategy/is_exit_order/etc — see
# _get_retry_context). The target row is always recent (within ~one retry
# cycle of when the order was placed), so this is a generous bound, not a
# tight one — keeps the scan fast regardless of how large audit_logs grows
# over months of live trading.
_RETRY_CONTEXT_SCAN_LIMIT = 200
# Strategies whose entries are multi-leg structures (credit spread / iron
# condor). Their anchor (first) leg is a SELL and reaches this same
# is_spread_leg=False code path, but retrying it standalone would place a
# duplicate leg alongside one the engine has already moved on from (the
# engine doesn't wait for a fill before placing the next leg / building
# _active_spreads — see live_trading_engine.py). Retry is intentionally
# scoped to single-leg strategies (ema_crossover_v1, momentum_v1) and plain
# exits only; multi-leg entry legs are cancelled-and-left, same as before.
_MULTI_LEG_STRATEGIES = {"credit_spread_v1", "iron_condor_v1"}


class OrderManager:
    """
    Institutional-grade Order Management System (OMS).

    Handles order lifecycle:
      place_order() → risk validation → broker routing → DB state
      expire_stale_orders() → cancel orders open > 5 min → retry if possible
      sync_orders() → reconcile DB status with broker
    """

    def __init__(
        self,
        broker: AbstractBroker,
        risk_manager: RiskManager,
        order_repo: BaseRepository,
        audit_repo: BaseRepository,
    ):
        self.broker       = broker
        self.risk_manager = risk_manager
        self.order_repo   = order_repo
        self.audit_repo   = audit_repo

    # ── Audit helper ─────────────────────────────────────────────────────────

    async def _audit(self, action: str, payload: Dict[str, Any]) -> None:
        try:
            await self.audit_repo.create({
                "service_name": "OrderManager",
                "action":       action,
                "payload":      payload,
                # IST-naive, matching trade_journal (see now_ist() usage
                # throughout live_trading_engine.py) — was datetime.utcnow()
                # until 2026-08-06, which made every order/audit timestamp
                # display 5.5h behind the actual IST time on the dashboard.
                "timestamp":    now_ist().replace(tzinfo=None),
            })
        except Exception as e:
            logger.warning(f"Audit log skipped ({action}): {e}")

    # ── Place order ───────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol:        str,
        side:          str,
        quantity:      int,
        price:         float,
        is_spread_leg: bool = False,
        is_exit_order: bool = False,
        strategy_name: Optional[str] = None,
        iv_rank:       Optional[float] = None,
        vix:           Optional[float] = None,
        capital_at_risk: Optional[float] = None,
        is_retry:      bool = False,
    ) -> Optional[Order]:
        """
        Main entry point for placing orders.
        Validates risk, saves initial state, routes to broker, updates state.

        is_spread_leg : True for legs 2-4 of multi-leg structures (skips entry-only checks)
        is_exit_order : True when closing an existing position (skips entry-only risk checks)
        strategy_name : Passed to RiskManager for capital allocation check
        iv_rank       : Per-symbol IV rank — gates spread/condor entries
        vix           : India VIX — market-wide IV gate
        capital_at_risk : Explicit max-loss figure passed straight through to
                        RiskManager.validate_trade() — see its docstring.
        is_retry      : Internal — set by expire_stale_orders() when
                        resubmitting a cancelled stale order at an adjusted
                        price. Recorded in the audit log only, so a retry
                        that itself goes stale is never retried again (bounds
                        retries to one attempt per original order).
        """
        # 1. Create PENDING record in DB
        db_order = await self.order_repo.create({
            "symbol":       symbol,
            "side":         side,
            "quantity":     quantity,
            "price":        price,
            "order_status": "PENDING",
            "created_at":   now_ist().replace(tzinfo=None),
        })
        # Recorded so a later stale-order retry can reconstruct the original
        # placement context (see _get_retry_context) without needing new
        # columns on the orders table — audit_logs.payload is already a JSON
        # column that accepts this for free.
        await self._audit("ORDER_RECEIVED", {
            "order_id": db_order.id, "symbol": symbol, "side": side,
            "strategy": strategy_name,
            "is_exit_order": is_exit_order,
            "is_spread_leg": is_spread_leg,
            "is_retry": is_retry,
        })

        # 2. Risk validation
        if not self.risk_manager.validate_trade(
            symbol, side, quantity, price,
            is_spread_leg=is_spread_leg,
            is_exit_order=is_exit_order,
            strategy_name=strategy_name,
            iv_rank=iv_rank,
            vix=vix,
            capital_at_risk=capital_at_risk,
        ):
            # Capture the returned merged object so order_status is reflected correctly
            db_order = await self.order_repo.update(db_order, {"order_status": "REJECTED_BY_RISK"})
            await self._audit("ORDER_REJECTED_RISK", {"order_id": db_order.id})
            return db_order

        # 3. Route to broker
        try:
            broker_order_id = await asyncio.wait_for(
                self.broker.place_order(symbol, side, quantity, price),
                timeout=BROKER_TIMEOUT_SEC,
            )
            updates: Dict[str, Any] = {
                "broker_order_id": broker_order_id,
                "order_status":    "OPEN",
            }
            # Fixed 2026-08-07: PaperBroker fills synchronously inside the
            # broker.place_order() call above (fill_price is already known
            # this instant) -- but that return value is just a bare order
            # ID; fill_price was previously only ever populated later, by
            # sync_orders()'s separate periodic reconciliation. Every caller
            # that reads db_order.fill_price immediately after place_order()
            # returns (see live_trading_engine.py's single-leg/spread/condor
            # exit and single-leg entry code, "fixed 2026-08-06") was
            # therefore always reading it before sync_orders() had a chance
            # to run -- making that fix a no-op in practice. Confirmed live
            # 2026-08-07: TATACONSUM's real fills (BUY 19.98, SELL 17.46,
            # both logged correctly by PaperBroker) still got recorded in
            # trade_journal as the pre-slippage quotes (19.40, 18.00).
            # Reconciling once, immediately, right here closes that gap --
            # for PaperBroker (instant fill) this always finds it; for a
            # real broker with a genuinely still-pending order, get_orders()
            # correctly returns no fill yet and this is a no-op, same as the
            # existing behavior sync_orders() still handles for that case.
            try:
                broker_orders = await asyncio.wait_for(
                    self.broker.get_orders(), timeout=BROKER_TIMEOUT_SEC
                )
                b_order = next(
                    (o for o in broker_orders if str(o.get("order_id", "")) == str(broker_order_id)),
                    None,
                )
                if b_order:
                    updates.update(self._extract_fill_updates(b_order, price, None))
            except Exception as e:
                logger.debug(f"Immediate fill reconciliation failed (sync_orders will retry): {e}")

            # Must capture the return — BaseRepository.update() returns a new merged
            # SQLAlchemy object; the original db_order is detached and NOT updated in place.
            db_order = await self.order_repo.update(db_order, updates)
            await self._audit("ORDER_ROUTED", {
                "order_id": db_order.id, "broker_order_id": broker_order_id,
            })
            # Track per-strategy deployed capital for single-leg BUY entries
            # (ema_crossover_v1, momentum_v1). Excludes is_spread_leg=True legs —
            # for credit_spread_v1/iron_condor_v1 the hedge/long legs are placed
            # with is_spread_leg=True and strategy_name set, which used to also
            # hit this hook and double- (spread) or triple- (condor) count
            # deployed capital on top of the engine's own explicit, max-loss-based
            # add_deployed_capital() call for those two structures (found
            # 2026-07-30) — the two mechanisms stacked instead of one owning it.
            if side == "BUY" and strategy_name and not is_spread_leg:
                self.risk_manager.add_deployed_capital(strategy_name, quantity * price)
            return db_order
        except asyncio.TimeoutError:
            logger.error(f"Broker order timed out after {BROKER_TIMEOUT_SEC}s: {side} {quantity} {symbol}")
            db_order = await self.order_repo.update(db_order, {"order_status": "FAILED"})
            await self._audit("ORDER_TIMEOUT", {"order_id": db_order.id, "symbol": symbol})
            return db_order
        except Exception as e:
            logger.error(f"Broker order placement failed: {e}")
            db_order = await self.order_repo.update(db_order, {"order_status": "FAILED"})
            await self._audit("ORDER_FAILED", {"order_id": db_order.id, "error": str(e)})
            return db_order

    # ── Stale order expiry ────────────────────────────────────────────────────

    async def _get_retry_context(self, order_id: int) -> Optional[Dict[str, Any]]:
        """
        Reconstruct an order's original placement context from its
        ORDER_RECEIVED audit entry (see place_order()) — no schema change
        needed since audit_logs.payload is already JSON. Returns None if no
        matching entry is found (fails safe: caller treats that as
        "don't retry", same as before this feature existed).
        """
        try:
            rows = await self.audit_repo.filter(
                action="ORDER_RECEIVED", order_by="id DESC", limit=_RETRY_CONTEXT_SCAN_LIMIT,
            )
            for row in rows:
                payload = row.payload or {}
                if payload.get("order_id") == order_id:
                    return payload
        except Exception as e:
            logger.debug(f"Retry context lookup failed for order {order_id}: {e}")
        return None

    @staticmethod
    def _adjusted_retry_price(side: str, price: float) -> float:
        """Move the limit price toward the market so a retry is more likely
        to actually fill: BUY pays a bit more, SELL accepts a bit less."""
        if side == "BUY":
            return round(price * (1 + RETRY_PRICE_ADJUSTMENT), 2)
        return round(price * (1 - RETRY_PRICE_ADJUSTMENT), 2)

    async def expire_stale_orders(self) -> int:
        """
        Cancel any OPEN orders that have been pending for more than
        ORDER_EXPIRY_MINUTES. Called every cycle by the engine.

        Retries ONCE at an adjusted price (RETRY_PRICE_ADJUSTMENT toward the
        market) for single-leg orders only (ema_crossover_v1/momentum_v1
        entries, and any plain exit order) — see _MULTI_LEG_STRATEGIES for
        why credit_spread_v1/iron_condor_v1's anchor leg is excluded.
        Everything else is cancelled and left, exactly as before this retry
        feature existed.

        Also releases any deployed capital that was tentatively added when
        the now-cancelled order was first submitted (found 2026-07-30: a
        stale single-leg BUY that never filled was left permanently counted
        against its strategy's budget until the next day's reset, since
        nothing released it when the order was cancelled — a retry re-adds
        its own amount via the normal place_order() path, so this is correct
        whether or not the order ends up being retried).

        Returns the number of orders cancelled (including any retried).
        """
        cutoff = now_ist().replace(tzinfo=None) - timedelta(minutes=ORDER_EXPIRY_MINUTES)
        open_orders = await self.order_repo.filter(order_status="OPEN")
        cancelled = 0
        retried = 0

        for order in open_orders:
            if not order.created_at:
                continue
            # Normalise: strip tzinfo if present (DB stores IST naive)
            created = order.created_at.replace(tzinfo=None) if order.created_at.tzinfo else order.created_at
            if created > cutoff:
                continue  # not stale yet

            logger.warning(
                f"Stale order detected: id={order.id} {order.side} {order.symbol} "
                f"open for >{ORDER_EXPIRY_MINUTES} min. Cancelling."
            )
            try:
                if order.broker_order_id:
                    await asyncio.wait_for(
                        self.broker.cancel_order(order.broker_order_id),
                        timeout=BROKER_TIMEOUT_SEC,
                    )
            except (asyncio.TimeoutError, Exception) as e:
                logger.error(f"Broker cancel failed for order {order.id}: {e}")

            await self.order_repo.update(order, {
                "order_status": "EXPIRED",
                "updated_at":   now_ist().replace(tzinfo=None),
            })
            await self._audit("ORDER_EXPIRED", {"order_id": order.id, "symbol": order.symbol})
            cancelled += 1

            ctx = await self._get_retry_context(order.id)

            # Release deployed capital tentatively added at submission — only
            # ever applies to the same case place_order() adds it for (BUY,
            # strategy_name set, not a spread/condor hedge leg). Independent
            # of whether we go on to retry below.
            if (
                ctx and order.price is not None
                and order.side == "BUY" and ctx.get("strategy") and not ctx.get("is_spread_leg")
            ):
                self.risk_manager.release_deployed_capital(
                    ctx["strategy"], order.quantity * float(order.price)
                )

            eligible = (
                ctx is not None
                and order.price is not None
                and not ctx.get("is_spread_leg")
                and not ctx.get("is_retry")
                and (ctx.get("is_exit_order") or ctx.get("strategy") not in _MULTI_LEG_STRATEGIES)
            )
            if eligible:
                retry_price = self._adjusted_retry_price(order.side, float(order.price))
                logger.info(
                    f"Retrying stale order {order.id}: {order.side} {order.quantity} "
                    f"{order.symbol} @ Rs{order.price} -> Rs{retry_price} (one attempt only)"
                )
                await self.place_order(
                    order.symbol, order.side, order.quantity, retry_price,
                    is_spread_leg=False,
                    is_exit_order=bool(ctx.get("is_exit_order")),
                    strategy_name=ctx.get("strategy"),
                    is_retry=True,
                )
                retried += 1

        if cancelled:
            logger.info(f"Expired {cancelled} stale order(s), retried {retried}.")
        return cancelled

    # ── Cancel ────────────────────────────────────────────────────────────────

    async def cancel_order(self, internal_order_id: int) -> bool:
        db_order = await self.order_repo.get_by_id(internal_order_id)
        if not db_order or not db_order.broker_order_id:
            logger.warning(f"Order {internal_order_id} not found or has no broker ID.")
            return False

        if db_order.order_status in ["COMPLETED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED"]:
            logger.warning(f"Cannot cancel order in state {db_order.order_status}")
            return False

        success = await self.broker.cancel_order(db_order.broker_order_id)
        if success:
            await self.order_repo.update(db_order, {"order_status": "CANCELLED"})
            await self._audit("ORDER_CANCELLED", {"order_id": db_order.id})
        return success

    # ── Sync ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_fill_updates(
        b_order: Dict[str, Any], expected_price: Optional[float], existing_fill_price: Optional[float],
    ) -> Dict[str, Any]:
        """
        Pull fill_price/slippage out of a broker order dict, if present and
        not already recorded. Shared by place_order()'s immediate
        post-routing check and sync_orders()'s periodic reconciliation —
        same extraction logic, just called at two different points in an
        order's life (see place_order()'s "Fixed 2026-08-07" comment for why
        both are needed: PaperBroker fills synchronously, but nothing
        forwarded that fill_price to the DB until sync_orders() ran later).
        """
        if existing_fill_price is not None:
            return {}
        b_fill = b_order.get("fill_price") or b_order.get("average_price")
        if not b_fill:
            return {}
        b_fill = float(b_fill)
        updates: Dict[str, Any] = {"fill_price": b_fill}
        if expected_price:
            updates["slippage"] = round(b_fill - expected_price, 4)
        return updates

    async def sync_orders(self) -> None:
        """Reconcile OPEN orders with live broker status, including fill_price and slippage."""
        open_db_orders = await self.order_repo.filter(order_status="OPEN")
        if not open_db_orders:
            return
        try:
            broker_orders  = await asyncio.wait_for(
                self.broker.get_orders(), timeout=BROKER_TIMEOUT_SEC
            )
            broker_map     = {str(o.get("order_id", "")): o for o in broker_orders}

            for db_order in open_db_orders:
                if not db_order.broker_order_id:
                    continue
                b_order = broker_map.get(str(db_order.broker_order_id))
                if not b_order:
                    continue

                new_status = self._map_broker_status(b_order.get("status", "OPEN"))
                updates: dict = {}
                if new_status != db_order.order_status:
                    updates["order_status"] = new_status

                updates.update(self._extract_fill_updates(
                    b_order,
                    float(db_order.price) if db_order.price else None,
                    float(db_order.fill_price) if db_order.fill_price is not None else None,
                ))

                if updates:
                    await self.order_repo.update(db_order, updates)
                    if "order_status" in updates:
                        await self._audit("ORDER_STATUS_SYNC", {
                            "order_id": db_order.id, "new_status": new_status,
                        })
        except Exception as e:
            logger.error(f"Failed to sync orders: {e}")

    @staticmethod
    def _map_broker_status(broker_status: str) -> str:
        s = broker_status.upper()
        if s in ("COMPLETE", "COMPLETED", "FILLED"):
            return "COMPLETED"
        if s in ("CANCELLED", "CANCELED"):
            return "CANCELLED"
        if s == "REJECTED":
            return "REJECTED"
        return "OPEN"
