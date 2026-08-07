"""
positions_router._positions_from_engine() -- the /positions API endpoint
that feeds the dashboard's Open Positions page.

Fixed 2026-08-06: only iterated engine._active_spreads and
_active_condors (credit_spread_v1/iron_condor_v1) -- single-leg positions
(ema_crossover_v1/momentum_v1, tracked in engine._single_leg_journals) were
never included at all, despite being correctly persisted to trade_journal.
Confirmed live: two real open momentum_v1 trades were in the DB but
completely absent from /positions.
"""
import sys
import types
import pytest


@pytest.fixture
def positions_router(monkeypatch):
    """
    Import positions_router with src.database.* stubbed out, since the real
    src.core.config.Settings() fails to construct in this dev environment
    (an unrelated pydantic-settings / .env mismatch, not something this
    router touches) -- this keeps the test hermetic and avoids depending on
    a real DB/session.
    """
    db_pkg = types.ModuleType("src.database"); db_pkg.__path__ = []
    conn_mod = types.ModuleType("src.database.connection")
    conn_mod.AsyncSessionLocal = object()

    models_pkg = types.ModuleType("src.database.models"); models_pkg.__path__ = []
    position_mod = types.ModuleType("src.database.models.position")
    class Position: pass
    position_mod.Position = Position

    journal_mod = types.ModuleType("src.database.models.trade_journal")
    class TradeJournal: pass
    journal_mod.TradeJournal = TradeJournal

    repos_pkg = types.ModuleType("src.database.repositories"); repos_pkg.__path__ = []
    base_mod = types.ModuleType("src.database.repositories.base")
    class BaseRepository:
        def __init__(self, model, session): pass
    base_mod.BaseRepository = BaseRepository

    stubs = {
        "src.database": db_pkg,
        "src.database.connection": conn_mod,
        "src.database.models": models_pkg,
        "src.database.models.position": position_mod,
        "src.database.models.trade_journal": journal_mod,
        "src.database.repositories": repos_pkg,
        "src.database.repositories.base": base_mod,
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    import importlib.util
    from pathlib import Path
    router_path = Path(__file__).resolve().parent.parent / "src" / "api" / "routers" / "positions_router.py"
    spec = importlib.util.spec_from_file_location("positions_router", router_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeJournal:
    def __init__(self, entry_price, quantity):
        self.entry_price = entry_price
        self.quantity = quantity


class _FakeJournalRepo:
    def __init__(self, model, session):
        pass

    async def get_by_id(self, jid):
        return {154: _FakeJournal(46.59, 175), 155: _FakeJournal(4.95, 1900)}.get(jid)


class _FakeEngine:
    _active_spreads = {}
    _active_condors = {}
    _single_leg_journals = {
        "TITAN26AUG4975CE": {"journal_id": 154, "strategy_name": "momentum_v1"},
        "POWERGRID26AUG270PE": {"journal_id": 155, "strategy_name": "momentum_v1"},
    }


@pytest.mark.asyncio
async def test_positions_from_engine_includes_single_leg_positions(positions_router):
    positions_router.BaseRepository = _FakeJournalRepo  # monkeypatch the DB repo used inside the function

    rows = await positions_router._positions_from_engine(_FakeEngine(), kite=None, redis=None)

    symbols = {r["symbol"] for r in rows}
    assert "TITAN26AUG4975CE" in symbols
    assert "POWERGRID26AUG270PE" in symbols

    titan = next(r for r in rows if r["symbol"] == "TITAN26AUG4975CE")
    assert titan["quantity"] == 175
    assert titan["avg_price"] == 46.59

    powergrid = next(r for r in rows if r["symbol"] == "POWERGRID26AUG270PE")
    assert powergrid["quantity"] == 1900
    assert powergrid["avg_price"] == 4.95
