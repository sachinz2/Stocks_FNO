"""
Daily Zerodha <-> DB reconciliation (src/live_trading/zerodha_sync.py, 2026-08-13).

sync_orders() (order_manager.py) only reconciles orders our app already
has a DB row for. This covers the gap: a broker order with NO matching
DB record at all (crashed before the PENDING write persisted, a
GTT-triggered order that bypassed OrderManager, a manual trade placed
directly in the Zerodha app) must be inserted, not silently missed.
Zerodha wins on any mismatch (go-live decision: auto-correct, not just
alert).
"""
from datetime import datetime
import pytest

from src.live_trading.zerodha_sync import (
    sync_orders_from_zerodha, get_zerodha_capital, daily_zerodha_sync, _map_status,
)


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

    async def filter(self, limit=None, order_by=None, **kwargs):
        return [r for r in self.rows if all(getattr(r, k, None) == v for k, v in kwargs.items())]


class _FakeKite:
    def __init__(self, orders=None, margins=None):
        self._orders = orders or []
        self._margins = margins

    def orders(self):
        return self._orders

    def margins(self):
        return self._margins


def _today_ts():
    from src.core.utils import now_ist
    return now_ist().replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.mark.asyncio
async def test_inserts_broker_order_with_no_matching_db_record():
    repo = _FakeRepo()
    kite = _FakeKite(orders=[{
        "order_id": "BR-1", "order_timestamp": _today_ts(),
        "status": "COMPLETE", "tradingsymbol": "TITAN26SEP4650PE",
        "transaction_type": "SELL", "quantity": 175,
        "price": 82.0, "average_price": 79.54,
    }])

    result = await sync_orders_from_zerodha(kite, repo)

    assert result == {"checked": 1, "inserted": 1, "corrected": 0, "failed": 0}
    assert len(repo.rows) == 1
    row = repo.rows[0]
    assert row.broker_order_id == "BR-1"
    assert row.symbol == "TITAN26SEP4650PE"
    assert row.side == "SELL"
    assert row.quantity == 175
    assert row.fill_price == 79.54
    assert row.order_status == "COMPLETED"


@pytest.mark.asyncio
async def test_corrects_fill_price_mismatch_zerodha_wins():
    repo = _FakeRepo()
    existing = await repo.create({
        "broker_order_id": "BR-2", "symbol": "ITC26JUL285PE", "side": "SELL",
        "quantity": 1725, "price": 2.55, "fill_price": 2.50,  # our (wrong) record
        "order_status": "COMPLETED",
    })
    kite = _FakeKite(orders=[{
        "order_id": "BR-2", "order_timestamp": _today_ts(),
        "status": "COMPLETE", "tradingsymbol": "ITC26JUL285PE",
        "transaction_type": "SELL", "quantity": 1725,
        "price": 2.55, "average_price": 2.40,  # Zerodha's real fill
    }])

    result = await sync_orders_from_zerodha(kite, repo)

    assert result == {"checked": 1, "inserted": 0, "corrected": 1, "failed": 0}
    assert existing.fill_price == 2.40


@pytest.mark.asyncio
async def test_no_op_when_db_already_matches_zerodha():
    repo = _FakeRepo()
    await repo.create({
        "broker_order_id": "BR-3", "symbol": "TCS26AUG3800CE", "side": "BUY",
        "quantity": 225, "price": 48.0, "fill_price": 48.41,
        "order_status": "COMPLETED",
    })
    kite = _FakeKite(orders=[{
        "order_id": "BR-3", "order_timestamp": _today_ts(),
        "status": "COMPLETE", "tradingsymbol": "TCS26AUG3800CE",
        "transaction_type": "BUY", "quantity": 225,
        "price": 48.0, "average_price": 48.41,
    }])

    result = await sync_orders_from_zerodha(kite, repo)
    assert result == {"checked": 1, "inserted": 0, "corrected": 0, "failed": 0}


@pytest.mark.asyncio
async def test_ignores_orders_not_from_today():
    from datetime import timedelta
    repo = _FakeRepo()
    kite = _FakeKite(orders=[{
        "order_id": "BR-OLD", "order_timestamp": _today_ts() - timedelta(days=3),
        "status": "COMPLETE", "tradingsymbol": "SBIN26JUL800CE",
        "transaction_type": "BUY", "quantity": 500,
        "price": 10.0, "average_price": 10.5,
    }])

    result = await sync_orders_from_zerodha(kite, repo)
    assert result == {"checked": 0, "inserted": 0, "corrected": 0, "failed": 0}
    assert repo.rows == []


def test_map_status():
    assert _map_status("COMPLETE") == "COMPLETED"
    assert _map_status("CANCELLED") == "CANCELLED"
    assert _map_status("REJECTED") == "REJECTED"
    assert _map_status("OPEN") == "OPEN"
    assert _map_status("TRIGGER PENDING") == "OPEN"


@pytest.mark.asyncio
async def test_get_zerodha_capital_parses_margins_response():
    kite = _FakeKite(margins={
        "equity": {
            "net": 321556.0,
            "available": {"live_balance": 250000.0, "cash": 250000.0},
            "utilised": {"debits": 71556.0},
        }
    })
    capital = await get_zerodha_capital(kite)
    assert capital == {"capital_left": 250000.0, "capital_in_use": 71556.0}


