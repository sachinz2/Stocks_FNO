"""
"Trade Quality Layer" v1 of 3 (2026-09-16, external review round 2 Part 2,
user-authorized top-3 scope: Trade Quality Score, option-quality filter,
rejected-signal outcome tracking). All three are observational/execution-
risk-grounded, not speculative new gates -- see each method's own docstring
in live_trading_engine.py for the reasoning.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.enums import SignalType


# ── _compute_trade_quality_score() -- component scoring ─────────────────────

class _FakeScoreEngine:
    _compute_trade_quality_score = LiveTradingEngine._compute_trade_quality_score

    def __init__(self, metrics):
        self._last_signal_metrics = metrics


def test_returns_none_when_no_real_candidate_was_scored():
    fake = _FakeScoreEngine({})  # NO_SIGNAL/ERROR path -- nothing to score
    assert fake._compute_trade_quality_score() is None


def test_best_case_buy_scores_near_the_top_of_the_band():
    fake = _FakeScoreEngine({
        "signal": "BUY", "regime": "TRENDING",
        "adx": 40.0, "rvol": 2.5,
        "rs_rank": 3, "rs_total": 40,
        "mtf_agree": True, "mtf_spread_pct": 1.2,
    })
    score = fake._compute_trade_quality_score()
    assert score == 100


def test_worst_case_scores_at_the_bottom():
    fake = _FakeScoreEngine({
        "signal": "BUY", "regime": "UNKNOWN",
        "adx": 15.0, "rvol": 0.5,
        "rs_rank": 35, "rs_total": 40,
        "mtf_agree": False, "mtf_spread_pct": 5.0,  # strong opposition
    })
    score = fake._compute_trade_quality_score()
    # adx=0, rvol=0, regime(unknown)=5, rs low bucket=5, mtf strong oppose=0
    assert score == 0 + 0 + 5 + 5 + 0


def test_untested_components_score_a_neutral_midpoint_not_zero():
    # A candidate rejected at RVOL never reaches ADX/RS/MTF -- those must
    # not be silently scored as "failed" (0), which would make an early
    # rejection look artificially worse than a later one on the same signal.
    fake = _FakeScoreEngine({"signal": "SELL", "regime": "TRENDING"})
    score = fake._compute_trade_quality_score()
    # adx=12, rvol=10, regime(TRENDING)=20, rs=10, mtf=8
    assert score == 12 + 10 + 20 + 10 + 8


def test_sell_rs_rank_is_mirrored_so_weakest_stock_scores_highest():
    # rank 1 = strongest vs NIFTY -- great for a BUY, bad for a SELL. A SELL
    # candidate on the single weakest stock (rank == rs_total) must score
    # the RS component the same as a BUY candidate on the single strongest
    # stock (rank == 1).
    buy = _FakeScoreEngine({
        "signal": "BUY", "regime": "TRENDING", "rs_rank": 1, "rs_total": 40,
    })
    sell = _FakeScoreEngine({
        "signal": "SELL", "regime": "TRENDING", "rs_rank": 40, "rs_total": 40,
    })
    assert buy._compute_trade_quality_score() == sell._compute_trade_quality_score()


def test_mtf_weak_opposition_scores_partial_credit_not_zero():
    agree = _FakeScoreEngine({"signal": "BUY", "mtf_agree": True, "mtf_spread_pct": 0.1})
    weak_oppose = _FakeScoreEngine({"signal": "BUY", "mtf_agree": False, "mtf_spread_pct": 0.1})
    strong_oppose = _FakeScoreEngine({"signal": "BUY", "mtf_agree": False, "mtf_spread_pct": 1.0})
    a, w, s = (
        agree._compute_trade_quality_score(),
        weak_oppose._compute_trade_quality_score(),
        strong_oppose._compute_trade_quality_score(),
    )
    assert a > w > s


# ── Shared fake repo, wired once for the whole module ────────────────────────
#
# _record_signal_trace(), _record_rejected_outcome(), and
# _backfill_rejected_outcomes() each open their own BaseRepository(model,
# session) -- three separate autouse fixtures each monkeypatching the same
# base_mod.BaseRepository would silently stomp on each other (whichever ran
# last for a given test wins, regardless of which section that test lives
# in). One repo double, dispatching on the model class it's constructed
# with, avoids that entirely.

class _FakeRepo:
    trace_created = []
    outcome_created = []
    pending = []
    updates = []

    def __init__(self, model, session):
        self._model_name = getattr(model, "__name__", "")

    async def create(self, obj_in):
        if self._model_name == "SignalDecisionTrace":
            _FakeRepo.trace_created.append(obj_in)
        else:
            _FakeRepo.outcome_created.append(obj_in)
        return obj_in

    async def filter(self, **kwargs):
        return _FakeRepo.pending

    async def update(self, db_obj, obj_in):
        _FakeRepo.updates.append((db_obj.id, dict(obj_in)))
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj


@pytest.fixture(autouse=True)
def _wire_fake_repo(monkeypatch):
    import src.database.repositories.base as base_mod
    _FakeRepo.trace_created = []
    _FakeRepo.outcome_created = []
    _FakeRepo.pending = []
    _FakeRepo.updates = []
    monkeypatch.setattr(base_mod, "BaseRepository", _FakeRepo)
    yield


# ── quality_score threaded into _record_signal_trace() ──────────────────────

class _FakeTraceEngine:
    _record_signal_trace = LiveTradingEngine._record_signal_trace
    _compute_trade_quality_score = LiveTradingEngine._compute_trade_quality_score
    _classify_signal_error = staticmethod(LiveTradingEngine._classify_signal_error)

    def __init__(self, stats, metrics=None):
        self._signal_gate_stats = stats
        self._last_gate_rejection = None
        self._last_signal_metrics = metrics or {}
        self._recorded_outcomes = []

    async def _record_rejected_outcome(self, strategy_name, symbol, last_gate, quality_score):
        self._recorded_outcomes.append((strategy_name, symbol, last_gate, quality_score))


@pytest.mark.asyncio
async def test_rejected_row_carries_the_computed_quality_score():
    gates_before = {"signal_generated": 3, "dte_passed": 3}
    gates_after = {"signal_generated": 4, "dte_passed": 4}  # died right after DTE
    fake = _FakeTraceEngine(
        {"momentum_v1": gates_after},
        metrics={"signal": "BUY", "regime": "TRENDING", "adx": 40.0, "rvol": 2.5},
    )

    await fake._record_signal_trace("momentum_v1", "SBIN", "TRENDING", gates_before)

    row = _FakeRepo.trace_created[0]
    assert row["final_decision"] == "REJECTED"
    assert row["quality_score"] is not None
    assert row["quality_score"] == fake._compute_trade_quality_score()


@pytest.mark.asyncio
async def test_no_signal_row_has_no_quality_score():
    gates_before = {"signal_generated": 10}
    fake = _FakeTraceEngine({"momentum_v1": dict(gates_before)}, metrics={})  # unchanged -> NO_SIGNAL

    await fake._record_signal_trace("momentum_v1", "INFY", "TRENDING", gates_before)

    row = _FakeRepo.trace_created[0]
    assert row["final_decision"] == "NO_SIGNAL"
    assert row["quality_score"] is None


@pytest.mark.asyncio
async def test_entered_row_also_carries_a_quality_score():
    gates_before = {}
    gates_after = {
        "signal_generated": 1, "dte_passed": 1, "rvol_passed": 1, "adx_passed": 1,
        "rs_passed": 1, "mtf_passed": 1, "lot_passed": 1, "contract_resolved": 1,
        "option_quality_passed": 1, "trade_placed": 1,
    }
    fake = _FakeTraceEngine(
        {"momentum_v1": gates_after},
        metrics={"signal": "BUY", "regime": "TRENDING"},
    )

    await fake._record_signal_trace("momentum_v1", "SBIN", "TRENDING", gates_before)

    row = _FakeRepo.trace_created[0]
    assert row["final_decision"] == "ENTERED"
    assert row["quality_score"] is not None


@pytest.mark.asyncio
async def test_rejection_triggers_the_outcome_recording_hook():
    gates_before = {"signal_generated": 3, "dte_passed": 3}
    gates_after = {"signal_generated": 4, "dte_passed": 4}
    fake = _FakeTraceEngine({"momentum_v1": gates_after}, metrics={"signal": "SELL"})

    await fake._record_signal_trace("momentum_v1", "SBIN", "RANGE_BOUND", gates_before)

    assert len(fake._recorded_outcomes) == 1
    strategy_name, symbol, last_gate, quality_score = fake._recorded_outcomes[0]
    assert (strategy_name, symbol, last_gate) == ("momentum_v1", "SBIN", "dte_passed")


@pytest.mark.asyncio
async def test_entered_does_not_trigger_the_outcome_recording_hook():
    gates_before = {}
    gates_after = {"signal_generated": 1, "trade_placed": 1}
    fake = _FakeTraceEngine({"momentum_v1": gates_after}, metrics={"signal": "BUY"})

    await fake._record_signal_trace("momentum_v1", "SBIN", "TRENDING", gates_before)

    assert fake._recorded_outcomes == []


# ── _record_rejected_outcome() -- initial row write ──────────────────────────

class _FakeOutcomeEngine:
    _record_rejected_outcome = LiveTradingEngine._record_rejected_outcome

    def __init__(self, metrics):
        self._last_signal_metrics = metrics


@pytest.mark.asyncio
async def test_records_a_row_when_signal_and_close_are_both_known():
    fake = _FakeOutcomeEngine({"signal": "SELL", "close": 1234.5})

    await fake._record_rejected_outcome("momentum_v1", "SBIN", "mtf_passed", 62)

    assert len(_FakeRepo.outcome_created) == 1
    row = _FakeRepo.outcome_created[0]
    assert row["strategy_name"] == "momentum_v1"
    assert row["symbol"] == "SBIN"
    assert row["signal"] == "SELL"
    assert row["rejected_at_gate"] == "mtf_passed"
    assert row["quality_score"] == 62
    assert row["close_at_rejection"] == 1234.5


@pytest.mark.asyncio
async def test_skips_silently_when_close_was_never_captured():
    # Very early rejections (VOLATILE-regime BUY skip, daily order limit)
    # return before underlying_price is computed -- no baseline price, no row.
    fake = _FakeOutcomeEngine({"signal": "BUY"})  # no "close" key

    await fake._record_rejected_outcome("momentum_v1", "SBIN", "signal_generated", None)

    assert _FakeRepo.outcome_created == []


# ── _backfill_rejected_outcomes() -- periodic forward-price fill-in ─────────

class _FakeRow:
    def __init__(self, id, timestamp, symbol, **cols):
        self.id = id
        self.timestamp = timestamp
        self.symbol = symbol
        self.close_5m = cols.get("close_5m")
        self.close_15m = cols.get("close_15m")
        self.close_30m = cols.get("close_30m")
        self.close_60m = cols.get("close_60m")
        self.outcome_complete = False


class _FakeBackfillEngine:
    _backfill_rejected_outcomes = LiveTradingEngine._backfill_rejected_outcomes
    _REJECTED_OUTCOME_OFFSETS_MIN = LiveTradingEngine._REJECTED_OUTCOME_OFFSETS_MIN
    _REJECTED_OUTCOME_GIVE_UP_MIN = LiveTradingEngine._REJECTED_OUTCOME_GIVE_UP_MIN

    def __init__(self, close_price):
        self._get_market_data = AsyncMock(return_value={"close": close_price})


def _minutes_ago(m):
    from src.core.utils import now_ist
    from datetime import timedelta
    return now_ist().replace(tzinfo=None) - timedelta(minutes=m)


@pytest.mark.asyncio
async def test_fills_in_the_5m_checkpoint_once_it_comes_due():
    row = _FakeRow(1, _minutes_ago(6), "SBIN")
    _FakeRepo.pending = [row]
    fake = _FakeBackfillEngine(close_price=1250.0)

    await fake._backfill_rejected_outcomes()

    assert row.close_5m == 1250.0
    assert row.close_15m is None  # not due yet
    assert row.outcome_complete is False


@pytest.mark.asyncio
async def test_marks_complete_once_all_four_checkpoints_are_filled():
    row = _FakeRow(2, _minutes_ago(65), "SBIN", close_5m=1200.0, close_15m=1210.0, close_30m=1220.0)
    _FakeRepo.pending = [row]
    fake = _FakeBackfillEngine(close_price=1230.0)

    await fake._backfill_rejected_outcomes()

    assert row.close_60m == 1230.0
    assert row.outcome_complete is True


@pytest.mark.asyncio
async def test_gives_up_on_stale_rows_even_with_gaps():
    # Live tick never came back for this symbol -- must not be scanned forever.
    row = _FakeRow(3, _minutes_ago(200), "DELISTEDCO")
    _FakeRepo.pending = [row]
    fake = _FakeBackfillEngine(close_price=None)
    fake._get_market_data = AsyncMock(return_value=None)  # no live tick available at all

    await fake._backfill_rejected_outcomes()

    assert row.outcome_complete is True
    assert row.close_60m is None  # gave up, didn't fabricate a value


@pytest.mark.asyncio
async def test_does_not_touch_a_row_with_nothing_due_yet():
    row = _FakeRow(4, _minutes_ago(2), "SBIN")  # not even 5m old yet
    _FakeRepo.pending = [row]
    fake = _FakeBackfillEngine(close_price=1250.0)

    await fake._backfill_rejected_outcomes()

    assert _FakeRepo.updates == []
    fake._get_market_data.assert_not_awaited()


# ── Option-quality filter, driven through the real _process_signal() ────────

class _FakeMtfRedis:
    def __init__(self, tick15_value):
        self._value = tick15_value

    async def get(self, key):
        return self._value


class _FakeOptionQualityEngine:
    _audit_gate = LiveTradingEngine._audit_gate
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure
    _OPTION_MAX_SPREAD_PCT = LiveTradingEngine._OPTION_MAX_SPREAD_PCT

    def __init__(self, tick15_value):
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {}
        self._exited_today = set()
        self._max_daily_orders = 0
        self._max_concurrent_intraday = 999
        self._last_signal_date = {}
        self._signal_gate_stats = {}
        self._last_gate_rejection = None
        self._close_option_positions = AsyncMock()
        self._has_open_option = AsyncMock(return_value=False)
        self._get_market_data = AsyncMock(return_value={
            "close": 1200.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        })
        self._redis = _FakeMtfRedis(tick15_value)
        self.rs_ranker = None
        self._get_lot_size = AsyncMock(return_value=550)
        self._get_iv_rank = AsyncMock(return_value=50.0)
        self._get_strike_interval = AsyncMock(return_value=50.0)
        self._resolve_contract = AsyncMock(return_value=(1200, "SBIN26SEP1200CE"))
        self._kite = object()  # any non-None sentinel
        # order_status != "OPEN" so downstream (_log_trade_open/_persist_state/
        # _notify/_ltp_poller) is never reached -- this harness is scoped to
        # the option-quality gate itself, not full order-placement side effects.
        self.order_manager = SimpleNamespace(
            place_order=AsyncMock(return_value=SimpleNamespace(order_status="REJECTED"))
        )


def _oq_strategy(**overrides):
    defaults = dict(
        name="momentum_v1", is_active=True, min_dte=0, max_dte=999,
        rvol_checked_internally=True, adx_checked_internally=True,
        require_rs=False,
        generate_signal=lambda market_data: SignalType.BUY,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_AGREEING_TICK15 = json.dumps({"ema20": 105.0, "ema50": 100.0})  # bullish, agrees with BUY


@pytest.mark.asyncio
async def test_option_quality_gate_rejects_a_wide_spread(monkeypatch):
    async def _fake_quote(contract, kite, redis):
        return 45.0

    async def _fake_quality(contract, kite):
        return {"bid": 42.0, "ask": 48.0, "spread_pct": 13.33, "oi": 1000, "volume": 500}

    monkeypatch.setattr("src.market_data.option_chain.get_option_quote", _fake_quote)
    monkeypatch.setattr("src.market_data.option_chain.get_option_quality_metrics", _fake_quality)
    fake = _FakeOptionQualityEngine(_AGREEING_TICK15)

    await LiveTradingEngine._process_signal(fake, _oq_strategy(), "SBIN", vix=15.0, regime="TRENDING")

    stats = fake._signal_gate_stats.get("momentum_v1", {})
    assert "option_quality_passed" not in stats
    rej = fake._last_gate_rejection
    assert rej["gate"] == "option_quality_passed"
    assert rej["reason"] == "OPTION_SPREAD_TOO_WIDE"
    assert rej["value"] == 13.33
    assert fake.order_manager.place_order.await_count == 0


@pytest.mark.asyncio
async def test_option_quality_gate_passes_a_tight_spread(monkeypatch):
    async def _fake_quote(contract, kite, redis):
        return 45.0

    async def _fake_quality(contract, kite):
        return {"bid": 44.0, "ask": 46.0, "spread_pct": 4.44, "oi": 50000, "volume": 20000}

    monkeypatch.setattr("src.market_data.option_chain.get_option_quote", _fake_quote)
    monkeypatch.setattr("src.market_data.option_chain.get_option_quality_metrics", _fake_quality)
    fake = _FakeOptionQualityEngine(_AGREEING_TICK15)

    await LiveTradingEngine._process_signal(fake, _oq_strategy(), "SBIN", vix=15.0, regime="TRENDING")

    stats = fake._signal_gate_stats.get("momentum_v1", {})
    assert stats.get("option_quality_passed", 0) >= 1
    assert fake.order_manager.place_order.await_count == 1


@pytest.mark.asyncio
async def test_option_quality_gate_fails_open_when_spread_is_not_computable(monkeypatch):
    # A thin, one-sided, or empty order book -- no spread_pct means nothing
    # to reject on; this v1 doesn't hard-block on missing depth alone.
    async def _fake_quote(contract, kite, redis):
        return 45.0

    async def _fake_quality(contract, kite):
        return {"bid": None, "ask": None, "spread_pct": None, "oi": None, "volume": None}

    monkeypatch.setattr("src.market_data.option_chain.get_option_quote", _fake_quote)
    monkeypatch.setattr("src.market_data.option_chain.get_option_quality_metrics", _fake_quality)
    fake = _FakeOptionQualityEngine(_AGREEING_TICK15)

    await LiveTradingEngine._process_signal(fake, _oq_strategy(), "SBIN", vix=15.0, regime="TRENDING")

    stats = fake._signal_gate_stats.get("momentum_v1", {})
    assert stats.get("option_quality_passed", 0) >= 1
    assert fake.order_manager.place_order.await_count == 1


@pytest.mark.asyncio
async def test_option_quality_gate_is_skippable_per_strategy(monkeypatch):
    quality_calls = []

    async def _fake_quote(contract, kite, redis):
        return 45.0

    async def _fake_quality(contract, kite):
        quality_calls.append(contract)
        return {"bid": 1.0, "ask": 100.0, "spread_pct": 99.0, "oi": None, "volume": None}

    monkeypatch.setattr("src.market_data.option_chain.get_option_quote", _fake_quote)
    monkeypatch.setattr("src.market_data.option_chain.get_option_quality_metrics", _fake_quality)
    fake = _FakeOptionQualityEngine(_AGREEING_TICK15)

    await LiveTradingEngine._process_signal(
        fake, _oq_strategy(option_quality_check=False), "SBIN", vix=15.0, regime="TRENDING",
    )

    assert quality_calls == [], "opted-out strategy must not even fetch quality metrics"
    stats = fake._signal_gate_stats.get("momentum_v1", {})
    assert stats.get("option_quality_passed", 0) >= 1
    assert fake.order_manager.place_order.await_count == 1
