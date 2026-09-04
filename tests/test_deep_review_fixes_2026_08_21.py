"""
Fixes from the 2026-08-21 nine-component deep review ("fix all" pass).
Covers the core trading-engine fixes implemented directly (not the ones
handled by the four parallel fix agents, which have their own test files).
"""
import inspect
import pytest

from src.market_data.option_chain import find_delta_strike, bs_delta


# ── find_delta_strike: enforce_min_otm opt-out for intentional ITM entries ──

def test_find_delta_strike_default_still_clamps_otm():
    strike = find_delta_strike(1000, 0.60, "CE", 5, 0.35, 10)
    d = bs_delta(1000, strike, 5 / 365, 0.35, "CE")
    assert strike == 1010
    assert d < 0.50  # OTM


def test_find_delta_strike_enforce_min_otm_false_allows_itm_fit():
    strike = find_delta_strike(1000, 0.60, "CE", 5, 0.35, 10, enforce_min_otm=False)
    d = bs_delta(1000, strike, 5 / 365, 0.35, "CE")
    assert strike == 990
    assert abs(d - 0.60) < 0.05  # genuinely close to the target delta, ITM


def test_find_delta_strike_enforce_min_otm_false_put_side():
    strike = find_delta_strike(1000, -0.60, "PE", 5, 0.35, 10, enforce_min_otm=False)
    d = bs_delta(1000, strike, 5 / 365, 0.35, "PE")
    assert abs(d - (-0.60)) < 0.05


def test_engine_momentum_entry_path_opts_out_of_otm_clamp():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._process_signal)
    idx = src.index("target = _delta_target if option_type")
    block = src[idx:idx + 300]
    assert "enforce_min_otm=False" in block


# ── get_entry_prices_for_spread: fail closed on inverted/unreliable quotes ──

def test_get_entry_prices_for_spread_returns_none_on_inversion():
    import asyncio
    from src.market_data.option_chain import get_entry_prices_for_spread

    async def _fake_quote(contract, kite, redis):
        # short leg quotes cheaper than the long leg -- inverted, impossible
        # for a genuine credit spread.
        return 5.0 if "SHORT" in contract else 8.0

    import src.market_data.option_chain as oc
    orig = oc.get_option_quote
    oc.get_option_quote = _fake_quote
    try:
        result = asyncio.run(
            get_entry_prices_for_spread("RELIANCE", "RELIANCE-SHORT", "RELIANCE-LONG", None, None, atr=10, dte=5)
        )
    finally:
        oc.get_option_quote = orig
    assert result is None


def test_engine_credit_spread_skips_entry_on_none_prices():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("_entry_prices = await get_entry_prices_for_spread")
    block = src[idx:idx + 400]
    assert "if _entry_prices is None" in block


def test_engine_iron_condor_skips_entry_on_none_prices():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("_put_prices = await get_entry_prices_for_spread")
    block = src[idx:idx + 500]
    assert "if _put_prices is None or _call_prices is None" in block


# ── run_signal_cycle: fresh positions snapshot after spread/condor exits ───

def test_check_open_option_exits_uses_post_exit_positions_snapshot():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine.run_signal_cycle)
    exits_idx = src.index("await self._check_condor_exits(active_strategies)")
    refetch_idx = src.index("positions = await self._safe_get_positions()", exits_idx)
    open_exits_idx = src.index("await self._check_open_option_exits(positions, active_strategies)")
    # the re-fetch must happen strictly between the spread/condor exits and
    # the single-leg exit check that consumes `positions`.
    assert exits_idx < refetch_idx < open_exits_idx


# ── Per-underlying exception isolation in spread/condor exit loops ─────────

def test_check_spread_exits_isolates_exceptions_per_underlying():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._check_spread_exits)
    loop_idx = src.index("for underlying, spread in self._active_spreads.items():")
    block = src[loop_idx:]
    assert "try:" in block
    assert "except Exception as exc:" in block


def test_check_condor_exits_isolates_exceptions_per_underlying():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    loop_idx = src.index("for underlying, c in self._active_condors.items():")
    block = src[loop_idx:]
    assert "try:" in block
    assert "except Exception as exc:" in block


def test_spread_exit_exception_in_one_underlying_does_not_abort_others():
    """A raising get_market_data() for one underlying must not stop the
    other underlyings in the same _check_spread_exits() cycle from being
    checked."""
    import asyncio
    from unittest.mock import AsyncMock
    from src.live_trading.live_trading_engine import LiveTradingEngine

    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine._active_spreads = {
        "BAD": {"expiry_date": "2099-01-01", "short_contract": "X", "long_contract": "Y",
                "option_type": "PE", "short_strike": 100, "long_strike": 90,
                "short_premium": 5, "long_premium": 2, "lot_size": 1,
                "spread_type": "BULL_PUT_SPREAD"},
        "GOOD": {"expiry_date": "2099-01-01", "short_contract": "X2", "long_contract": "Y2",
                 "option_type": "PE", "short_strike": 100, "long_strike": 90,
                 "short_premium": 5, "long_premium": 2, "lot_size": 1,
                 "spread_type": "BULL_PUT_SPREAD"},
    }
    engine._close_on_first_cycle = set()
    engine._redis = None

    calls = []

    async def _fake_get_market_data(underlying):
        calls.append(underlying)
        if underlying == "BAD":
            raise RuntimeError("simulated data error")
        return None  # GOOD: no market data -> loop's own `continue`, reached only if BAD didn't abort it

    engine._get_market_data = _fake_get_market_data

    asyncio.run(engine._check_spread_exits({}))

    assert set(calls) == {"BAD", "GOOD"}, (
        "an exception while processing BAD must not prevent GOOD from being checked"
    )


