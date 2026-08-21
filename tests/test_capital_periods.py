"""
Expiry-to-expiry capital period rollover orchestration
(src/portfolio/capital_periods.py).

rollover_if_needed()'s DB-touching helpers (get_active_period,
_create_period, _close_period) are monkeypatched here rather than faked
against a real SQLAlchemy session -- this isolates the orchestration logic
(when to close/create periods, when to sync risk_manager) from the SQL
layer, which is exactly where a real bug slipped through untested
(2026-08-13): set_capital() was gated on "did a rollover happen this
call", so a restart mid-period (no rollover due) silently left
risk_manager's live limits on the static default forever, until the next
real expiry.
"""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

import src.portfolio.capital_periods as capital_periods


def _period(start, end, starting_capital, closed=False, ending_capital=None):
    return SimpleNamespace(
        period_start=start, period_end=end,
        starting_capital=starting_capital, ending_capital=ending_capital,
        closed=closed,
    )


class _FakeRiskManager:
    def __init__(self):
        self.initial_capital = 300_000.0
        self.set_capital_calls = []

    def set_capital(self, new_capital):
        self.set_capital_calls.append(new_capital)
        self.initial_capital = new_capital


@pytest.mark.asyncio
async def test_set_capital_called_even_when_no_rollover_needed(monkeypatch):
    # The active period is already current (period_end >= today) -- no
    # rollover happens this call. Before the 2026-08-13 fix, set_capital()
    # would NOT be called here at all, leaving risk_manager stuck on
    # whatever it was constructed with (the static config default) after
    # any ordinary restart.
    active = _period(date(2026, 7, 29), date(2026, 8, 25), 321_556.0)
    monkeypatch.setattr(capital_periods, "get_active_period", AsyncMock(return_value=active))
    monkeypatch.setattr(capital_periods, "_create_period", AsyncMock())
    monkeypatch.setattr(capital_periods, "_close_period", AsyncMock())

    rm = _FakeRiskManager()
    result = await capital_periods.rollover_if_needed(
        session_factory=object(), default_capital=300_000.0, risk_manager=rm,
        today=date(2026, 8, 13),
    )

    assert result is active
    assert rm.set_capital_calls == [321_556.0], \
        "set_capital() must sync the live risk_manager on every call, not only when a rollover happens"
    assert rm.initial_capital == 321_556.0


@pytest.mark.asyncio
async def test_set_capital_called_on_actual_rollover(monkeypatch):
    old_period = _period(date(2026, 7, 29), date(2026, 8, 25), 300_000.0)
    closed_period = _period(date(2026, 7, 29), date(2026, 8, 25), 300_000.0, closed=True, ending_capital=340_000.0)
    new_period = _period(date(2026, 8, 26), date(2026, 9, 29), 340_000.0)

    monkeypatch.setattr(capital_periods, "get_active_period", AsyncMock(return_value=old_period))
    monkeypatch.setattr(capital_periods, "_close_period", AsyncMock(return_value=closed_period))
    monkeypatch.setattr(capital_periods, "_create_period", AsyncMock(return_value=new_period))

    rm = _FakeRiskManager()
    result = await capital_periods.rollover_if_needed(
        session_factory=object(), default_capital=300_000.0, risk_manager=rm,
        today=date(2026, 8, 27),  # past the old period's end -> rollover due
    )

    assert result is new_period
    assert rm.set_capital_calls == [340_000.0]


@pytest.mark.asyncio
async def test_no_risk_manager_does_not_raise(monkeypatch):
    active = _period(date(2026, 7, 29), date(2026, 8, 25), 300_000.0)
    monkeypatch.setattr(capital_periods, "get_active_period", AsyncMock(return_value=active))
    monkeypatch.setattr(capital_periods, "_create_period", AsyncMock())
    monkeypatch.setattr(capital_periods, "_close_period", AsyncMock())

    result = await capital_periods.rollover_if_needed(
        session_factory=object(), default_capital=300_000.0, risk_manager=None,
        today=date(2026, 8, 13),
    )
    assert result is active


