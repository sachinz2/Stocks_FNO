"""
StrategyMonitor's rolling-drawdown auto-pause check.

Fixed 2026-08-06: DEFAULT_EXPECTED_DRAWDOWN used non-versioned strategy-name
keys ("ema_crossover", "credit_spread", "iron_condor") that never matched
the real strategy IDs used everywhere else in the system ("ema_crossover_v1"
etc). Since StrategyMonitor is instantiated with no override, exp_dd always
resolved to 0 for every real strategy, and Check 2 (rolling drawdown) is
gated on exp_dd > 0 -- meaning the drawdown-based auto-pause had never
actually fired for any strategy since this file was written.
"""
from datetime import datetime, timedelta

import pytest
from src.risk.strategy_monitor import StrategyMonitor, DEFAULT_EXPECTED_DRAWDOWN
from src.strategies.base import StrategyRegistry, StrategyBase


def test_default_expected_drawdown_keys_match_real_strategy_ids():
    for real_id in ["ema_crossover_v1", "credit_spread_v1", "iron_condor_v1", "momentum_v1"]:
        assert real_id in DEFAULT_EXPECTED_DRAWDOWN
        assert DEFAULT_EXPECTED_DRAWDOWN[real_id] > 0
    for old_key in ["ema_crossover", "credit_spread", "iron_condor"]:
        assert old_key not in DEFAULT_EXPECTED_DRAWDOWN, f"stale non-versioned key {old_key} still present"


class _FakeStrategy(StrategyBase):
    def initialize(self):
        pass

    def generate_signal(self, data):
        return None

    def manage_position(self, p, c):
        return None

    def shutdown(self):
        pass


class _Row:
    def __init__(self, pnl, exit_time=None):
        self.pnl = pnl
        self.exit_time = exit_time


class _FakeRepo:
    def __init__(self, rows):
        self.rows = rows

    async def filter(self, **kwargs):
        return self.rows


@pytest.mark.asyncio
async def test_drawdown_check_actually_fires_and_auto_pauses():
    sid = "credit_spread_v1"
    StrategyRegistry._active_instances[sid] = _FakeStrategy(sid, {})
    StrategyRegistry._active_instances[sid].is_active = True

    try:
        # 30 trades constructed to isolate Check 2 (drawdown) from Check 1
        # (profit factor): 20 wins of +1000 (peak 20,000), one -18,000 loss
        # (drawdown = 18,000, > 1.5x credit_spread_v1's 10,000 expected =
        # 15,000 threshold), then 9 more +1000 wins. PF = 29000/18000 ~= 1.61,
        # comfortably above the 0.9 floor, so Check 1 must NOT fire -- only
        # Check 2 should.
        pnls = [1000] * 20 + [-18000] + [1000] * 9
        rows = [_Row(p) for p in pnls]
        monitor = StrategyMonitor(_FakeRepo(rows))

        await monitor._evaluate_strategy(sid)

        inst = StrategyRegistry._active_instances[sid]
        assert inst.is_active is False, "strategy with a real large drawdown must be auto-paused"
        assert "drawdown" in (monitor._pause_reasons.get(sid) or "").lower()
    finally:
        del StrategyRegistry._active_instances[sid]


# ── Manual-resume grace period (2026-08-27, live incident) ──────────────────
# A restart wiped ema_crossover_v1's in-memory auto-pause state, and a
# subsequent manual /activate was found to be immediately undone by the very
# next evaluate_all() cycle -- it re-reads the same last-30-trade rolling
# window that justified the original pause, so a strategy could never close
# a single new trade to start displacing it. mark_resumed() scopes the next
# evaluation to trades closed strictly after the resume.

def test_mark_resumed_clears_stale_pause_bookkeeping():
    sid = "ema_crossover_v1"
    monitor = StrategyMonitor(_FakeRepo([]))
    monitor._pause_reasons[sid] = "Rolling PF 0.063 < floor 0.9 (last 30 trades)"
    monitor._paused_at[sid] = "2026-08-26T13:11:22"

    monitor.mark_resumed(sid)

    assert monitor._pause_reasons.get(sid) is None
    assert monitor._paused_at.get(sid) is None
    assert sid in monitor._resumed_at


@pytest.mark.asyncio
async def test_manual_resume_grace_period_prevents_immediate_re_pause():
    sid = "ema_crossover_v1"
    StrategyRegistry._active_instances[sid] = _FakeStrategy(sid, {})
    StrategyRegistry._active_instances[sid].is_active = True
    try:
        # 30 losing trades, all closed BEFORE the resume -- exactly the
        # disaster batch that justified the original pause.
        old_time = datetime(2026, 8, 26, 13, 0, 0)
        rows = [_Row(-2000, exit_time=old_time) for _ in range(30)]
        monitor = StrategyMonitor(_FakeRepo(rows))

        monitor.mark_resumed(sid)
        await monitor._evaluate_strategy(sid)

        inst = StrategyRegistry._active_instances[sid]
        assert inst.is_active is True, (
            "a fresh manual resume must not be immediately undone by the "
            "exact pre-resume trades that caused the original pause"
        )
    finally:
        del StrategyRegistry._active_instances[sid]


@pytest.mark.asyncio
async def test_manual_resume_still_re_pauses_once_enough_fresh_bad_trades_close():
    sid = "ema_crossover_v1"
    StrategyRegistry._active_instances[sid] = _FakeStrategy(sid, {})
    StrategyRegistry._active_instances[sid].is_active = True
    try:
        resume_time = datetime(2026, 8, 27, 9, 0, 0)
        old_time = resume_time - timedelta(days=1)
        new_time = resume_time + timedelta(hours=1)
        # 20 old losses (must be excluded) + 30 NEW losses since resume
        # (enough to re-evaluate, and bad enough to justify a real re-pause).
        rows = (
            [_Row(-2000, exit_time=old_time) for _ in range(20)]
            + [_Row(-2000, exit_time=new_time) for _ in range(30)]
        )
        monitor = StrategyMonitor(_FakeRepo(rows))
        monitor._resumed_at[sid] = resume_time

        await monitor._evaluate_strategy(sid)

        inst = StrategyRegistry._active_instances[sid]
        assert inst.is_active is False, (
            "the grace period must not be a permanent bypass -- 30 genuinely "
            "fresh losing trades since resume must still trigger a re-pause"
        )
    finally:
        del StrategyRegistry._active_instances[sid]


@pytest.mark.asyncio
async def test_get_report_scopes_trades_in_window_to_after_resume():
    sid = "ema_crossover_v1"
    StrategyRegistry._active_instances[sid] = _FakeStrategy(sid, {})
    StrategyRegistry._active_instances[sid].is_active = True
    try:
        resume_time = datetime(2026, 8, 27, 9, 0, 0)
        old_time = resume_time - timedelta(days=1)
        new_time = resume_time + timedelta(hours=1)
        rows = (
            [_Row(-2000, exit_time=old_time) for _ in range(20)]
            + [_Row(1000, exit_time=new_time) for _ in range(3)]
        )
        monitor = StrategyMonitor(_FakeRepo(rows))
        monitor._resumed_at[sid] = resume_time

        report = await monitor.get_report()

        assert report[sid]["trades_in_window"] == 3
        assert report[sid]["resumed_at"] == resume_time.isoformat()
    finally:
        del StrategyRegistry._active_instances[sid]


def test_activate_endpoint_wires_mark_resumed():
    import inspect
    from src.api.routers import strategy_router
    src = inspect.getsource(strategy_router.activate_strategy)
    assert "mark_resumed" in src