# ── OrderRequest / RiskManager: reject non-positive quantity/price ─────────

def test_order_request_rejects_non_positive_quantity_and_price():
    import pytest
    from pydantic import ValidationError
    from src.api.dto.schemas import OrderRequest

    for bad in (
        dict(symbol="RELIANCE", side="BUY", quantity=0, price=100.0),
        dict(symbol="RELIANCE", side="BUY", quantity=-5, price=100.0),
        dict(symbol="RELIANCE", side="BUY", quantity=5, price=0.0),
        dict(symbol="RELIANCE", side="BUY", quantity=5, price=-1.0),
    ):
        with pytest.raises(ValidationError):
            OrderRequest(**bad)

    OrderRequest(symbol="RELIANCE", side="BUY", quantity=5, price=100.0)  # valid, no raise


def test_risk_manager_validate_trade_rejects_non_positive_quantity_price():
    from src.risk.risk_manager import RiskManager
    rm = RiskManager(initial_capital=300_000.0)
    assert rm.validate_trade("RELIANCE", "BUY", quantity=0, price=100.0, strategy_name="momentum_v1") is False
    assert rm.validate_trade("RELIANCE", "BUY", quantity=-5, price=100.0, strategy_name="momentum_v1") is False
    assert rm.validate_trade("RELIANCE", "BUY", quantity=5, price=0.0, strategy_name="momentum_v1") is False


# ── risk_router: read-only real limits, mutations explicitly unimplemented ─

def test_risk_router_rules_endpoint_reads_real_risk_manager_state():
    import asyncio
    from types import SimpleNamespace
    from src.api.routers.risk_router import get_risk_rules
    from src.risk.risk_manager import RiskManager

    rm = RiskManager(initial_capital=300_000.0)
    fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        trading_engine=SimpleNamespace(risk_manager=rm)
    )))
    result = asyncio.run(get_risk_rules(fake_request))
    rule_names = {r["rule"] for r in result}
    assert "max_daily_loss_pct" in rule_names
    assert "kill_switch_active" in rule_names


def test_risk_router_mutations_return_501_not_fake_success():
    import inspect
    from src.api.routers import risk_router
    for fn_name in ("create_risk_rule", "update_risk_rule", "delete_risk_rule"):
        src = inspect.getsource(getattr(risk_router, fn_name))
        assert "501" in src or "NOT_IMPLEMENTED" in src


# ── PENDING_VERIFICATION is never treated as a successful exit ─────────────

def test_all_six_exit_sites_no_longer_treat_pending_verification_as_closed():
    """Static check: every exit call site's order_status gate now excludes
    PENDING_VERIFICATION explicitly, or routes through
    _resolve_pending_exit_order()/_pending_exits() first."""
    from src.live_trading.live_trading_engine import LiveTradingEngine
    for method_name in (
        "_execute_single_leg_exit", "_close_option_positions",
        "_check_spread_exits", "_check_condor_exits",
    ):
        src = inspect.getsource(getattr(LiveTradingEngine, method_name))
        assert "PENDING_VERIFICATION" in src, f"{method_name} doesn't handle PENDING_VERIFICATION"


def test_execute_single_leg_exit_does_not_finalize_on_pending_verification():
    """Behavioral test: a fresh PENDING_VERIFICATION order must not be
    journaled as closed, capital must not be released, and the pending
    order id must be remembered instead of being silently dropped."""
    import asyncio
    from types import SimpleNamespace
    from src.live_trading.live_trading_engine import LiveTradingEngine

    class _PendingOrder:
        id = 4242
        order_status = "PENDING_VERIFICATION"
        fill_price = None

    class _FakeOrderManager:
        async def place_order(self, *a, **kw):
            return _PendingOrder()

    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine.order_manager = _FakeOrderManager()
    engine.risk_manager = SimpleNamespace(release_deployed_capital=lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("capital must not be released while the order is still PENDING_VERIFICATION")))
    engine._single_leg_journals = {"TITAN26AUG4975CE": {"strategy_name": "momentum_v1"}}
    engine._peak_premiums = {}
    engine._pending_single_leg_exit_orders = {}
    persisted = []
    async def _fake_persist():
        persisted.append(dict(engine._pending_single_leg_exit_orders))
    engine._persist_state = _fake_persist

    result = asyncio.run(engine._execute_single_leg_exit(
        "TITAN26AUG4975CE", 175, 46.59, 48.93, "test exit"
    ))

    assert result is False
    assert "TITAN26AUG4975CE" in engine._single_leg_journals, "journal entry must NOT be popped while pending"
    assert engine._pending_single_leg_exit_orders.get("TITAN26AUG4975CE") == 4242
    assert any("TITAN26AUG4975CE" in p for p in persisted), "pending order id must be persisted"


