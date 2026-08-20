"""
Defense-in-depth round (2026-08-20), same day as the component-level health
audit. After that audit's verdict, the user asked to close 4 items that were
deliberately deferred as lower-priority (gaps, not active bugs):
  1. No dead-man's-switch if the scheduler/event loop itself wedges.
  2. Strategies have no self-check for "I've stopped signaling" -- the same
     failure signature as the 2026-07-27..07-29 DTE-window incident.
  3. The kill switch / daily-loss limit are unit-tested per function but
     never exercised as a realistic multi-trade sequence.
  4. Zerodha broker calls block the asyncio event loop under load.
"""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.utils import now_ist


# ── 1. Scheduler dead-man's-switch (static-source, matches this codebase's ──
#    established pattern for main.py -- see test_zerodha_ticker.py's
#    test_watchdog_exits_process_on_staleness_not_inplace_reconnect).

def test_scheduler_deadman_switch_wired_into_lifespan():
    from pathlib import Path
    main_py = Path(__file__).resolve().parent.parent / "src" / "api" / "main.py"
    src = main_py.read_text(encoding="utf-8")

    assert "_scheduler_watchdog_thread" in src
    assert "threading.Thread(" in src
    assert 'daemon=True' in src

    idx = src.index("_scheduler_watchdog_thread")
    block = src[idx:idx + 2500]
    assert "os._exit(1)" in block, "watchdog must force a full process exit on a stale heartbeat"
    assert "time.sleep(" in block, "watchdog must be a real blocking-sleep loop, not an asyncio task"

    # The heartbeat job itself must be registered on the scheduler so a
    # wedged scheduler (jobs stop firing) actually starves the heartbeat.
    assert "scheduler_heartbeat" in src
    assert "scheduler.add_job(" in src.split("_scheduler_heartbeat_job")[1][:500]


# ── 2. Strategy "stopped signaling" self-check ───────────────────────────────

@pytest.mark.asyncio
async def test_process_signal_records_last_signal_date_on_a_real_signal(monkeypatch):
    from src.core.enums import SignalType

    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True,
        generate_signal=lambda md: SignalType.BUY,
    )
    fake = SimpleNamespace(
        _last_signal_date={},
        _get_market_data=AsyncMock(return_value={"ltp_source": "zerodha_live_ticks", "close": 100.0}),
        _max_daily_orders=0, _today_order_count=0,
        _has_active_multi_leg_structure=lambda symbol: True,  # short-circuit before any order logic
    )

    await LiveTradingEngine._process_signal(fake, strategy, "RELIANCE", vix=15.0, regime="TRENDING")

    assert fake._last_signal_date["ema_crossover_v1"] == now_ist().date().isoformat()


@pytest.mark.asyncio
async def test_signal_staleness_establishes_baseline_on_first_observation():
    """A strategy never seen by this check before gets today as its baseline
    instead of being silently skipped forever -- covers the 'zero signals
    since deployment' variant of the incident, not just 'went quiet later'."""
    notifications = []
    fake = SimpleNamespace(
        _last_signal_date={},
        _notify=AsyncMock(side_effect=lambda m: notifications.append(m)),
    )

    class _FakeStrategy:
        is_active = True

    import unittest.mock as _mock
    with _mock.patch(
        "src.live_trading.live_trading_engine.StrategyRegistry.get_active_strategies",
        return_value={"ema_crossover_v1": _FakeStrategy()},
    ):
        await LiveTradingEngine._check_signal_staleness(fake)

    assert fake._last_signal_date["ema_crossover_v1"] == now_ist().date().isoformat()
    assert notifications == []  # no alert on the very first day


@pytest.mark.asyncio
async def test_signal_staleness_alerts_after_the_threshold_with_no_signals():
    stale_date = (now_ist().date() - timedelta(days=LiveTradingEngine._SIGNAL_STALENESS_DAYS)).isoformat()
    notifications = []
    fake = SimpleNamespace(
        _last_signal_date={"ema_crossover_v1": stale_date},
        _notify=AsyncMock(side_effect=lambda m: notifications.append(m)),
        _SIGNAL_STALENESS_DAYS=LiveTradingEngine._SIGNAL_STALENESS_DAYS,
    )

    class _FakeStrategy:
        is_active = True

    import unittest.mock as _mock
    with _mock.patch(
        "src.live_trading.live_trading_engine.StrategyRegistry.get_active_strategies",
        return_value={"ema_crossover_v1": _FakeStrategy()},
    ):
        await LiveTradingEngine._check_signal_staleness(fake)

    assert len(notifications) == 1
    assert "ema_crossover_v1" in notifications[0]
    assert "ZERO trading signals" in notifications[0]