@pytest.mark.asyncio
async def test_get_zerodha_capital_returns_none_on_broker_error():
    class _BrokenKite:
        def margins(self):
            raise RuntimeError("network error")
    assert await get_zerodha_capital(_BrokenKite()) is None


@pytest.mark.asyncio
async def test_daily_sync_is_noop_in_paper_mode_no_kite():
    # Must not raise -- paper mode has no real kite client at all.
    await daily_zerodha_sync(None, _FakeRepo())


# ── Scheduled job must gate on TradingMode.LIVE, not just "kite exists" (2026-08-13) ──
#
# A real, authenticated kite client is attached regardless of paper/live
# mode -- it's used for real VIX/option quotes even in paper mode (see
# main.py's "Always try Zerodha for market data if a token exists"). The
# job was originally gated only on `kite is not None`, which meant it
# would have run during paper trading too and pulled the REAL Zerodha
# account's actual orders into this app's `orders` table -- paper trades
# never touch the real account, so there's nothing there to legitimately
# reconcile, and doing so risked corrupting the paper trade history this
# app's own P&L numbers are read from. main.py's lifespan is too heavy
# (real DB/broker/redis wiring) to invoke directly in a unit test -- this
# is a static-source regression guard instead, matching this project's
# convention for other lifespan-embedded scheduled jobs.

def test_daily_zerodha_sync_job_gated_on_live_mode():
    import inspect
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    idx = src.index("async def _run_daily_zerodha_sync")
    job_body = src[idx:idx + 400]
    assert "if mode != TradingMode.LIVE:" in job_body
    assert "return" in job_body


# ── kite.orders()/kite.margins() must not block the event loop (2026-08-13) ──
#
# Every other KiteConnect call site in this codebase wraps the blocking
# REST call in run_in_executor. These two were missed when the file was
# new -- confirmed here by using a kite fake whose orders()/margins()
# assert they're NOT running on the main thread (run_in_executor moves
# them to a worker thread; a direct synchronous call would run on the
# event loop's own thread instead).

class _FakeThreadCheckingKite:
    def __init__(self, orders=None, margins=None):
        self._orders = orders or []
        self._margins = margins
        import threading
        self._main_thread = threading.current_thread()
        self.orders_called_off_main_thread = None
        self.margins_called_off_main_thread = None

    def orders(self):
        import threading
        self.orders_called_off_main_thread = threading.current_thread() is not self._main_thread
        return self._orders

    def margins(self):
        import threading
        self.margins_called_off_main_thread = threading.current_thread() is not self._main_thread
        return self._margins


@pytest.mark.asyncio
async def test_sync_orders_calls_kite_orders_via_executor_not_blocking():
    repo = _FakeRepo()
    kite = _FakeThreadCheckingKite(orders=[])
    await sync_orders_from_zerodha(kite, repo)
    assert kite.orders_called_off_main_thread is True


@pytest.mark.asyncio
async def test_get_zerodha_capital_calls_kite_margins_via_executor_not_blocking():
    kite = _FakeThreadCheckingKite(margins={
        "equity": {"available": {"live_balance": 1000.0}, "utilised": {"debits": 0}},
    })
    await get_zerodha_capital(kite)
    assert kite.margins_called_off_main_thread is True


# ── per-order error isolation (2026-08-13) ──────────────────────────────────
#
# One order's DB failure used to abort reconciliation of every order after
# it in that day's list, with no indication of what was skipped -- the
# same bug class this same day's other loops (square-off, expiry
# force-close) were explicitly guarded against, missed here since this
# file was new.

class _FailOnceRepo(_FakeRepo):
    def __init__(self, fail_for_broker_id):
        super().__init__()
        self._fail_for = fail_for_broker_id

    async def create(self, data):
        if data.get("broker_order_id") == self._fail_for:
            raise RuntimeError("simulated DB write failure")
        return await super().create(data)


@pytest.mark.asyncio
async def test_one_bad_order_does_not_block_reconciling_the_rest():
    repo = _FailOnceRepo(fail_for_broker_id="BR-BAD")
    kite = _FakeKite(orders=[
        {
            "order_id": "BR-BAD", "order_timestamp": _today_ts(),
            "status": "COMPLETE", "tradingsymbol": "SBIN26AUG800CE",
            "transaction_type": "BUY", "quantity": 500,
            "price": 10.0, "average_price": 10.5,
        },
        {
            "order_id": "BR-GOOD", "order_timestamp": _today_ts(),
            "status": "COMPLETE", "tradingsymbol": "TCS26AUG3800CE",
            "transaction_type": "BUY", "quantity": 225,
            "price": 48.0, "average_price": 48.41,
        },
    ])

    result = await sync_orders_from_zerodha(kite, repo)

    assert result == {"checked": 2, "inserted": 1, "corrected": 0, "failed": 1}
    assert len(repo.rows) == 1
    assert repo.rows[0].broker_order_id == "BR-GOOD"