def test_execute_single_leg_exit_checks_resolved_pending_order_before_resubmitting():
    """On the next call, a still-PENDING order must be re-checked via
    order_repo, not resubmitted as a brand-new order."""
    import asyncio
    from types import SimpleNamespace
    from src.live_trading.live_trading_engine import LiveTradingEngine

    place_order_calls = []

    class _FakeOrderRepo:
        async def get_by_id(self, order_id):
            assert order_id == 4242
            return SimpleNamespace(id=4242, order_status="PENDING_VERIFICATION", fill_price=None)

    class _FakeOrderManager:
        order_repo = _FakeOrderRepo()
        async def place_order(self, *a, **kw):
            place_order_calls.append((a, kw))
            raise AssertionError("must not resubmit while the prior order is still PENDING_VERIFICATION")

    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine.order_manager = _FakeOrderManager()
    engine.risk_manager = SimpleNamespace()
    engine._single_leg_journals = {"TITAN26AUG4975CE": {"strategy_name": "momentum_v1"}}
    engine._peak_premiums = {}
    engine._pending_single_leg_exit_orders = {"TITAN26AUG4975CE": 4242}
    async def _fake_persist():
        pass
    engine._persist_state = _fake_persist

    result = asyncio.run(engine._execute_single_leg_exit(
        "TITAN26AUG4975CE", 175, 46.59, 48.93, "test exit"
    ))

    assert result is False
    assert place_order_calls == []
    assert engine._pending_single_leg_exit_orders.get("TITAN26AUG4975CE") == 4242


# ── Sector-concentration snapshot no longer goes stale within a cycle ──────

def test_order_manager_appends_new_entry_to_current_open_positions():
    """A successful non-exit, non-spread-leg order must immediately update
    risk_manager.current_open_positions so the NEXT entry in the same cycle
    sees it -- not just at the start of the next cycle."""
    import asyncio
    from src.risk.risk_manager import RiskManager
    from src.orders.order_manager import OrderManager

    class _FakeBroker:
        async def place_order(self, *a, **kw):
            return "broker-order-1"

    class _FakeRepo:
        def __init__(self):
            self.rows = []
        async def create(self, data):
            from types import SimpleNamespace
            row = SimpleNamespace(id=len(self.rows) + 1, **data)
            self.rows.append(row)
            return row
        async def update(self, obj, data):
            for k, v in data.items():
                setattr(obj, k, v)
            return obj

    class _FakeAuditRepo:
        async def create(self, data):
            return None

    rm = RiskManager(initial_capital=300_000.0)
    om = OrderManager(order_repo=_FakeRepo(), audit_repo=_FakeAuditRepo(), broker=_FakeBroker(), risk_manager=rm)

    assert rm.current_open_positions == []
    asyncio.run(om.place_order("HDFCBANK25AUG1600CE", "BUY", 550, 45.0, strategy_name="momentum_v1"))
    assert any(p["symbol"] == "HDFCBANK25AUG1600CE" for p in rm.current_open_positions), (
        "current_open_positions must reflect the just-placed entry immediately, "
        "not only after the next full-cycle broker refresh"
    )


def test_order_manager_does_not_track_exit_or_spread_leg_orders_as_new_positions():
    import asyncio
    from src.risk.risk_manager import RiskManager
    from src.orders.order_manager import OrderManager

    class _FakeBroker:
        async def place_order(self, *a, **kw):
            return "broker-order-1"

    class _FakeRepo:
        def __init__(self):
            self.rows = []
        async def create(self, data):
            from types import SimpleNamespace
            row = SimpleNamespace(id=len(self.rows) + 1, **data)
            self.rows.append(row)
            return row
        async def update(self, obj, data):
            for k, v in data.items():
                setattr(obj, k, v)
            return obj

    class _FakeAuditRepo:
        async def create(self, data):
            return None

    rm = RiskManager(initial_capital=300_000.0)
    om = OrderManager(order_repo=_FakeRepo(), audit_repo=_FakeAuditRepo(), broker=_FakeBroker(), risk_manager=rm)

    asyncio.run(om.place_order("X26AUG100CE", "SELL", 100, 10.0, is_exit_order=True))
    asyncio.run(om.place_order("Y26AUG100PE", "BUY", 100, 10.0, is_spread_leg=True, strategy_name="credit_spread_v1"))
    assert rm.current_open_positions == []


# ── Capital-basis fixes: on_market_open rebuild, partial-close reconcile, expiry force-close ──

