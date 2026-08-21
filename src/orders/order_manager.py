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
        product_override: Optional[str] = None,
    ) -> Optional[Order]:
        """
        Main entry point for placing orders.
        Validates risk, saves initial state, routes to broker, updates state.

        is_spread_leg : True for legs 2-4 of multi-leg structures (skips entry-only checks)
        is_exit_order : True when closing an existing position (skips entry-only risk checks)
        strategy_name : Passed to RiskManager for capital allocation check, and
                        to ZerodhaBroker for MIS vs NRML product selection
        iv_rank       : Per-symbol IV rank — gates spread/condor entries
        vix           : India VIX — market-wide IV gate
        capital_at_risk : Explicit max-loss figure passed straight through to
                        RiskManager.validate_trade() — see its docstring.
        is_retry      : Internal — set by expire_stale_orders() when
                        resubmitting a cancelled stale order at an adjusted
                        price. Recorded in the audit log only, so a retry
                        that itself goes stale is never retried again (bounds
                        retries to one attempt per original order).
        product_override : For closing an ORPHANED position where
                        strategy_name can't be recovered — pass the real
                        product string straight from the broker's own
                        position record instead. See ZerodhaBroker._product_for().
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
                self.broker.place_order(
                    symbol, side, quantity, price,
                    is_exit_order=is_exit_order, strategy_name=strategy_name,
                    product_override=product_override,
                    client_order_id=str(db_order.id),
                ),
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
            #
            # Fixed 2026-08-20 (deep review): this write used to sit inside the
            # same try/except as the broker call above -- if it itself raised
            # (transient DB hiccup right after a genuinely successful broker
            # placement), control fell into the generic `except Exception`
            # below, which persisted order_status=FAILED using the STALE
            # db_order (broker_order_id never written). sync_orders() only
            # ever reconciles order_status=='OPEN' rows, so a FAILED row with
            # no broker_order_id can never be tied back to the real, live
            # broker order again -- capital/position tracking would silently
            # diverge from reality with no reconciliation path. A DB failure
            # AFTER the broker has already accepted the order must never
            # downgrade it to FAILED. Retry the critical write a few times;
            # if it still fails, keep the order live in the caller's eyes
            # (in-memory broker_order_id/OPEN) and escalate loudly instead of
            # silently orphaning it.
            _db_exc: Optional[Exception] = None
            for _attempt in range(3):
                try:
                    db_order = await self.order_repo.update(db_order, updates)
                    _db_exc = None
                    break
                except Exception as e:
                    _db_exc = e
                    if _attempt < 2:
                        await asyncio.sleep(0.5 * (_attempt + 1))

            if _db_exc is not None:
                logger.critical(
                    f"CRITICAL: order placed at broker (broker_order_id={broker_order_id}, "
                    f"{side} {quantity} {symbol}) but persisting it to the DB failed after "
                    f"3 attempts: {_db_exc}. Order is LIVE at the broker -- NOT marking FAILED. "
                    f"Manual reconciliation required for internal order id={db_order.id}."
                )
                try:
                    await self._audit("ORDER_DB_PERSIST_FAILED", {
                        "order_id": db_order.id, "broker_order_id": broker_order_id,
                        "error": str(_db_exc),
                    })
                except Exception:
                    pass
                # Return an in-memory-patched record so the immediate caller
                # (e.g. live_trading_engine.py reading order_status/fill_price
                # right after this call) still sees the real broker outcome,
                # even though the DB row itself may still read PENDING.
                db_order.broker_order_id = broker_order_id
                db_order.order_status = updates.get("order_status", "OPEN")
                if "fill_price" in updates:
                    db_order.fill_price = updates["fill_price"]
                return db_order

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

            # Fixed 2026-08-21 (deep review): risk_manager.current_open_positions
            # was only refreshed from the broker once per cycle, BEFORE the
            # entry loop starts -- so the sector-concentration and max-open-
            # position checks (validate_trade() layers 4 and 6, both skipped
            # for is_spread_leg=True legs, matching those checks' own scope)
            # saw the same stale snapshot for every entry placed within that
            # same cycle. If 3+ names from a 2-per-sector-capped sector
            # ranked in the same cycle's candidates, each one's entry passed
            # the "0 open positions in this sector" check, breaching the cap
            # 2-3x within a single minute. Appending the just-opened
            # position here (this is the single choke point every entry
            # order passes through) makes the NEXT entry in the same cycle
            # see it immediately, without an extra broker round trip.
            if not is_exit_order and not is_spread_leg:
                self.risk_manager.current_open_positions.append({"symbol": symbol, "quantity": quantity})
            return db_order
        except asyncio.TimeoutError:
            logger.error(f"Broker order timed out after {BROKER_TIMEOUT_SEC}s: {side} {quantity} {symbol}")
            # Fixed 2026-08-20 (external review): a client-side timeout does
            # NOT mean the broker never processed the order -- the
            # underlying kite.place_order() call runs in a worker thread
            # (see ZerodhaBroker.place_order()) that asyncio.wait_for's
            # cancellation cannot actually stop; it can keep running and
            # succeed seconds after we've already given up waiting.
            # Immediately marking FAILED risked the exact same "broker
            # accepted it, we think it didn't" split already fixed this
            # session for the post-success DB-write-failure case. Reconcile
            # against the broker's real order list (matched by the same tag
            # place_order() attached) before deciding.
            #
            # Fixed 2026-08-21 (external review): _reconcile_after_timeout()
            # used to swallow its OWN failure to check (get_orders() itself
            # erroring, e.g. the exact kind of transient connectivity blip
            # that caused the 2026-08-21 KAYNES incident) and return None --
            # indistinguishable from "checked, genuinely not there." Both
            # outcomes then fell through to the same `order_status=FAILED`
            # line below, silently concluding failure for an order whose
            # real fate is actually unknown. If the broker DID accept it,
            # a real position now exists completely untracked (no capital
            # allocation, no exit monitoring, nothing). Now:
            # _reconcile_after_timeout() raises instead of swallowing, so
            # this can distinguish "verified not found" (below, still
            # legitimately FAILED) from "could not verify at all" (this
            # except clause) and refuse to guess for the latter.
            try:
                reconciled_id = await self._reconcile_after_timeout(db_order.id)
            except Exception as _verify_exc:
                logger.critical(
                    f"CRITICAL: order {db_order.id} ({side} {quantity} {symbol}) timed out "
                    f"client-side AND verification against the broker's order list also "
                    f"failed ({_verify_exc}) -- outcome is genuinely unknown. NOT marking "
                    f"FAILED (a real broker fill would then go completely untracked). "
                    f"Left as PENDING_VERIFICATION -- expire_stale_orders() retries this "
                    f"automatically every cycle; escalate manually if it persists."
                )
                db_order = await self.order_repo.update(db_order, {"order_status": "PENDING_VERIFICATION"})
                await self._audit("ORDER_VERIFICATION_FAILED", {
                    "order_id": db_order.id, "symbol": symbol, "error": str(_verify_exc),
                })
                return db_order

            if reconciled_id:
                db_order = await self.order_repo.update(db_order, {
                    "broker_order_id": reconciled_id, "order_status": "OPEN",
                })
                await self._audit("ORDER_TIMEOUT_RECONCILED", {
                    "order_id": db_order.id, "broker_order_id": reconciled_id,
                })
                logger.warning(
                    f"Order {db_order.id} timed out client-side but WAS found "
                    f"live at the broker (id={reconciled_id}) -- corrected to "
                    f"OPEN, not FAILED."
                )
                # Fixed 2026-08-21 (deep review): this branch sets the SAME
                # terminal OPEN state the main success path above does, but
                # skipped the matching add_deployed_capital() call -- a real,
                # live position the risk manager's per-strategy budget never
                # learned about, silently permitting more capital deployment
                # than the strategy's cap allows. Mirrors the exact condition
                # used at the main success path.
                if side == "BUY" and strategy_name and not is_spread_leg:
                    self.risk_manager.add_deployed_capital(strategy_name, quantity * price)
                return db_order
            # Reaching here means _reconcile_after_timeout() genuinely
            # checked the broker's order list and found no matching tag --
            # safe to conclude FAILED, unlike the except clause above.
            db_order = await self.order_repo.update(db_order, {"order_status": "FAILED"})
            await self._audit("ORDER_TIMEOUT", {"order_id": db_order.id, "symbol": symbol})
            return db_order
        except Exception as e:
            logger.error(f"Broker order placement failed: {e}")
            db_order = await self.order_repo.update(db_order, {"order_status": "FAILED"})
            await self._audit("ORDER_FAILED", {"order_id": db_order.id, "error": str(e)})
            return db_order

    async def _reconcile_after_timeout(self, internal_order_id: int) -> Optional[str]:
        """
        After a client-side place_order() timeout, check whether the broker
        actually processed the order anyway -- see the 2026-08-20 fix note
        in place_order()'s TimeoutError handler above. Matches by the same
        tag ZerodhaBroker.place_order() attaches (broker_order_tag()).
        PaperBroker orders never legitimately time out (synchronous,
        instant) and don't support tags, so this safely finds nothing for
        it -- effectively a Zerodha-only path.

        Fixed 2026-08-21 (external review): used to catch get_orders()
        failures itself and return None -- indistinguishable from "checked,
        genuinely not found." Now RAISES instead, so the caller can tell
        "couldn't verify" (exception) apart from "verified not there"
        (returns None), since those require opposite-risk responses -- see
        place_order()'s TimeoutError handler.
        """
        from src.core.utils import broker_order_tag
        tag = broker_order_tag(str(internal_order_id))
        broker_orders = await asyncio.wait_for(self.broker.get_orders(), timeout=BROKER_TIMEOUT_SEC)
        for o in broker_orders:
            if o.get("tag") == tag and o.get("status") not in ("REJECTED", "CANCELLED"):
                return str(o.get("order_id"))
        return None

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

    async def _retry_pending_verification_orders(self) -> None:
        """
        Fixed 2026-08-21 (external review): re-attempt broker verification
        for every order left PENDING_VERIFICATION by a prior timeout whose
        _reconcile_after_timeout() check itself failed (e.g. a transient
        connectivity blip -- get_orders() couldn't be reached, so the
        outcome was genuinely unknown and NOT safe to conclude FAILED; see
        place_order()'s TimeoutError handler). Called every cycle via
        expire_stale_orders(), same cadence as the rest of order-lifecycle
        bookkeeping.

        - Found live at the broker now -> correct to OPEN.
        - Verified genuinely not there (broker reachable, no matching tag)
          -> NOW safe to conclude FAILED, since this is a real, checked
          answer, not a guess.
        - Still can't verify -> leave as PENDING_VERIFICATION, log again
          next cycle. Never auto-concludes FAILED on an unverifiable
          outcome, no matter how many cycles it takes -- a stuck order here
          means a human needs to check the broker directly, not that the
          system should guess.
        """
        pending = await self.order_repo.filter(order_status="PENDING_VERIFICATION")
        for order in pending:
            try:
                reconciled_id = await self._reconcile_after_timeout(order.id)
            except Exception as e:
                logger.warning(
                    f"PENDING_VERIFICATION retry: order {order.id} still can't be "
                    f"verified ({e}) -- leaving as-is, will retry next cycle."
                )
                continue
            if reconciled_id:
                await self.order_repo.update(order, {
                    "broker_order_id": reconciled_id, "order_status": "OPEN",
                })
                await self._audit("ORDER_TIMEOUT_RECONCILED", {
                    "order_id": order.id, "broker_order_id": reconciled_id,
                })
                logger.warning(
                    f"PENDING_VERIFICATION retry: order {order.id} found live at "
                    f"the broker (id={reconciled_id}) -- corrected to OPEN."
                )
                # Fixed 2026-08-21 (deep review): same missing
                # add_deployed_capital() call as _reconcile_after_timeout()'s
                # inline branch in place_order() -- this loop runs on a LATER
                # cycle with no access to the original call's local
                # variables, so the retry-context lookup (not direct
                # parameters) is used to reconstruct whether capital should
                # have been added.
                await self._add_capital_if_should_have_been_deployed(order)
            else:
                await self.order_repo.update(order, {"order_status": "FAILED"})
                await self._audit("ORDER_TIMEOUT", {"order_id": order.id, "symbol": order.symbol})
                logger.warning(
                    f"PENDING_VERIFICATION retry: order {order.id} verified "
                    f"genuinely not at the broker -- now safe to mark FAILED."
                )

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
        # Fixed 2026-08-21 (external review): retry any order left in
        # PENDING_VERIFICATION by a prior timeout-verification failure (see
        # place_order()'s TimeoutError handler and _reconcile_after_timeout())
        # before touching OPEN orders below -- a fresh attempt to verify
        # against the broker now that this cycle is running.
        await self._retry_pending_verification_orders()

        # Fixed 2026-08-20 (deep review): reconcile against the broker's real
        # status FIRST. This used to go straight to cancel_order() below and
        # discard its return value entirely -- if the order had actually
        # already filled at the broker in the window since the last periodic
        # sync_orders() (up to ~1 minute, one signal cycle), cancel_order()
        # correctly returned False (nothing left to cancel), but that was
        # never checked: the order got marked EXPIRED anyway and then RETRIED,
        # placing a genuine duplicate order at the broker for an already-live
        # position, plus a capital double-count (release here, re-add on the
        # retry). Syncing first moves any order the broker reports as
        # COMPLETED/CANCELLED/REJECTED out of the "OPEN" bucket before the
        # query below ever sees it.
        await self.sync_orders()

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
            cancel_ok = True
            try:
                if order.broker_order_id:
                    cancel_ok = await asyncio.wait_for(
                        self.broker.cancel_order(order.broker_order_id),
                        timeout=BROKER_TIMEOUT_SEC,
                    )
            except (asyncio.TimeoutError, Exception) as e:
                logger.error(f"Broker cancel failed for order {order.id}: {e}")
                cancel_ok = False

            if not cancel_ok:
                # A failed cancel almost always means the broker no longer
                # considers the order cancellable -- i.e. it resolved
                # (filled/rejected/already cancelled) in the tiny window
                # between the sync_orders() call above and this cancel
                # attempt. Re-sync and trust whatever real terminal status
                # comes back rather than assuming "stale, safe to retry."
                await self.sync_orders()
                refreshed = await self.order_repo.get_by_id(order.id)
                if refreshed and refreshed.order_status != "OPEN":
                    logger.info(
                        f"Stale order {order.id} was already {refreshed.order_status} "
                        "at the broker — not expiring/retrying."
                    )
                    continue

            await self.order_repo.update(order, {
                "order_status": "EXPIRED",
                "updated_at":   now_ist().replace(tzinfo=None),
            })
            await self._audit("ORDER_EXPIRED", {"order_id": order.id, "symbol": order.symbol})
            cancelled += 1

            ctx = await self._get_retry_context(order.id)

            # Fixed 2026-08-21 (deep review): a stale order can be
            # PARTIALLY filled at the broker (a resting LIMIT order stays
            # OPEN while partially filled) -- sync_orders() above already
            # picked up the latest known filled_quantity before this order
            # was even fetched. Only the UNFILLED remainder's capital was
            # ever "at risk of never happening"; the filled portion is a
            # real position and must stay counted. Retrying (if eligible)
            # must resubmit only the remainder too, not the original full
            # quantity -- otherwise a 5-lot order that filled 1 lot and
            # rests would silently re-buy the full 5 lots on retry,
            # over-buying by the already-filled lot.
            _filled = getattr(order, "filled_quantity", None) or 0
            _remaining_qty = order.quantity - _filled
            if _filled > 0:
                logger.warning(
                    f"Stale order {order.id} ({order.symbol}) was PARTIALLY filled "
                    f"before expiry: {_filled}/{order.quantity}. Only the unfilled "
                    f"remainder ({_remaining_qty}) is eligible for capital release/retry."
                )

            # Release deployed capital tentatively added at submission — only
            # ever applies to the same case place_order() adds it for (BUY,
            # strategy_name set, not a spread/condor hedge leg). Independent
            # of whether we go on to retry below. Only the unfilled
            # remainder's capital is released -- see comment above.
            if (
                _remaining_qty > 0 and ctx and order.price is not None
                and order.side == "BUY" and ctx.get("strategy") and not ctx.get("is_spread_leg")
            ):
                self.risk_manager.release_deployed_capital(
                    ctx["strategy"], _remaining_qty * float(order.price)
                )

            eligible = (
                _remaining_qty > 0
                and ctx is not None
                and order.price is not None
                and not ctx.get("is_spread_leg")
                and not ctx.get("is_retry")
                and (ctx.get("is_exit_order") or ctx.get("strategy") not in _MULTI_LEG_STRATEGIES)
            )
            if eligible:
                retry_price = self._adjusted_retry_price(order.side, float(order.price))
                logger.info(
                    f"Retrying stale order {order.id}: {order.side} {_remaining_qty} "
                    f"{order.symbol} @ Rs{order.price} -> Rs{retry_price} (one attempt only)"
                )
                await self.place_order(
                    order.symbol, order.side, _remaining_qty, retry_price,
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
        updates: Dict[str, Any] = {}
        if existing_fill_price is None:
            b_fill = b_order.get("fill_price") or b_order.get("average_price")
            if b_fill:
                b_fill = float(b_fill)
                updates["fill_price"] = b_fill
                if expected_price:
                    updates["slippage"] = round(b_fill - expected_price, 4)
        # Fixed 2026-08-21 (deep review): filled_quantity tracked
        # independently of fill_price above -- a partial fill's quantity
        # can keep growing across later polls even after fill_price (the
        # average price of whatever's filled so far) was already recorded
        # once. Kite Connect reports this as "filled_quantity" on the order.
        b_filled_qty = b_order.get("filled_quantity")
        if b_filled_qty is not None:
            try:
                updates["filled_quantity"] = int(b_filled_qty)
            except (TypeError, ValueError):
                pass
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
                        # Fixed 2026-08-21 (deep review): deployed capital was
                        # only ever released via expire_stale_orders()'s
                        # EXPIRED path. An order that went OPEN (capital
                        # added) and is discovered HERE to have actually been
                        # REJECTED/CANCELLED at the broker -- an async RMS/
                        # margin rejection arriving after our own OPEN write --
                        # never re-enters the "order_status=OPEN" query this
                        # method filters on again, so that capital stayed
                        # permanently counted against the strategy's daily
                        # budget for the rest of the session. Release it here,
                        # the one place that actually observes this
                        # transition, using the retry-context lookup
                        # (order_received audit entry) to reconstruct whether
                        # add_deployed_capital() would have fired for this
                        # order in the first place -- same condition
                        # place_order() itself uses.
                        if new_status in ("REJECTED", "CANCELLED", "FAILED"):
                            await self._release_capital_if_was_deployed(db_order)
        except Exception as e:
            logger.error(f"Failed to sync orders: {e}")

    async def _release_capital_if_was_deployed(self, db_order: Order) -> None:
        """
        Release deployed capital for an order that add_deployed_capital()
        added when it first went OPEN (side=="BUY", strategy_name set,
        not a spread leg -- see place_order()'s own comment for why
        is_spread_leg is excluded), now that it's been discovered to have
        actually failed at the broker rather than filled. Looked up via the
        ORDER_RECEIVED audit context rather than new columns, matching the
        existing _get_retry_context() pattern.
        """
        if getattr(db_order, "side", None) != "BUY":
            return
        ctx = await self._get_retry_context(db_order.id)
        if not ctx:
            return
        strategy_name = ctx.get("strategy")
        is_spread_leg = ctx.get("is_spread_leg", False)
        if strategy_name and not is_spread_leg:
            price = float(db_order.price) if db_order.price else 0.0
            self.risk_manager.release_deployed_capital(strategy_name, db_order.quantity * price)

    async def _add_capital_if_should_have_been_deployed(self, db_order: Order) -> None:
        """
        Mirror of _release_capital_if_was_deployed() for the opposite
        direction: an order that timed out client-side, was left
        PENDING_VERIFICATION (outcome unknown), and is now confirmed to have
        actually succeeded at the broker on a LATER cycle -- it never went
        through place_order()'s normal add_deployed_capital() call at all,
        since that call never got to run before the timeout. Same lookup
        and same condition as the release side.
        """
        if getattr(db_order, "side", None) != "BUY":
            return
        ctx = await self._get_retry_context(db_order.id)
        if not ctx:
            return
        strategy_name = ctx.get("strategy")
        is_spread_leg = ctx.get("is_spread_leg", False)
        if strategy_name and not is_spread_leg:
            price = float(db_order.price) if db_order.price else 0.0
            self.risk_manager.add_deployed_capital(strategy_name, db_order.quantity * price)

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
