"""
OrderManager behavior not covered by tests/test_orders.py:
  - is_spread_leg BUY legs (hedge/long legs of a spread or condor) must NOT
    trigger the automatic add_deployed_capital hook -- that's owned
    explicitly by the engine's own max-loss-based accounting, and used to
    double-/triple-count when both fired.
  - expire_stale_orders() real retry-at-adjusted-price: scoped to
    single-leg orders only, bounded to exactly one attempt, with correct
    deployed-capital release/re-add.
  - place_order() reconciles the real broker fill immediately (2026-08-07
    fix) instead of only through the separately-scheduled sync_orders() job.
"""
import asyncio
from datetime import datetime, timedelta
import pytest

from src.risk.risk_manager import RiskManager
from src.orders.order_manager import OrderManager, ORDER_EXPIRY_MINUTES


class _Row:
    _next_id = 1

    def __init__(self, **kw):
        self.id = _Row._next_id
        _Row._next_id += 1
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
    def __init__(self):
        self.placed = []
        self.cancelled = []

    async def place_order(self, symbol, side, qty, price):
        oid = f"bo-{len(self.placed) + 1}"
        self.placed.append((symbol, side, qty, price))
        return oid

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    async def get_positions(self):
        return []

    async def get_orders(self):
        return []


async def _make_stale_order(om, order_repo, **kwargs):
    """Place an order the normal way, then age it past ORDER_EXPIRY_MINUTES."""
    db_order = await om.place_order(**kwargs)
    db_order.created_at = datetime.utcnow() - timedelta(minutes=ORDER_EXPIRY_MINUTES + 1)
    return db_order


# ── Double-count suppression (2026-07-30) ───────────────────────────────────

def test_place_order_excludes_spread_legs_from_auto_deployed_capital_source():
    import inspect
    src = inspect.getsource(OrderManager.place_order)
    assert "not is_spread_leg" in src


@pytest.mark.asyncio
async def test_hedge_leg_does_not_double_count_deployed_capital():
    rm = RiskManager(initial_capital=300_000.0)
    om = OrderManager(_FakeBroker(), rm, _FakeRepo(), _FakeRepo())

    # Short leg (SELL, is_spread_leg=False) with explicit capital_at_risk --
    # mirrors _process_credit_spread's short_order call. add_deployed_capital
    # isn't called by validate_trade itself; the engine calls it explicitly
    # after both legs succeed -- simulate that here.
    await om.place_order(
        "TESTCE", "SELL", 25, 10.0,
        is_spread_leg=False, strategy_name="credit_spread_v1", capital_at_risk=3750.0,
    )
    rm.add_deployed_capital("credit_spread_v1", 3750.0)

    # Long/hedge leg (BUY, is_spread_leg=True) -- must NOT auto-add anything.
    await om.place_order(
        "TESTPE", "BUY", 25, 3.0,
        is_spread_leg=True, strategy_name="credit_spread_v1",
    )

    deployed = rm.get_deployed_by_strategy()["credit_spread_v1"]
    assert deployed == 3750.0, "expected exactly 3750.0, no double-count from the hedge leg"


# ── Stale-order retry (expire_stale_orders) ─────────────────────────────────

@pytest.mark.asyncio
async def test_single_leg_buy_retries_once_with_capital_release_and_readd():
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    await _make_stale_order(
        om, order_repo,
        symbol="SBIN26AUG800CE", side="BUY", quantity=25, price=100.0,
        strategy_name="ema_crossover_v1",
    )
    assert rm.get_deployed_by_strategy().get("ema_crossover_v1", 0.0) == 2500.0  # 25*100

    cancelled = await om.expire_stale_orders()

    assert cancelled == 1
    assert len(broker.cancelled) == 1, "original order must be cancelled at the broker"
    assert len(broker.placed) == 2, "expected original + 1 retry placement"
    _, _, _, retry_price = broker.placed[-1]
    assert abs(retry_price - 101.5) < 0.01, "retry price should be ~1.5% toward the market"

    # Released 2500 (original) then re-added 25*101.5=2537.5 (retry) = net 2537.5
    deployed_after = rm.get_deployed_by_strategy().get("ema_crossover_v1", 0.0)
    assert abs(deployed_after - 2537.5) < 0.01

    retry_order = order_repo.rows[-1]
    assert retry_order.order_status == "OPEN"


@pytest.mark.asyncio
async def test_multi_leg_anchor_sell_leg_is_never_retried():
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    await _make_stale_order(
        om, order_repo,
        symbol="INFY26AUG1800PE", side="SELL", quantity=25, price=15.0,
        is_spread_leg=False, strategy_name="credit_spread_v1", capital_at_risk=3000.0,
    )
    cancelled = await om.expire_stale_orders()

    assert cancelled == 1
    assert len(broker.placed) == 1, "credit_spread_v1 anchor leg must NOT be retried"
    # The auto-add never fired for this SELL leg (only BUY legs trigger it),
    # so nothing should be released either.
    assert rm.get_deployed_by_strategy().get("credit_spread_v1", 0.0) == 0.0