def test_on_market_open_rebuild_uses_real_fill_basis_not_net_credit():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._rebuild_deployed_capital)
    assert 'get("short_premium", 0) - _s.get("long_premium", 0)' in src
    assert 'get("net_credit"' not in src


def test_on_market_open_calls_rebuild_deployed_capital():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine.on_market_open)
    assert "await self._rebuild_deployed_capital()" in src


def test_start_calls_rebuild_deployed_capital_after_restore():
    """A mid-day restart must rebuild deployed capital too, not just the
    fixed 09:15 on_market_open() job."""
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine.start)
    restore_idx = src.index("await self._restore_state()")
    rebuild_idx = src.index("await self._rebuild_deployed_capital()")
    assert restore_idx < rebuild_idx


def test_rebuild_deployed_capital_covers_single_leg_positions_too():
    """Single-leg (ema_crossover_v1/momentum_v1) capital must be rebuilt
    from real broker positions, not left uncovered."""
    import asyncio
    from types import SimpleNamespace
    from src.live_trading.live_trading_engine import LiveTradingEngine

    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine._active_spreads = {}
    engine._active_condors = {}
    engine._single_leg_journals = {
        "TITAN26AUG4975CE": {"strategy_name": "momentum_v1", "underlying": "TITAN"},
    }
    engine.risk_manager = SimpleNamespace(_added=[])
    engine.risk_manager.add_deployed_capital = lambda strategy, amount: engine.risk_manager._added.append((strategy, amount))

    async def _fake_positions():
        return [{"symbol": "TITAN26AUG4975CE", "quantity": 175, "avg_price": 46.59}]
    engine._safe_get_positions = _fake_positions

    asyncio.run(engine._rebuild_deployed_capital())

    assert engine.risk_manager._added == [("momentum_v1", pytest.approx(175 * 46.59))]


def test_partial_close_reconcile_uses_real_fill_basis_not_net_credit():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._reconcile_partially_closed_multi_leg_legs)
    assert 's.get("short_premium", 0) - s.get("long_premium", 0)' in src
    assert 'c.get("net_credit"' not in src
    assert 's.get("net_credit"' not in src


def test_expiry_force_close_uses_real_fill_basis_not_net_credit():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._square_off_all)
    assert "_fill_credit" in src
    assert "_fill_credit_c" in src


# ── order_manager: capital released/added on REJECTED / timeout-reconciled ─

def test_sync_orders_releases_capital_when_order_discovered_rejected():
    import asyncio
    from types import SimpleNamespace
    from src.risk.risk_manager import RiskManager
    from src.orders.order_manager import OrderManager

    class _Repo:
        def __init__(self, rows):
            self.rows = rows
        async def filter(self, **kw):
            if kw.get("order_status") == "OPEN":
                return [r for r in self.rows if r.order_status == "OPEN"]
            if kw.get("action") == "ORDER_RECEIVED":
                return self.audit_rows
            return []
        async def update(self, obj, data):
            for k, v in data.items():
                setattr(obj, k, v)
            return obj
        async def create(self, data):
            return None

    row = SimpleNamespace(
        id=99, symbol="TATASTEEL26AUG150CE", side="BUY", quantity=1350,
        price=12.5, fill_price=None, order_status="OPEN", broker_order_id="bo-99",
    )
    repo = _Repo([row])
    repo.audit_rows = [SimpleNamespace(payload={
        "order_id": 99, "strategy": "momentum_v1", "is_exit_order": False, "is_spread_leg": False,
    })]

    class _Broker:
        async def get_orders(self):
            return [{"order_id": "bo-99", "status": "REJECTED"}]

    rm = RiskManager(initial_capital=300_000.0)
    rm.add_deployed_capital("momentum_v1", 1350 * 12.5)
    assert rm._strategy_deployed["momentum_v1"] == 1350 * 12.5

    om = OrderManager(_Broker(), rm, repo, repo)
    asyncio.run(om.sync_orders())

    assert rm._strategy_deployed["momentum_v1"] == 0.0, (
        "capital must be released once sync_orders() discovers the order was actually REJECTED"
    )


def test_timeout_reconcile_adds_capital_when_order_found_live_at_broker():
    """place_order()'s inline _reconcile_after_timeout() success branch must
    add_deployed_capital(), matching the normal success path."""
    from src.orders.order_manager import OrderManager
    import inspect
    src = inspect.getsource(OrderManager.place_order)
    idx = src.index("if reconciled_id:")
    block = src[idx:idx + 900]
    assert "add_deployed_capital" in block


def test_pending_verification_retry_adds_capital_when_resolved_live():
    from src.orders.order_manager import OrderManager
    import inspect
    src = inspect.getsource(OrderManager._retry_pending_verification_orders)
    assert "_add_capital_if_should_have_been_deployed" in src


# ── filled_quantity: partial fills tracked, retry uses only the remainder ──

def test_order_model_has_filled_quantity_column():
    from src.database.models.order import Order
    assert hasattr(Order, "filled_quantity")


