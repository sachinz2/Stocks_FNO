"""
analytics_router.get_pnl_summary() (2026-08-13).

Two bugs found live while investigating why the dashboard's Positions/PnL
and Strategy pages looked wrong:

1. total_unrealized/today_unrealized read the MySQL `positions` table --
   nothing in the live trading path keeps that table current (confirmed:
   its most recent row was last updated 2026-07-24, and every row has
   quantity=0). This meant unrealized PnL was silently ALWAYS ZERO,
   completely missing every currently-open position's real unrealized PnL.
   Fixed to reuse positions_router's _positions_from_engine() -- the exact
   same live-engine-state source /positions itself uses.

2. "today" was computed via date.today() (the container's system-local
   date, confirmed UTC) instead of now_ist().date() -- same recurring
   "wrong timezone reference" bug class already fixed elsewhere in this
   project (order timestamps, regime timestamps).
"""
import sys
import types
import pytest


@pytest.fixture
def analytics_router(monkeypatch):
    """
    Import analytics_router with src.database.* stubbed out (same hermetic
    pattern as test_positions_router.py) and a fake positions_router module
    pre-registered in sys.modules, since get_pnl_summary() imports
    _positions_from_engine lazily (function-local import) -- pre-registering
    intercepts that import cleanly.
    """
    db_pkg = types.ModuleType("src.database"); db_pkg.__path__ = []
    conn_mod = types.ModuleType("src.database.connection")
    conn_mod.AsyncSessionLocal = object()

    models_pkg = types.ModuleType("src.database.models"); models_pkg.__path__ = []
    journal_mod = types.ModuleType("src.database.models.trade_journal")
    class TradeJournal: pass
    journal_mod.TradeJournal = TradeJournal

    stubs = {
        "src.database": db_pkg,
        "src.database.connection": conn_mod,
        "src.database.models": models_pkg,
        "src.database.models.trade_journal": journal_mod,
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    import importlib.util
    from pathlib import Path
    router_path = Path(__file__).resolve().parent.parent / "src" / "api" / "routers" / "analytics_router.py"
    spec = importlib.util.spec_from_file_location("analytics_router", router_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTrade:
    def __init__(self, strategy_name, pnl, exit_time):
        self.strategy_name = strategy_name
        self.pnl = pnl
        self.exit_time = exit_time


class _FakeRequest:
    def __init__(self, engine=None):
        class _State:
            trading_engine = engine
            redis = None
            kite = None
        class _App:
            state = _State()
        self.app = _App()


def _fake_positions_router_module(rows):
    async def _positions_from_engine(engine, kite, redis):
        return rows
    mod = types.ModuleType("src.api.routers.positions_router")
    mod._positions_from_engine = _positions_from_engine
    return mod


@pytest.mark.asyncio
async def test_unrealized_pnl_comes_from_live_engine_positions_not_stale_db_table(
    analytics_router, monkeypatch,
):
    from datetime import datetime
    from src.core.utils import now_ist

    async def _fake_closed_trades():
        return []
    monkeypatch.setattr(analytics_router, "_get_closed_trades", _fake_closed_trades)

    live_rows = [
        {"symbol": "BAJFINANCE26SEP1160CE", "quantity": -750, "unrealized_pnl": 1250.0},
        {"symbol": "BAJFINANCE26SEP1190CE", "quantity": 750, "unrealized_pnl": -300.0},
    ]
    fake_pr_mod = _fake_positions_router_module(live_rows)
    monkeypatch.setitem(sys.modules, "src.api.routers.positions_router", fake_pr_mod)

    result = await analytics_router.get_pnl_summary(_FakeRequest(engine=object()))

    # 1250 - 300 = 950, NOT 0 (which is what the stale MySQL table always gave)
    assert result["total_unrealized"] == 950.0
    assert result["today_unrealized"] == 950.0
    assert result["open_positions"] == 2


@pytest.mark.asyncio
async def test_no_engine_running_falls_back_to_zero_unrealized_not_crash(
    analytics_router, monkeypatch,
):
    async def _fake_closed_trades():
        return []
    monkeypatch.setattr(analytics_router, "_get_closed_trades", _fake_closed_trades)
    monkeypatch.setitem(
        sys.modules, "src.api.routers.positions_router", _fake_positions_router_module([]),
    )

    result = await analytics_router.get_pnl_summary(_FakeRequest(engine=None))

    assert result["total_unrealized"] == 0
    assert result["open_positions"] == 0


@pytest.mark.asyncio
async def test_today_uses_ist_date_not_system_local_date(analytics_router, monkeypatch):
    from datetime import datetime
    from src.core.utils import now_ist

    ist_today = now_ist().date()

    async def _fake_closed_trades():
        return [_FakeTrade("momentum_v1", 500.0, datetime.combine(ist_today, datetime.min.time()))]
    monkeypatch.setattr(analytics_router, "_get_closed_trades", _fake_closed_trades)
    monkeypatch.setitem(
        sys.modules, "src.api.routers.positions_router", _fake_positions_router_module([]),
    )

    result = await analytics_router.get_pnl_summary(_FakeRequest(engine=None))

    assert result["closed_trades_today"] == 1
    assert result["today_realized"] == 500.0
