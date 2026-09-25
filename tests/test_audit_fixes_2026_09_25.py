"""
Fixes from the 2026-09-25 "hunt for other logical bugs" audit (three
parallel focused reviews of capital/margin, exit management, and order
lifecycle/idempotency -- prompted by the IV-history starvation incident
the same day). Each fix below closes a real, verified gap found by that
audit, not a speculative one.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.risk.risk_manager import RiskManager
from src.orders.order_manager import OrderManager, ORDER_EXPIRY_MINUTES


class _Row:
    _next_id = 1

    def __init__(self, **kw):
        self.id = _Row._next_id
        _Row._next_id += 1
        # Match the real Order model's nullable columns -- both default to
        # None until a fill is confirmed. Without this, _extract_fill_updates()
        # raises AttributeError the first time a test's fake broker returns a
        # real order (unlike test_order_manager_extended.py's fake, whose
        # get_orders() always returns [] and never reaches that code path).
        self.fill_price = None
        self.filled_quantity = None
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeRepo:
    def __init__(self):
        self.rows = []

    async def create(self, data):
        row = _Row(**data)
        self.rows.append(row)
        return row

    async def update(self, obj, data):
        for k, v in data.items():
            setattr(obj, k, v)
        return obj

    async def get_by_id(self, id):
        return next((r for r in self.rows if r.id == id), None)

    async def filter(self, limit=None, order_by=None, **kwargs):
        out = [r for r in self.rows if all(getattr(r, k, None) == v for k, v in kwargs.items())]
        if order_by:
            col, _, direction = order_by.partition(" ")
            out.sort(key=lambda r: getattr(r, col), reverse=(direction.upper() == "DESC"))
        if limit is not None:
            out = out[:limit]
        return out


class _FakeBroker:
    """Unlike test_order_manager_extended.py's fake, get_orders() can be
    scripted to return a DIFFERENT response on successive calls -- needed
    to simulate a fill landing between two sync points in the same
    expire_stale_orders() pass."""

    def __init__(self):
        self.placed = []
        self.cancelled = []
        self._orders_responses = []
        self._get_orders_calls = 0

    async def place_order(self, symbol, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None, client_order_id=None):
        oid = f"bo-{len(self.placed) + 1}"
        self.placed.append((symbol, side, qty, price))
        return oid

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    async def get_positions(self):
        return []

    async def get_orders(self):
        if not self._orders_responses:
            return []
        idx = min(self._get_orders_calls, len(self._orders_responses) - 1)
        self._get_orders_calls += 1
        return self._orders_responses[idx]


async def _make_stale_order(om, order_repo, **kwargs):
    db_order = await om.place_order(**kwargs)
    db_order.created_at = datetime.utcnow() - timedelta(minutes=ORDER_EXPIRY_MINUTES + 1)
    return db_order


# ── 1. Duplicate-order guard (place_order) ──────────────────────────────────

@pytest.mark.asyncio
async def test_place_order_refuses_a_duplicate_when_one_is_already_open():
    """A resting, unfilled entry order for the exact same contract must
    block a second one -- this is the gap that could double a position
    across an abrupt restart (os._exit(1) watchdog paths bypass the
    graceful _persist_state() shutdown entirely)."""
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    first = await om.place_order(
        "RELIANCE26SEP2900CE", "BUY", 500, 42.0, strategy_name="ema_crossover_v1",
    )
    assert first is not None and first.order_status == "OPEN"

    second = await om.place_order(
        "RELIANCE26SEP2900CE", "BUY", 500, 42.5, strategy_name="ema_crossover_v1",
    )

    assert second is None
    assert len(broker.placed) == 1, "must not reach the broker a second time"


@pytest.mark.asyncio
async def test_place_order_still_allows_exit_orders_despite_a_resting_entry():
    """An exit must never be blocked by this guard -- e.g. closing an
    already-filled portion while a resting remainder of the same original
    order is still outstanding is legitimate, not a duplicate."""
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    await om.place_order("RELIANCE26SEP2900CE", "BUY", 500, 42.0, strategy_name="ema_crossover_v1")

    exit_order = await om.place_order(
        "RELIANCE26SEP2900CE", "SELL", 500, 45.0, is_exit_order=True,
    )

    assert exit_order is not None
    assert exit_order.order_status == "OPEN"
    assert len(broker.placed) == 2


@pytest.mark.asyncio
async def test_place_order_allows_a_new_order_once_the_prior_one_is_no_longer_open():
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    first = await om.place_order("RELIANCE26SEP2900CE", "BUY", 500, 42.0, strategy_name="ema_crossover_v1")
    await order_repo.update(first, {"order_status": "COMPLETED"})

    second = await om.place_order("RELIANCE26SEP2900CE", "BUY", 500, 42.5, strategy_name="ema_crossover_v1")

    assert second is not None
    assert second.order_status == "OPEN"
    assert len(broker.placed) == 2


@pytest.mark.asyncio
async def test_place_order_duplicate_guard_is_scoped_per_symbol():
    """A resting order on one contract must not block an entry on a
    completely different one."""
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    await om.place_order("RELIANCE26SEP2900CE", "BUY", 500, 42.0, strategy_name="ema_crossover_v1")
    other = await om.place_order("TCS26SEP3900PE", "BUY", 300, 55.0, strategy_name="momentum_v1")

    assert other is not None
    assert other.order_status == "OPEN"


# ── 2. Capital release must use the UNFILLED remainder, not full quantity ──

@pytest.mark.asyncio
async def test_cancel_order_releases_only_the_unfilled_remainder():
    """Fixed 2026-09-25 (audit finding): _release_capital_if_was_deployed()
    used to release db_order.quantity * price -- the full originally
    requested size -- even when part of the order had already filled. The
    filled portion is a real open position that will correctly release its
    own capital again on its normal exit; releasing it here too
    double-counts it as 'available'."""
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    order = await om.place_order(
        "SBIN26AUG800CE", "BUY", 10, 100.0, strategy_name="ema_crossover_v1",
    )
    assert rm.get_deployed_by_strategy()["ema_crossover_v1"] == 1000.0  # 10*100

    order.filled_quantity = 4  # 4 of 10 lots already filled at the broker

    ok = await om.cancel_order(order.id)

    assert ok is True
    # Only the unfilled remainder (10-4=6) * 100 = 600 released, leaving
    # 1000 - 600 = 400 still counted for the 4 filled lots' real capital.
    deployed_after = rm.get_deployed_by_strategy().get("ema_crossover_v1", 0.0)
    assert abs(deployed_after - 400.0) < 0.01


@pytest.mark.asyncio
async def test_cancel_order_releases_full_amount_when_nothing_filled():
    """Sanity check: the ordinary case (filled_quantity None/0) must still
    release the full original amount, unchanged."""
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    order = await om.place_order(
        "SBIN26AUG800CE", "BUY", 10, 100.0, strategy_name="ema_crossover_v1",
    )
    assert rm.get_deployed_by_strategy()["ema_crossover_v1"] == 1000.0

    await om.cancel_order(order.id)

    assert rm.get_deployed_by_strategy().get("ema_crossover_v1", 0.0) == 0.0


@pytest.mark.asyncio
async def test_sync_orders_uses_the_freshly_updated_order_not_a_stale_snapshot():
    """Fixed 2026-09-25 (audit finding): _sync_orders_locked() discarded
    order_repo.update()'s return value, so _release_capital_if_was_deployed()
    downstream read filled_quantity off the STALE pre-update object even
    though `updates` (built the same iteration) carried the broker's real,
    just-synced value for this exact OPEN->CANCELLED transition."""
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    order = await om.place_order(
        "SBIN26AUG800CE", "BUY", 10, 100.0, strategy_name="ema_crossover_v1",
    )
    assert rm.get_deployed_by_strategy()["ema_crossover_v1"] == 1000.0

    # Broker now reports this order CANCELLED with 4/10 lots filled --
    # discovered by sync_orders(), not by our own cancel_order() call.
    broker._orders_responses = [[{
        "order_id": "bo-1", "status": "CANCELLED", "filled_quantity": 4,
        "average_price": 100.0,
    }]]

    await om.sync_orders()

    deployed_after = rm.get_deployed_by_strategy().get("ema_crossover_v1", 0.0)
    assert abs(deployed_after - 400.0) < 0.01, (
        "must release only the unfilled 6 lots (600), leaving 400 for the "
        "4 real filled lots -- not release the full 1000 off a stale snapshot"
    )


# ── 3. Stale-order retry must re-sync before computing the remainder ───────

@pytest.mark.asyncio
async def test_expire_stale_orders_resyncs_filled_quantity_before_retry():
    """Race: the top-of-method sync_orders() sees 2/5 filled (still OPEN).
    Before THIS order's own cancel_order() broker round-trip completes, 2
    more lots fill (4/5), leaving 1 lot resting -- which cancel legitimately
    cancels. The retry must resubmit only the true remaining 1 lot, computed
    from a re-sync AFTER the cancel succeeds, not the stale top-of-method
    snapshot (which would wrongly compute 5-2=3)."""
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    await _make_stale_order(
        om, order_repo,
        symbol="SBIN26AUG800CE", side="BUY", quantity=5, price=100.0,
        strategy_name="ema_crossover_v1",
    )

    broker._orders_responses = [
        # Call 1: top-of-method sync_orders() -- still OPEN, 2/5 filled.
        [{"order_id": "bo-1", "status": "OPEN", "filled_quantity": 2, "average_price": 100.0}],
        # Call 2: this order's own post-cancel re-sync -- now CANCELLED, 4/5 filled.
        [{"order_id": "bo-1", "status": "CANCELLED", "filled_quantity": 4, "average_price": 100.0}],
    ]

    cancelled = await om.expire_stale_orders()

    assert cancelled == 1
    assert len(broker.placed) == 2, "expected original + 1 retry"
    _, _, retry_qty, _ = broker.placed[-1]
    assert retry_qty == 1, f"retry must resubmit the true remaining 1 lot (5-4), got {retry_qty}"