def test_migration_b008_is_the_new_head_after_b007():
    import importlib
    b008 = importlib.import_module("migrations.versions.b008_add_filled_quantity_to_orders")
    assert b008.down_revision == "b007"
    assert b008.revision == "b008"


def test_extract_fill_updates_tracks_filled_quantity_independently_of_fill_price():
    from src.orders.order_manager import OrderManager
    # filled_quantity must update regardless of whether fill_price changed --
    # a partial fill's quantity keeps growing across later polls. Uses the
    # SAME broker-reported fill_price as existing_fill_price here (no real
    # price change this poll) specifically to isolate that filled_quantity's
    # update is independent of any fill_price change -- see
    # test_extract_fill_updates_refreshes_fill_price_on_a_growing_partial_fill
    # (2026-09-04) for the fill_price-changes-too case.
    updates = OrderManager._extract_fill_updates(
        {"fill_price": 48.5, "filled_quantity": 300}, expected_price=48.0, existing_fill_price=48.5,
    )
    assert updates == {"filled_quantity": 300}


def test_extract_fill_updates_refreshes_fill_price_on_a_growing_partial_fill():
    """Fixed 2026-09-04 (live incident): fill_price used to be captured ONCE
    and never refreshed -- correct for PaperBroker's atomic fills, but wrong
    for a real Zerodha order that fills in multiple tranches at different
    average prices. Every downstream consumer (P&L, daily-loss kill switch,
    StrategyMonitor's rolling PF) trusts fill_price as authoritative, so a
    frozen stale price silently corrupts all of them the first time a real
    limit order fills in more than one poll."""
    from src.orders.order_manager import OrderManager
    # Poll 1 recorded fill_price=99.50 from a partial fill. Poll 2: the order
    # has filled further (or completed) at a different average -- must overwrite.
    updates = OrderManager._extract_fill_updates(
        {"fill_price": 100.75, "filled_quantity": 750}, expected_price=100.0, existing_fill_price=99.50,
    )
    assert updates["fill_price"] == 100.75
    assert updates["slippage"] == round(100.75 - 100.0, 4)
    assert updates["filled_quantity"] == 750


def test_extract_fill_updates_is_a_noop_when_fill_price_is_unchanged():
    """Guard against over-fixing: an unchanged average price must not
    generate a spurious update/slippage recompute every single poll."""
    from src.orders.order_manager import OrderManager
    updates = OrderManager._extract_fill_updates(
        {"fill_price": 99.50, "filled_quantity": 300}, expected_price=100.0, existing_fill_price=99.50,
    )
    assert "fill_price" not in updates
    assert "slippage" not in updates
    assert updates == {"filled_quantity": 300}


def test_paper_broker_orders_report_full_filled_quantity():
    import asyncio
    from src.paper_trading.paper_broker import PaperBroker
    broker = PaperBroker(initial_balance=300_000.0)
    asyncio.run(broker.place_order("RELIANCE26AUG1400CE", "BUY", 250, 40.0))
    orders = asyncio.run(broker.get_orders())
    assert orders[-1]["filled_quantity"] == 250


def test_stale_order_retry_uses_unfilled_remainder_not_full_quantity():
    """A stale order that was PARTIALLY filled (filled_quantity < quantity)
    must retry only the unfilled remainder, and release capital only for
    that remainder -- not the full original quantity."""
    import asyncio
    from datetime import datetime, timedelta
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
        async def place_order(self, symbol, side, qty, price, is_exit_order=False,
                               strategy_name=None, product_override=None, client_order_id=None):
            self.placed.append((symbol, side, qty, price))
            return f"bo-{len(self.placed)}"
        async def cancel_order(self, order_id):
            return True
        async def get_positions(self):
            return []
        async def get_orders(self):
            return []  # sync_orders() finds nothing further -- filled_quantity stays as set below

    order_repo = _FakeRepo()
    rm = RiskManager(initial_capital=300_000.0)
    om = OrderManager(_FakeBroker(), rm, order_repo, order_repo)

    db_order = asyncio.run(om.place_order(
        "SBIN26AUG800CE", "BUY", 500, 40.0, strategy_name="momentum_v1",
    ))
    # Simulate: 200 of the 500 requested already filled at the broker
    # (a resting LIMIT order, still status=OPEN) before it went stale.
    db_order.filled_quantity = 200
    db_order.created_at = datetime.utcnow() - timedelta(minutes=ORDER_EXPIRY_MINUTES + 1)

    # Intercept the release call directly -- expire_stale_orders() both
    # releases (for the unfilled remainder) AND, via the retry's own
    # place_order() call, re-adds capital for the NEW retry order's actual
    # quantity*price -- so the net before/after delta on its own isn't a
    # clean signal of what was released.
    released_calls = []
    _orig_release = rm.release_deployed_capital
    rm.release_deployed_capital = lambda strategy, amount: (
        released_calls.append((strategy, amount)), _orig_release(strategy, amount)
    )[-1]

    asyncio.run(om.expire_stale_orders())

    # Fixed 2026-09-04 (live incident): release now uses the SAME price
    # basis (the adjusted retry price) that the retry's own place_order()
    # call re-adds capital at -- previously this released at the ORIGINAL
    # order.price (40.0) while the retry re-added at the adjusted
    # ~1.5%-moved price, a persistent capital-accounting drift on every
    # retry. Only the unfilled remainder's qty is used either way.
    from src.orders.order_manager import RETRY_PRICE_ADJUSTMENT
    _retry_price = round(40.0 * (1 + RETRY_PRICE_ADJUSTMENT), 2)
    assert released_calls == [("momentum_v1", pytest.approx(300 * _retry_price))], (
        "only the unfilled remainder's qty must be released, at the SAME price basis "
        "the retry re-adds capital at, not the full original quantity or a mismatched price"
    )

    # The retry (second place_order call) must request only the remainder,
    # at the same adjusted price used for the release above.
    broker = om.broker
    assert len(broker.placed) == 2  # original + retry
    _, _, retry_qty, retry_order_price = broker.placed[-1]
    assert retry_qty == 300
    assert retry_order_price == pytest.approx(_retry_price)


