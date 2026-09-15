"""
SignalDecisionTrace (2026-09-15, external review): per-candidate diagnostic
row -- WHY a specific (strategy, symbol) pair entered or got rejected THIS
cycle, derived by diffing LiveTradingEngine._signal_gate_stats before/after
one _process_signal() call. See _record_signal_trace()'s and
SignalDecisionTrace's own docstrings for the full design rationale
(diff-based, not threaded through _process_signal's internals -- zero risk
to which trades actually get taken).
"""
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeTraceRepo:
    created = []

    def __init__(self, model, session):
        pass

    async def create(self, obj_in):
        _FakeTraceRepo.created.append(obj_in)
        return obj_in


class _FakeTraceEngine:
    _record_signal_trace = LiveTradingEngine._record_signal_trace

    def __init__(self, stats):
        self._signal_gate_stats = stats


@pytest.fixture(autouse=True)
def _wire_fake_repo(monkeypatch):
    import src.database.repositories.base as base_mod
    _FakeTraceRepo.created = []
    monkeypatch.setattr(base_mod, "BaseRepository", _FakeTraceRepo)
    yield


@pytest.mark.asyncio
async def test_entered_when_trade_placed_incremented():
    fake = _FakeTraceEngine({"momentum_v1": {
        "signal_generated": 1, "dte_passed": 1, "rvol_passed": 1, "adx_passed": 1,
        "rs_passed": 1, "mtf_passed": 1, "lot_passed": 1, "contract_resolved": 1,
        "trade_placed": 1,
    }})
    gates_before = {}  # first candidate this cycle for this strategy

    await fake._record_signal_trace("momentum_v1", "BANDHANBNK", "TRENDING", gates_before)

    assert len(_FakeTraceRepo.created) == 1
    row = _FakeTraceRepo.created[0]
    assert row["final_decision"] == "ENTERED"
    assert row["last_gate_reached"] == "trade_placed"
    assert row["symbol"] == "BANDHANBNK"
    assert row["regime"] == "TRENDING"
    assert row["detail"] is None


@pytest.mark.asyncio
async def test_rejected_reports_the_last_single_leg_gate_reached():
    # Candidate reached signal_generated + dte_passed this cycle but died
    # at RVOL -- must report "rvol_passed" as NOT reached (last reached =
    # dte_passed), matching the real 2026-09-15 PAYTM/BANDHANBNK incident
    # this feature exists to make directly queryable instead of inferred by
    # hand from gate-audit counters.
    gates_before = {"signal_generated": 3, "dte_passed": 3}
    gates_after = {"signal_generated": 4, "dte_passed": 4}  # rvol_passed never incremented
    fake = _FakeTraceEngine({"ema_crossover_v1": gates_after})

    await fake._record_signal_trace("ema_crossover_v1", "PAYTM", "TRENDING", gates_before)

    row = _FakeTraceRepo.created[0]
    assert row["final_decision"] == "REJECTED"
    assert row["last_gate_reached"] == "dte_passed"


@pytest.mark.asyncio
async def test_rejected_reports_the_last_spread_pipeline_gate_reached():
    # credit_spread_v1's own pipeline (dte -> lot_size -> vix -> iv_rank ->
    # direction -> adx -> event_calendar -> contract_resolved -> margin ->
    # trade_placed) uses different gate names than the single-leg pipeline
    # but shares the same diffing logic and canonical order.
    gates_before = {"dte_passed": 5, "lot_size_passed": 5, "vix_passed": 5}
    gates_after = {"dte_passed": 6, "lot_size_passed": 6, "vix_passed": 5}  # died at vix
    fake = _FakeTraceEngine({"credit_spread_v1": gates_after})

    await fake._record_signal_trace("credit_spread_v1", "RELIANCE", "RANGE_BOUND", gates_before)

    row = _FakeTraceRepo.created[0]
    assert row["final_decision"] == "REJECTED"
    assert row["last_gate_reached"] == "lot_size_passed"


@pytest.mark.asyncio
async def test_no_signal_when_nothing_incremented_this_cycle():
    # generate_signal() returned HOLD, strategy paused, or data not live yet
    # -- no gate incremented at all for this candidate this cycle.
    gates_before = {"signal_generated": 10, "dte_passed": 9}
    fake = _FakeTraceEngine({"momentum_v1": dict(gates_before)})  # unchanged

    await fake._record_signal_trace("momentum_v1", "INFY", "TRENDING", gates_before)

    row = _FakeTraceRepo.created[0]
    assert row["final_decision"] == "NO_SIGNAL"
    assert row["last_gate_reached"] is None


@pytest.mark.asyncio
async def test_error_records_exception_detail_and_last_reached_gate():
    gates_before = {"signal_generated": 1}
    gates_after = {"signal_generated": 2, "dte_passed": 1}
    fake = _FakeTraceEngine({"momentum_v1": gates_after})

    await fake._record_signal_trace(
        "momentum_v1", "SBIN", "TRENDING", gates_before, exception="KeyError: 'adx14'",
    )

    row = _FakeTraceRepo.created[0]
    assert row["final_decision"] == "ERROR"
    assert row["last_gate_reached"] == "dte_passed"
    assert "KeyError" in row["detail"]


@pytest.mark.asyncio
async def test_record_signal_trace_swallows_repo_errors(monkeypatch):
    class _BrokenRepo:
        def __init__(self, model, session):
            pass

        async def create(self, obj_in):
            raise ConnectionError("db down")

    import src.database.repositories.base as base_mod
    monkeypatch.setattr(base_mod, "BaseRepository", _BrokenRepo)

    fake = _FakeTraceEngine({"momentum_v1": {"signal_generated": 1}})
    await fake._record_signal_trace("momentum_v1", "SBIN", "TRENDING", {})  # must not raise