@pytest.mark.asyncio
async def test_signal_staleness_does_not_alert_a_recently_active_strategy():
    recent_date = (now_ist().date() - timedelta(days=1)).isoformat()
    notifications = []
    fake = SimpleNamespace(
        _last_signal_date={"ema_crossover_v1": recent_date},
        _notify=AsyncMock(side_effect=lambda m: notifications.append(m)),
        _SIGNAL_STALENESS_DAYS=LiveTradingEngine._SIGNAL_STALENESS_DAYS,
    )

    class _FakeStrategy:
        is_active = True

    import unittest.mock as _mock
    with _mock.patch(
        "src.live_trading.live_trading_engine.StrategyRegistry.get_active_strategies",
        return_value={"ema_crossover_v1": _FakeStrategy()},
    ):
        await LiveTradingEngine._check_signal_staleness(fake)

    assert notifications == []


@pytest.mark.asyncio
async def test_signal_staleness_skips_paused_strategies():
    """A strategy paused by StrategyMonitor/regime-switching legitimately
    produces zero signals -- must not spam an alert for an intentional pause."""
    stale_date = (now_ist().date() - timedelta(days=10)).isoformat()
    notifications = []
    fake = SimpleNamespace(
        _last_signal_date={"credit_spread_v1": stale_date},
        _notify=AsyncMock(side_effect=lambda m: notifications.append(m)),
    )

    class _FakePausedStrategy:
        is_active = False

    import unittest.mock as _mock
    with _mock.patch(
        "src.live_trading.live_trading_engine.StrategyRegistry.get_active_strategies",
        return_value={"credit_spread_v1": _FakePausedStrategy()},
    ):
        await LiveTradingEngine._check_signal_staleness(fake)

    assert notifications == []


@pytest.mark.asyncio
async def test_last_signal_date_persists_and_restores_across_restart():
    saved = {}

    class _FakeRedis:
        async def set(self, key, value, ex=None):
            saved[key] = value

        async def get(self, key):
            return saved.get(key)

    fake = SimpleNamespace(
        _redis=_FakeRedis(),
        _active_spreads={}, _active_condors={}, _single_leg_journals={},
        _exited_today=set(), _profit_closed_today=set(), _today_order_count=0,
        _peak_premiums={}, _last_signal_date={"momentum_v1": "2026-08-15"},
    )

    await LiveTradingEngine._persist_state(fake)

    fresh = SimpleNamespace(_redis=_FakeRedis(), _last_signal_date={})
    fresh._redis.get = fake._redis.get  # share the same backing store
    # Minimal restore of just the piece under test -- _restore_state() does a
    # lot more (broker reconciliation etc.) that needs a much heavier fixture;
    # exercised end-to-end elsewhere, this isolates the persist/restore pair.
    import json
    from src.live_trading.live_trading_engine import _REDIS_LAST_SIGNAL
    raw = await fresh._redis.get(_REDIS_LAST_SIGNAL)
    restored = json.loads(raw)
    assert restored == {"momentum_v1": "2026-08-15"}


# ── 3. Kill switch / daily-loss limit: realistic multi-trade sequence ───────