# ── IV rank fed real market IV, not ATR-derived historical vol ─────────────

def test_get_iv_rank_accepts_and_prefers_live_sigma():
    import asyncio
    from unittest.mock import AsyncMock, patch
    from src.live_trading.live_trading_engine import LiveTradingEngine

    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine._redis = object()  # truthy, only used as a not-None check by the mocked calls below

    with patch("src.market_data.option_chain.update_iv_history", new=AsyncMock()) as mock_update, \
         patch("src.market_data.option_chain.get_iv_rank", new=AsyncMock(return_value=0.42)):
        result = asyncio.run(engine._get_iv_rank("RELIANCE", 2500.0, 25.0, 20, live_sigma=0.31))

    assert result == 0.42
    # The real market sigma (0.31), not an ATR-derived proxy, must be what
    # gets recorded into the IV history.
    mock_update.assert_awaited_once_with("RELIANCE", 0.31, engine._redis)


def test_credit_spread_computes_live_sigma_before_iv_rank():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    sigma_idx = src.index("sigma      = await self._get_live_sigma(")
    iv_rank_idx = src.index("iv_rank    = await self._get_iv_rank(")
    assert sigma_idx < iv_rank_idx
    assert "live_sigma=sigma" in src


def test_iron_condor_computes_live_sigma_before_iv_rank():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    sigma_idx = src.index("sigma      = await self._get_live_sigma(")
    iv_rank_idx = src.index("iv_rank    = await self._get_iv_rank(")
    assert sigma_idx < iv_rank_idx
    assert "live_sigma=sigma" in src


# ── walk_forward / robustness: real strategy, not a hardcoded EMA proxy ────

def _synthetic_daily_ohlcv(n=400, seed=7):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    steps = rng.normal(0.15, 2.0, size=n)
    close = 1000 + np.cumsum(steps)
    close = np.maximum(close, 50)
    high = close + rng.uniform(0.5, 3.0, size=n)
    low  = close - rng.uniform(0.5, 3.0, size=n)
    openp = close + rng.normal(0, 1.0, size=n)
    volume = rng.integers(100_000, 500_000, size=n)
    df = pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )
    return df


def test_walk_forward_resolves_instance_id_and_registry_key_forms():
    from src.backtesting.walk_forward import WalkForwardTester
    for name in ("momentum_v1", "MOMENTUM", "ema_crossover_v1", "EMA_CROSSOVER"):
        tester = WalkForwardTester(strategy_name=name, param_grid={"fast_period": [20], "slow_period": [50]})
        assert tester._resolve_strategy_registry_key() is not None, name


def test_walk_forward_credit_spread_and_unknown_names_are_not_supported():
    from src.backtesting.walk_forward import WalkForwardTester
    for name in ("credit_spread_v1", "CREDIT_SPREAD", "iron_condor_v1", "IRON_CONDOR", "nonsense_strategy"):
        tester = WalkForwardTester(strategy_name=name, param_grid={"fast_period": [20], "slow_period": [50]})
        assert tester._resolve_strategy_registry_key() is None, name


def test_walk_forward_simulate_raises_for_multi_leg_strategy():
    from src.backtesting.walk_forward import WalkForwardTester, StrategyNotSimulatableError
    tester = WalkForwardTester(strategy_name="credit_spread_v1", param_grid={"fast_period": [20], "slow_period": [50]})
    df = tester._add_indicators(_synthetic_daily_ohlcv())
    with pytest.raises(StrategyNotSimulatableError):
        tester._simulate(df, {"fast_period": 20, "slow_period": 50})


def test_walk_forward_run_returns_not_supported_for_multi_leg_without_hitting_kite():
    import asyncio
    from src.backtesting.walk_forward import WalkForwardTester

    class _ExplodingKite:
        def historical_data(self, *a, **kw):
            raise AssertionError("must not fetch history for an unsupported strategy")

    tester = WalkForwardTester(
        strategy_name="credit_spread_v1", param_grid={"fast_period": [20], "slow_period": [50]},
        kite=_ExplodingKite(), instrument_tokens={"RELIANCE": 123},
    )
    results = asyncio.run(tester.run(symbol="RELIANCE", start_year=2020, end_year=2024))
    assert results == []
    assert tester.not_supported_reason is not None
    summary = tester.summary(results)
    assert summary["verdict"] == "NOT_SUPPORTED"
    assert "profit_factor" not in summary
    assert "degradation_ratio" not in summary


