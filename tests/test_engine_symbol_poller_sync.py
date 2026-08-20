"""
LiveTradingEngine.attach_symbol_poller() / _sync_must_track_underlyings()
(2026-08-20).

The weekly active-universe recompute job can drop a symbol below the
liquidity floor. This reconciles the OHLC poller's force-tracked underlying
set against whatever's actually open (_active_spreads/_active_condors/
_single_leg_journals) once per signal cycle, so an open position never loses
market-data coverage -- a single source of truth instead of needing an
incremental register/unregister call at every one of the many entry/exit
call sites in live_trading_engine.py.
"""
from types import SimpleNamespace

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakePoller:
    def __init__(self):
        self._must_track = set()

    def register_underlying(self, symbol):
        self._must_track.add(symbol)

    def unregister_underlying(self, symbol):
        self._must_track.discard(symbol)


def _stub_engine(active_spreads=None, active_condors=None, single_leg_journals=None):
    return SimpleNamespace(
        _active_spreads=active_spreads or {},
        _active_condors=active_condors or {},
        _single_leg_journals=single_leg_journals or {},
    )


def test_sync_registers_underlyings_from_all_three_position_types():
    stub = _stub_engine(
        active_spreads={"RELIANCE": {}},
        active_condors={"TCS": {}},
        single_leg_journals={"INFY26SEP2000CE": {"underlying": "INFY"}},
    )
    poller = _FakePoller()
    stub._symbol_poller = poller

    LiveTradingEngine._sync_must_track_underlyings(stub)

    assert poller._must_track == {"RELIANCE", "TCS", "INFY"}


def test_sync_unregisters_a_closed_underlying():
    stub = _stub_engine(active_spreads={"RELIANCE": {}})
    poller = _FakePoller()
    poller._must_track = {"RELIANCE", "STALE_CLOSED_ONE"}
    stub._symbol_poller = poller

    LiveTradingEngine._sync_must_track_underlyings(stub)

    assert poller._must_track == {"RELIANCE"}


def test_sync_is_a_noop_when_no_poller_attached():
    stub = _stub_engine(active_spreads={"RELIANCE": {}})
    # No _symbol_poller attribute at all -- getattr(..., None) path.
    LiveTradingEngine._sync_must_track_underlyings(stub)  # must not raise


def test_attach_symbol_poller_syncs_immediately_for_restored_positions():
    # Mirrors attach_ltp_poller()'s existing "re-register restored contracts
    # immediately" behavior, for underlyings instead of option contracts.
    stub = _stub_engine(single_leg_journals={"AXISBANK26SEP1260CE": {"underlying": "AXISBANK"}})
    stub._sync_must_track_underlyings = lambda: LiveTradingEngine._sync_must_track_underlyings(stub)
    poller = _FakePoller()

    LiveTradingEngine.attach_symbol_poller(stub, poller)

    assert stub._symbol_poller is poller
    assert poller._must_track == {"AXISBANK"}


def test_sync_ignores_single_leg_journal_entries_missing_underlying():
    # Defensive: a malformed/legacy journal entry without "underlying" must
    # not crash the sync or register a bogus None/empty-string symbol.
    stub = _stub_engine(single_leg_journals={"SOME26SEPCE": {}})
    poller = _FakePoller()
    stub._symbol_poller = poller

    LiveTradingEngine._sync_must_track_underlyings(stub)

    assert poller._must_track == set()
