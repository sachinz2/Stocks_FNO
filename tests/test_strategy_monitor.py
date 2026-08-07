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
    def __init__(self, pnl):
        self.pnl = pnl


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