def test_walk_forward_simulate_actually_calls_the_real_strategy():
    """The old bug: this always ran a hardcoded EMA crossover regardless of
    strategy_name. Confirm generate_signal() on the REAL registered
    strategy class is what's actually invoked."""
    from unittest.mock import patch
    from src.backtesting.walk_forward import WalkForwardTester
    from src.strategies.momentum import MomentumStrategy

    tester = WalkForwardTester(strategy_name="momentum_v1", param_grid={"fast_period": [20], "slow_period": [50]})
    df = tester._add_indicators(_synthetic_daily_ohlcv())

    call_count = 0
    original = MomentumStrategy.generate_signal

    def _counting_wrapper(self, data):
        nonlocal call_count
        call_count += 1
        return original(self, data)

    with patch.object(MomentumStrategy, "generate_signal", _counting_wrapper):
        tester._simulate(df, {"fast_period": 20, "slow_period": 50})
        assert call_count > 0, "the real MomentumStrategy.generate_signal must actually be called"


def test_walk_forward_simulate_unregisters_instance_after_running():
    from src.strategies.base import StrategyRegistry
    from src.backtesting.walk_forward import WalkForwardTester

    tester = WalkForwardTester(strategy_name="ema_crossover_v1", param_grid={"fast_period": [20], "slow_period": [50]})
    df = tester._add_indicators(_synthetic_daily_ohlcv())
    before = set(StrategyRegistry.get_active_strategies().keys())
    tester._simulate(df, {"fast_period": 20, "slow_period": 50})
    after = set(StrategyRegistry.get_active_strategies().keys())
    assert after == before, "the simulation instance must not leak into the live strategy registry"


def test_robustness_analyzer_same_fixes_present():
    from src.backtesting.robustness import ParameterRobustnessAnalyzer
    for name in ("momentum_v1", "MOMENTUM"):
        analyzer = ParameterRobustnessAnalyzer(strategy_name=name, param_grid={"fast_period": [20], "slow_period": [50]})
        assert analyzer._resolve_strategy_registry_key() is not None
    for name in ("credit_spread_v1", "iron_condor_v1"):
        analyzer = ParameterRobustnessAnalyzer(strategy_name=name, param_grid={"fast_period": [20], "slow_period": [50]})
        assert analyzer._resolve_strategy_registry_key() is None


def test_robustness_analyze_returns_not_supported_for_multi_leg_without_hitting_kite():
    import asyncio
    from src.backtesting.robustness import ParameterRobustnessAnalyzer

    class _ExplodingKite:
        def historical_data(self, *a, **kw):
            raise AssertionError("must not fetch history for an unsupported strategy")

    analyzer = ParameterRobustnessAnalyzer(
        strategy_name="iron_condor_v1", param_grid={"fast_period": [20], "slow_period": [50]},
        kite=_ExplodingKite(), instrument_tokens={"RELIANCE": 123},
    )
    result = asyncio.run(analyzer.analyze(symbol="RELIANCE", years=3))
    assert result["verdict"] == "NOT_SUPPORTED"
    assert "robustness_ratio" not in result


# ── analytics_router: walk-forward/robustness endpoints were missing the
# `request` parameter entirely while reading request.app.state, a separate
# pre-existing bug (NameError on every call) found while fixing the above ──

def test_walk_forward_and_robustness_endpoints_declare_request_param():
    import inspect
    from src.api.routers import analytics_router
    for fn in (analytics_router.run_walk_forward, analytics_router.run_robustness_check):
        params = inspect.signature(fn).parameters
        assert "request" in params, f"{fn.__name__} reads request.app.state but never declares `request`"


def test_walk_forward_endpoint_surfaces_not_supported_instead_of_generic_no_results():
    src_module = __import__("inspect").getsource(
        __import__("src.api.routers.analytics_router", fromlist=["run_walk_forward"]).run_walk_forward
    )
    assert "not_supported_reason" in src_module


# ── Notifications: email timeout, ComboNotifier wired in production ────────

def test_email_service_smtp_has_socket_timeout():
    src = inspect.getsource(__import__("src.notifications.email_service", fromlist=["EmailNotifier"]))
    assert "smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=" in src


def test_email_service_send_is_bounded_by_asyncio_wait_for():
    from src.notifications.email_service import EmailNotifier
    src = inspect.getsource(EmailNotifier.send)
    assert "asyncio.wait_for" in src


