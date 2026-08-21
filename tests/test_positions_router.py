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


# ── group_id/structure_type (2026-08-13) ─────────────────────────────────────
#
# The dashboard used to group legs by "same underlying + has both a short and
# a long leg" -- a heuristic that misattributes a standalone single-leg
# position into a credit_spread_v1/iron_condor_v1 group whenever both happen
# to be open on the same underlying (both trade from the same 41-symbol F&O
# universe, so this is entirely plausible). The engine already knows exactly
# which legs belong to which structure -- expose that directly instead.

class _FakeMultiStructureEngine:
    _active_spreads = {
        "BAJFINANCE": {
            "short_contract": "BAJFINANCE26SEP1160CE", "long_contract": "BAJFINANCE26SEP1190CE",
            "short_premium": 18.59, "long_premium": 10.46, "lot_size": 750,
        },
    }
    _active_condors = {
        "TITAN": {
            "put_short_contract": "TITAN26SEP4650PE", "put_long_contract": "TITAN26SEP4400PE",
            "call_short_contract": "TITAN26SEP5100CE", "call_long_contract": "TITAN26SEP5300CE",
            "put_short_premium": 82.0, "put_long_premium": 13.0,
            "call_short_premium": 45.0, "call_long_premium": 8.0,
            "lot_size": 175,
        },
    }
    # A standalone single-leg BUY on BAJFINANCE -- same underlying as the
    # active credit spread above. Must NOT be folded into that spread's group.
    # journal_id=154 reuses _FakeJournalRepo's existing fixture entry.
    _single_leg_journals = {
        "BAJFINANCE26SEP1200CE": {"journal_id": 154, "strategy_name": "momentum_v1"},
    }


@pytest.mark.asyncio
async def test_spread_legs_share_a_group_id(positions_router):
    positions_router.BaseRepository = _FakeJournalRepo
    rows = await positions_router._positions_from_engine(_FakeMultiStructureEngine(), kite=None, redis=None)

    short_leg = next(r for r in rows if r["symbol"] == "BAJFINANCE26SEP1160CE")
    long_leg  = next(r for r in rows if r["symbol"] == "BAJFINANCE26SEP1190CE")
    assert short_leg["group_id"] == long_leg["group_id"]
    assert short_leg["group_id"] == "spread:BAJFINANCE"
    assert short_leg["structure_type"] == "credit_spread"


@pytest.mark.asyncio
async def test_condor_legs_share_a_group_id_distinct_from_spread(positions_router):
    positions_router.BaseRepository = _FakeJournalRepo
    rows = await positions_router._positions_from_engine(_FakeMultiStructureEngine(), kite=None, redis=None)

    condor_legs = [r for r in rows if r["structure_type"] == "iron_condor"]
    assert len(condor_legs) == 4
    group_ids = {r["group_id"] for r in condor_legs}
    assert group_ids == {"condor:TITAN"}


# ── _fetch_market_prices() kite.ltp() fallback staleness check (2026-08-21) ──
#
# Fixed 2026-08-21: steps 1-2 (Redis cache) are staleness-aware at their
# source (resolve_reliable_option_price(), used by ZerodhaLTPPoller/
# option_chain.get_option_quote()), but step 3 (cache miss -> batched Kite
# call) used to call kite.ltp() and trust last_price directly with no
# staleness check at all -- a stale last-traded price on a thin/illiquid
# leg could be silently shown as current on the dashboard/analytics. Now
# uses kite.quote() + resolve_reliable_option_price(), matching the other
# 2 call sites of that function.

class _FakeKiteQuote:
    """Stands in for kite.quote() -- batched, keyed by "NFO:{contract}"."""

    def __init__(self, responses: dict):
        self._responses = responses
        self.calls = []

    def quote(self, nfo_syms):
        self.calls.append(list(nfo_syms))
        return {sym: self._responses[sym] for sym in nfo_syms if sym in self._responses}

    def ltp(self, nfo_syms):
        raise AssertionError("kite.ltp() must not be called -- the fallback now uses kite.quote()")


@pytest.mark.asyncio
async def test_fetch_market_prices_fallback_uses_staleness_resolver_for_stale_quote(positions_router):
    import datetime as dt

    # TITAN26SEP4650PE: stale last_price (last traded 2 weeks ago), but has
    # live order-book depth -- resolve_reliable_option_price() must fall back
    # to the bid/ask midpoint instead of trusting the stale last_price.
    stale_quote = {
        "last_price": 82.0,
        "last_trade_time": dt.datetime(2026, 7, 29, 10, 0, 0),  # not today
        "depth": {
            "buy":  [{"price": 34.0, "quantity": 50}],
            "sell": [{"price": 35.6, "quantity": 50}],
        },
    }
    kite = _FakeKiteQuote({"NFO:TITAN26SEP4650PE": stale_quote})

    prices = await positions_router._fetch_market_prices(["TITAN26SEP4650PE"], kite, redis=None)

    assert prices["TITAN26SEP4650PE"] == 34.8, \
        "stale last_price (Rs82) must not be used -- expected the bid/ask midpoint from resolve_reliable_option_price()"


@pytest.mark.asyncio
async def test_fetch_market_prices_fallback_trusts_price_traded_today(positions_router):
    from src.core.utils import now_ist

    today_quote = {
        "last_price": 46.55,
        "last_trade_time": now_ist().replace(tzinfo=None),
        "depth": {"buy": [], "sell": []},
    }
    kite = _FakeKiteQuote({"NFO:BAJFINANCE26SEP1160CE": today_quote})

    prices = await positions_router._fetch_market_prices(["BAJFINANCE26SEP1160CE"], kite, redis=None)

    assert prices["BAJFINANCE26SEP1160CE"] == 46.55


@pytest.mark.asyncio
async def test_standalone_single_leg_on_same_underlying_as_spread_is_not_grouped(positions_router):
    # The regression this was built to catch: BAJFINANCE has BOTH an active
    # credit spread AND an unrelated standalone single-leg position. The
    # single-leg position must have group_id=None, never the spread's group_id.
    positions_router.BaseRepository = _FakeJournalRepo
    rows = await positions_router._positions_from_engine(_FakeMultiStructureEngine(), kite=None, redis=None)

    standalone = next(r for r in rows if r["symbol"] == "BAJFINANCE26SEP1200CE")
    assert standalone["group_id"] is None
    assert standalone["structure_type"] == "single_leg"

    spread_leg = next(r for r in rows if r["symbol"] == "BAJFINANCE26SEP1160CE")
    assert spread_leg["group_id"] != standalone["group_id"]
