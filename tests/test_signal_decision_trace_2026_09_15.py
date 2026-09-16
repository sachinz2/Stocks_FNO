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
    # staticmethod() wrapper required: a plain function stored as a class
    # attribute is auto-bound as a regular method (self injected as the
    # first arg) unless explicitly marked static -- LiveTradingEngine
    # itself doesn't need this since Python resolves @staticmethod there
    # directly, but re-assigning the already-unwrapped function here does.
    _classify_signal_error = staticmethod(LiveTradingEngine._classify_signal_error)
    # 2026-09-16 ("Trade Quality Layer" v1): quality_score is computed on
    # every call now -- reuse the real method rather than stub it out, same
    # as _classify_signal_error above.
    _compute_trade_quality_score = LiveTradingEngine._compute_trade_quality_score

    def __init__(self, stats):
        self._signal_gate_stats = stats
        # 2026-09-16: set by the RVOL/ADX/RS/MTF threshold checks
        # themselves inside the real _process_signal() -- absent here since
        # these tests drive _record_signal_trace() directly.
        self._last_gate_rejection = None
        # 2026-09-16: same reasoning -- populated inline by _process_signal(),
        # empty here since these tests skip straight to _record_signal_trace().
        self._last_signal_metrics = {}

    async def _record_rejected_outcome(self, strategy_name, symbol, last_gate, quality_score):
        # Pure side-effect hook for RejectedSignalOutcome -- not under test
        # in this file (see test_trade_quality_layer_2026_09_16.py for that).
        pass


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
        "momentum_v1", "SBIN", "TRENDING", gates_before, exception=KeyError("adx14"),
    )

    row = _FakeTraceRepo.created[0]
    assert row["final_decision"] == "ERROR"
    assert row["last_gate_reached"] == "dte_passed"
    assert "adx14" in row["detail"]


# ── _classify_signal_error() -- structured error classification ────────────
# 2026-09-15, external review: "an exception can look like an ordinary
# rejected trade" without this. Packed into `detail` rather than a new
# column, to avoid an Alembic migration for a best-effort refinement.

def test_classify_broker_error_by_exception_type():
    assert LiveTradingEngine._classify_signal_error(ConnectionError("timeout"), None) == "BROKER_ERROR"
    assert LiveTradingEngine._classify_signal_error(TimeoutError("timeout"), "dte_passed") == "BROKER_ERROR"


def test_classify_pre_signal_error_when_no_gate_reached():
    assert LiveTradingEngine._classify_signal_error(KeyError("adx14"), None) == "DATA_OR_STRATEGY_ERROR"


def test_classify_gate_error_for_entry_gate_checkpoints():
    for gate in ("dte_passed", "rvol_passed", "adx_passed", "rs_passed", "mtf_passed"):
        assert LiveTradingEngine._classify_signal_error(ValueError("x"), gate) == "GATE_ERROR"


def test_classify_option_chain_error_for_contract_resolution_checkpoints():
    for gate in ("lot_passed", "contract_resolved"):
        assert LiveTradingEngine._classify_signal_error(ValueError("x"), gate) == "OPTION_CHAIN_ERROR"


def test_classify_risk_error_at_margin_checkpoint():
    assert LiveTradingEngine._classify_signal_error(ValueError("x"), "margin_passed") == "RISK_ERROR"


@pytest.mark.asyncio
async def test_error_detail_is_prefixed_with_its_classification():
    gates_before = {}
    gates_after = {}  # nothing incremented -- pre-signal failure
    fake = _FakeTraceEngine({"momentum_v1": gates_after})

    await fake._record_signal_trace(
        "momentum_v1", "SBIN", "TRENDING", gates_before, exception=KeyError("adx14"),
    )

    row = _FakeTraceRepo.created[0]
    assert row["detail"].startswith("[DATA_OR_STRATEGY_ERROR]")


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