def test_email_send_actually_times_out_instead_of_hanging():
    """send() itself must return within its own timeout budget. Measured via
    a manually-driven loop (not asyncio.run()) because asyncio.run()'s
    shutdown phase waits for the default ThreadPoolExecutor's threads to
    finish -- including the deliberately-hung one this test spawns, which
    isn't cancellable once started -- which would make the TEST's own
    cleanup slow without indicating send() itself hung."""
    import asyncio
    import time
    from src.notifications.email_service import EmailNotifier, _SEND_OVERALL_TIMEOUT_SEC

    notifier = EmailNotifier.__new__(EmailNotifier)
    notifier.sender = "x@example.com"
    notifier.password = "pw"
    notifier.recipient = "y@example.com"
    notifier.enabled = True
    notifier.paused = False

    def _hang(subject, body):
        time.sleep(_SEND_OVERALL_TIMEOUT_SEC + 30)
        return True
    notifier._send_blocking = _hang

    loop = asyncio.new_event_loop()
    try:
        start = time.monotonic()
        result = loop.run_until_complete(notifier.send("subject line\nbody"))
        elapsed = time.monotonic() - start
    finally:
        # Deliberately not closing the loop/executor -- the hung background
        # thread from _hang() is not cancellable; closing here would block
        # this test on the same non-issue asyncio.run() would.
        pass

    assert result is False
    assert elapsed < _SEND_OVERALL_TIMEOUT_SEC + 5, f"send() must not hang past its own timeout, took {elapsed:.1f}s"


def test_main_py_wires_combo_notifier_not_bare_email_notifier():
    import inspect
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    assert "notifier = ComboNotifier()" in src
    assert "notifier = EmailNotifier()" not in src


def test_combo_notifier_exposes_paused_and_enabled_for_admin_router():
    """admin_router's /email-alerts endpoints read/write notifier.paused and
    notifier.enabled directly -- ComboNotifier must expose the same surface
    as the bare EmailNotifier() it replaced."""
    from src.notifications.combo_notifier import ComboNotifier
    notifier = ComboNotifier.__new__(ComboNotifier)
    from types import SimpleNamespace
    notifier.email = SimpleNamespace(paused=False, enabled=True)
    notifier.telegram = SimpleNamespace(enabled=False)

    assert notifier.enabled is True
    assert notifier.paused is False
    notifier.paused = True
    assert notifier.email.paused is True


# ── Dashboard: real MAX_DAILY_LOSS_PCT, capital-deployed no longer double-counts ──

def test_dashboard_reads_max_daily_loss_pct_from_env_not_hardcoded():
    src = inspect.getsource(__import__("importlib").import_module("src.dashboard.app"))
    assert 'MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT"' in src


def test_estimate_capital_deployed_nets_grouped_legs_instead_of_double_counting():
    from src.dashboard.app import _estimate_capital_deployed
    positions = [
        # credit spread: short leg (signed qty negative), long leg (positive)
        {"symbol": "X26AUG100PE", "quantity": -750, "avg_price": 20.0, "group_id": "spread:X"},
        {"symbol": "X26AUG80PE",  "quantity": 750,  "avg_price": 5.0,  "group_id": "spread:X"},
        # standalone single-leg
        {"symbol": "Y26AUG200CE", "quantity": 500, "avg_price": 30.0, "group_id": None},
    ]
    result = _estimate_capital_deployed(positions)
    # Old buggy behavior summed both legs unsigned: 750*20 + 750*5 + 500*30 = 33750
    # Fixed behavior nets the grouped legs: |(-750*20) + (750*5)| + 500*30 = 11250 + 15000 = 26250
    old_buggy_value = 750 * 20.0 + 750 * 5.0 + 500 * 30.0
    assert result < old_buggy_value
    assert result == pytest.approx(abs(-750 * 20.0 + 750 * 5.0) + 500 * 30.0)


# ── Scheduler: daily cron jobs have an explicit misfire grace, not the 1s default ──

def test_scheduler_daily_jobs_have_misfire_grace_time():
    src = inspect.getsource(__import__("importlib").import_module("src.core.scheduler"))
    assert "_DAILY_JOB_MISFIRE_GRACE_SEC" in src
    for job_id in ('id=JOB_MARKET_OPEN', 'id="gap_check"', 'id=JOB_MARKET_CLOSE',
                   'id=JOB_DAILY_PNL', 'id="capital_period_rollover"', 'id="zerodha_daily_auth"'):
        idx = src.index(job_id)
        block = src[idx:idx + 300]
        assert "misfire_grace_time" in block, f"{job_id} missing misfire_grace_time"


# ── backtest_router: honest 501, not fabricated numbers ────────────────────

def test_backtest_router_endpoints_return_501_not_fake_data():
    import asyncio
    from fastapi import HTTPException
    from src.api.routers.backtest_router import run_backtest, get_backtest_result
    from src.api.dto.schemas import BacktestRunRequest

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run_backtest(BacktestRunRequest(
            strategy="momentum_v1", symbol="RELIANCE", start_date="2024-01-01", end_date="2024-06-01",
        )))
    assert exc_info.value.status_code == 501

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_backtest_result(12345))
    assert exc_info.value.status_code == 501