@pytest.mark.asyncio
async def test_bootstraps_first_period_when_none_exists(monkeypatch):
    created = _period(date(2026, 7, 29), date(2026, 8, 25), 300_000.0)
    create_mock = AsyncMock(return_value=created)
    monkeypatch.setattr(capital_periods, "get_active_period", AsyncMock(return_value=None))
    monkeypatch.setattr(capital_periods, "_create_period", create_mock)
    monkeypatch.setattr(capital_periods, "_close_period", AsyncMock())

    rm = _FakeRiskManager()
    result = await capital_periods.rollover_if_needed(
        session_factory=object(), default_capital=300_000.0, risk_manager=rm,
        today=date(2026, 8, 13),
    )

    assert result is created
    create_mock.assert_awaited_once()
    assert rm.set_capital_calls == [300_000.0]


@pytest.mark.asyncio
async def test_catches_up_multiple_overdue_periods(monkeypatch):
    # Simulates the process being down across more than one expiry cycle.
    p1 = _period(date(2026, 6, 30), date(2026, 7, 28), 300_000.0)
    p1_closed = _period(date(2026, 6, 30), date(2026, 7, 28), 300_000.0, closed=True, ending_capital=310_000.0)
    p2 = _period(date(2026, 7, 29), date(2026, 8, 25), 310_000.0)
    p2_closed = _period(date(2026, 7, 29), date(2026, 8, 25), 310_000.0, closed=True, ending_capital=320_000.0)
    p3 = _period(date(2026, 8, 26), date(2026, 9, 29), 320_000.0)

    monkeypatch.setattr(capital_periods, "get_active_period", AsyncMock(return_value=p1))
    monkeypatch.setattr(capital_periods, "_close_period", AsyncMock(side_effect=[p1_closed, p2_closed]))
    monkeypatch.setattr(capital_periods, "_create_period", AsyncMock(side_effect=[p2, p3]))

    rm = _FakeRiskManager()
    result = await capital_periods.rollover_if_needed(
        session_factory=object(), default_capital=300_000.0, risk_manager=rm,
        today=date(2026, 8, 27),
    )

    assert result is p3
    assert rm.set_capital_calls == [320_000.0]


# ── concurrent-creation guard (2026-08-21) ───────────────────────────────────
#
# Fixed 2026-08-21: rollover_if_needed()'s check-then-insert (get_active_period
# -> _create_period when none exists) had no atomicity guard -- two
# near-simultaneous calls in the same process (e.g. a deploy's startup call
# racing the 08:00 scheduled job tick) could both observe "no active period"
# and each insert a duplicate open CapitalPeriod row. Guarded with an
# in-process asyncio.Lock (rollover_if_needed is only ever called from within
# one process -- see api/main.py startup and src/core/scheduler.py's
# in-process APScheduler job).

class _FakeDB:
    """Minimal in-memory stand-in for the capital_periods table."""

    def __init__(self):
        self.periods: list = []


async def _racy_get_active_period(db, today=None):
    # Yield control here (simulating the real async DB round-trip) so a
    # concurrent caller gets a chance to interleave right at the classic
    # check-then-insert race window.
    await asyncio.sleep(0)
    for p in db.periods:
        if not p.closed:
            return p
    return None


async def _racy_create_period(db, period_start, period_end, starting_capital):
    await asyncio.sleep(0)
    row = _period(period_start, period_end, starting_capital)
    db.periods.append(row)
    return row


@pytest.mark.asyncio
async def test_concurrent_rollover_calls_create_only_one_active_period(monkeypatch):
    monkeypatch.setattr(capital_periods, "get_active_period", _racy_get_active_period)
    monkeypatch.setattr(capital_periods, "_create_period", _racy_create_period)
    monkeypatch.setattr(capital_periods, "_close_period", AsyncMock())

    db = _FakeDB()
    results = await asyncio.gather(
        capital_periods.rollover_if_needed(
            session_factory=db, default_capital=300_000.0, today=date(2026, 8, 13),
        ),
        capital_periods.rollover_if_needed(
            session_factory=db, default_capital=300_000.0, today=date(2026, 8, 13),
        ),
    )

    active_periods = [p for p in db.periods if not p.closed]
    assert len(active_periods) == 1, \
        f"expected exactly one active period, got {len(active_periods)} -- concurrent calls raced past the guard"
    assert results[0] is results[1] is active_periods[0]