@pytest.mark.asyncio
async def test_kill_switch_trips_mid_sequence_blocks_entries_not_exits():
    """A realistic sequence: several entries succeed, losses accumulate,
    the daily-loss limit trips the kill switch mid-sequence -- subsequent
    entries must be blocked while an exit for an existing position still
    goes through, and deployed capital tracking stays consistent throughout."""
    from src.orders.order_manager import OrderManager
    from src.risk.risk_manager import RiskManager

    class _Row:
        _next_id = 1

        def __init__(self):
            self.id = _Row._next_id
            _Row._next_id += 1
            self.order_status = "PENDING"
            self.broker_order_id = None
            self.fill_price = None
            self.price = None
            self.side = None
            self.symbol = None
            self.quantity = None
            self.created_at = None

    class _Repo:
        def __init__(self):
            self.rows = []

        async def create(self, data):
            row = _Row()
            for k, v in data.items():
                setattr(row, k, v)
            self.rows.append(row)
            return row

        async def update(self, obj, updates):
            for k, v in updates.items():
                setattr(obj, k, v)
            return obj

        async def filter(self, **kw):
            return [r for r in self.rows if getattr(r, "order_status", None) == kw.get("order_status")]

        async def get_by_id(self, oid):
            for r in self.rows:
                if r.id == oid:
                    return r
            return None

    class _Broker:
        def __init__(self):
            self._n = 0

        async def place_order(self, symbol, side, quantity, price, is_exit_order=False,
                               strategy_name=None, product_override=None, client_order_id=None):
            self._n += 1
            return f"bo-{self._n}"

        async def get_orders(self):
            return []

        async def cancel_order(self, order_id):
            return True

    order_repo = _Repo()
    audit_repo = _Repo()
    rm = RiskManager(initial_capital=300_000.0, max_daily_loss_pct=0.05)  # 5% = Rs15,000 daily loss cap
    om = OrderManager(_Broker(), rm, order_repo, audit_repo)

    # Two winning-looking entries open fine (kill switch inactive).
    o1 = await om.place_order("SBIN26AUG800CE", "BUY", 25, 100.0, strategy_name="ema_crossover_v1")
    o2 = await om.place_order("TCS26AUG3800CE", "BUY", 25, 100.0, strategy_name="ema_crossover_v1")
    assert o1.order_status == "OPEN"
    assert o2.order_status == "OPEN"
    assert rm.get_deployed_by_strategy()["ema_crossover_v1"] == 25 * 100.0 * 2

    # A realized loss big enough to breach the 5% daily-loss limit comes in
    # (e.g. both positions stopped out for a combined Rs16,000 loss) --
    # update_state() is what _refresh_risk_state() feeds every cycle in the
    # real engine.
    rm.update_state(positions=[], realized_pnl=-16_000.0, unrealized_pnl=0.0)

    # A THIRD entry attempt must now be blocked -- validate_trade() is what
    # place_order() calls internally before ever reaching the broker.
    o3 = await om.place_order("INFY26AUG1800CE", "BUY", 25, 100.0, strategy_name="ema_crossover_v1")
    assert o3.order_status == "REJECTED_BY_RISK"

    # But closing an EXISTING position (is_exit_order=True) must still go
    # through -- the kill switch/daily-loss check bypasses exits by design
    # (layer 0 in RiskManager.validate_trade()), otherwise a tripped kill
    # switch would trap the account in its losing positions.
    exit_order = await om.place_order(
        "SBIN26AUG800CE", "SELL", 25, 90.0, is_exit_order=True, strategy_name="ema_crossover_v1",
    )
    assert exit_order.order_status == "OPEN"

    # Releasing capital for the exited leg must still correctly reduce the
    # per-strategy deployed-capital figure -- risk tracking doesn't freeze
    # just because the kill switch is active.
    rm.release_deployed_capital("ema_crossover_v1", 25 * 100.0)
    assert rm.get_deployed_by_strategy()["ema_crossover_v1"] == 25 * 100.0  # only o2's entry remains


# ── 4. Zerodha broker calls must not block the event loop ───────────────────

def _bare_zerodha_broker():
    from src.brokers.zerodha import ZerodhaBroker
    broker = ZerodhaBroker.__new__(ZerodhaBroker)
    kite = MagicMock()
    kite.VARIETY_REGULAR = "regular"
    kite.EXCHANGE_NFO = "NFO"
    kite.EXCHANGE_NSE = "NSE"
    kite.PRODUCT_MIS = "MIS"
    kite.PRODUCT_NRML = "NRML"
    kite.ORDER_TYPE_MARKET = "MARKET"
    kite.ORDER_TYPE_LIMIT = "LIMIT"
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"
    broker.kite = kite
    return broker


@pytest.mark.asyncio
async def test_place_order_offloads_the_synchronous_kite_call_to_a_thread(monkeypatch):
    broker = _bare_zerodha_broker()
    broker.kite.place_order.return_value = "order-1"

    calls = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, *a, **kw):
        calls.append(func)
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr("src.brokers.zerodha.asyncio.to_thread", _spy_to_thread)

    order_id = await broker.place_order("SBIN", "BUY", 100, 500.0)

    assert order_id == "order-1"
    assert broker.kite.place_order in calls


@pytest.mark.asyncio
async def test_get_positions_offloads_the_synchronous_kite_call_to_a_thread(monkeypatch):
    broker = _bare_zerodha_broker()
    broker.kite.positions.return_value = {"net": [{"symbol": "SBIN", "quantity": 25}]}

    calls = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, *a, **kw):
        calls.append(func)
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr("src.brokers.zerodha.asyncio.to_thread", _spy_to_thread)

    result = await broker.get_positions()

    assert result == [{"symbol": "SBIN", "quantity": 25}]
    assert broker.kite.positions in calls


@pytest.mark.asyncio
async def test_cancel_order_offloads_its_whole_retry_sequence_to_a_thread(monkeypatch):
    """_cancel_order_call() has its own blocking-sleep @retry backoff --
    the ENTIRE call (not just one kite.cancel_order() invocation) must run
    off the event loop, or a transient failure's retry delay blocks everything."""
    broker = _bare_zerodha_broker()
    broker.kite.cancel_order.return_value = None

    calls = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, *a, **kw):
        calls.append(func)
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr("src.brokers.zerodha.asyncio.to_thread", _spy_to_thread)

    result = await broker.cancel_order("order-1")

    assert result is True
    assert broker._cancel_order_call in calls