@pytest.mark.asyncio
async def test_a_retry_that_itself_goes_stale_is_not_retried_again():
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    await _make_stale_order(
        om, order_repo,
        symbol="TCS26AUG3800CE", side="BUY", quantity=25, price=50.0,
        strategy_name="momentum_v1",
    )
    await om.expire_stale_orders()  # first pass: cancels original, places 1 retry
    assert len(broker.placed) == 2

    order_repo.rows[-1].created_at = datetime.utcnow() - timedelta(minutes=ORDER_EXPIRY_MINUTES + 1)
    cancelled = await om.expire_stale_orders()

    assert cancelled == 1, "the stale retry itself must be cancelled"
    assert len(broker.placed) == 2, "must NOT place a second retry (bounded to one attempt)"


@pytest.mark.asyncio
async def test_plain_exit_order_is_retried_with_no_capital_side_effects():
    rm = RiskManager(initial_capital=300_000.0)
    order_repo = _FakeRepo()
    broker = _FakeBroker()
    om = OrderManager(broker, rm, order_repo, _FakeRepo())

    await _make_stale_order(
        om, order_repo,
        symbol="SBIN26AUG800CE", side="SELL", quantity=25, price=120.0,
        is_exit_order=True,
    )
    await om.expire_stale_orders()

    assert len(broker.placed) == 2, "plain exit order must be retried"
    assert rm.get_deployed_by_strategy() == {}


# ── Immediate fill reconciliation (2026-08-07) ──────────────────────────────

class _FakeOrderObj:
    """Duck-typed stand-in for the SQLAlchemy Order model."""
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.price = kwargs.get("price")
        self.fill_price = kwargs.get("fill_price")
        self.order_status = kwargs.get("order_status")
        for k, v in kwargs.items():
            setattr(self, k, v)


class _ImmediateFillOrderRepo:
    """Mirrors BaseRepository.create/update: update() returns a NEW merged
    object (real repo's documented contract)."""
    def __init__(self):
        self._next_id = 1

    async def create(self, data):
        obj = _FakeOrderObj(id=self._next_id, **data)
        self._next_id += 1
        return obj

    async def update(self, obj, data):
        merged = {k: getattr(obj, k) for k in vars(obj)}
        merged.update(data)
        return _FakeOrderObj(**merged)


class _FakeAuditRepo:
    async def create(self, data):
        return None


class _SynchronousFillBroker:
    """Mirrors PaperBroker: place_order() fills SYNCHRONOUSLY and stores the
    real fill in an internal dict, but returns only a bare order ID -- the
    exact shape of the 2026-08-07 bug (fill_price was only ever picked up
    later, by sync_orders()'s separately-scheduled reconciliation, so every
    caller reading db_order.fill_price immediately after place_order()
    returns was always reading it before that job had run)."""
    def __init__(self, real_fill_price):
        self._real_fill_price = real_fill_price
        self._orders = {}

    async def place_order(self, symbol, side, quantity, price):
        order_id = "paper-order-1"
        self._orders[order_id] = {
            "order_id": order_id, "symbol": symbol, "side": side,
            "quantity": quantity, "price": price,
            "fill_price": self._real_fill_price, "status": "COMPLETED",
        }
        return order_id  # bare ID only

    async def get_orders(self):
        return list(self._orders.values())


@pytest.mark.asyncio
async def test_place_order_reconciles_real_fill_immediately():
    entry_fill = 5.29  # real TATACONSUM-style fill, distinct from the quote
    quoted_price = 4.95

    om = OrderManager(
        _SynchronousFillBroker(entry_fill),
        RiskManager(initial_capital=300_000.0),
        _ImmediateFillOrderRepo(), _FakeAuditRepo(),
    )

    db_order = await om.place_order(
        "POWERGRID26AUG270PE", "BUY", 1900, quoted_price, strategy_name="momentum_v1",
    )

    # This is exactly what live_trading_engine.py's entry/exit code does
    # immediately after place_order() returns -- before the fix, fill_price
    # was always None here.
    assert db_order.fill_price is not None
    assert abs(db_order.fill_price - entry_fill) < 0.001, (
        f"fill_price={db_order.fill_price} should be the real fill, not the quote {quoted_price}"
    )
    assert db_order.order_status == "OPEN"
    assert db_order.slippage is not None
    assert abs(db_order.slippage - (entry_fill - quoted_price)) < 0.001


@pytest.mark.asyncio
async def test_place_order_falls_back_gracefully_when_fill_unavailable():
    # A real (non-instant-fill) broker with a genuinely still-pending order:
    # get_orders() correctly returns no fill yet, and this must not crash --
    # sync_orders() picks it up later, same as before this fix.
    class _PendingBroker:
        async def place_order(self, symbol, side, quantity, price):
            return "broker-order-pending"
        async def get_orders(self):
            return [{"order_id": "broker-order-pending", "status": "OPEN"}]  # no fill_price yet

    om = OrderManager(
        _PendingBroker(), RiskManager(initial_capital=300_000.0),
        _ImmediateFillOrderRepo(), _FakeAuditRepo(),
    )

    db_order = await om.place_order("RELIANCE", "BUY", 10, 2500.0)

    assert db_order.order_status == "OPEN"
    assert db_order.fill_price is None
