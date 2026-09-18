"""
_detect_gate_bottlenecks()/_check_gate_bottleneck_anomalies() (2026-09-18,
user-reported live incident, "find more bugs before going live").

_check_signal_staleness() already catches a strategy that stops signaling
entirely -- but several of this project's real incidents (the LOW_VOL/
credit_spread_v1 contradiction found and fixed earlier the same day, the
2026-09-04-fixed momentum_v1 RVOL-measurement-bias dead period) had
generate_signal() firing normally the whole time; the candidate died at one
specific, LATER gate, every single time, for days to weeks, before anyone
happened to notice. This watches gate-to-gate conversion specifically.
"""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine


# ── _detect_gate_bottlenecks() -- pure detection logic ──────────────────────

def _days_ago(n):
    from src.core.utils import now_ist
    return now_ist().date() - timedelta(days=n)


def _daily(strategy, gate, day, count):
    return (strategy, gate, day), count


def test_detects_a_sustained_total_blockage_at_one_gate():
    # Mirrors the real 2026-09-18 LOW_VOL/credit_spread_v1 incident shape:
    # lot_size_passed active every day, but the very next gate (vix_passed)
    # stuck at zero for 3 straight days despite having a real track record
    # further back in the lookback window.
    recent = [_days_ago(2), _days_ago(1), _days_ago(0)]
    daily_max = dict([
        _daily("credit_spread_v1", "lot_size_passed", _days_ago(10), 40),
        _daily("credit_spread_v1", "vix_passed", _days_ago(10), 12),  # real track record
        _daily("credit_spread_v1", "lot_size_passed", recent[0], 30),
        _daily("credit_spread_v1", "vix_passed", recent[0], 0),
        _daily("credit_spread_v1", "lot_size_passed", recent[1], 25),
        _daily("credit_spread_v1", "vix_passed", recent[1], 0),
        _daily("credit_spread_v1", "lot_size_passed", recent[2], 20),
        _daily("credit_spread_v1", "vix_passed", recent[2], 0),
    ])

    anomalies = LiveTradingEngine._detect_gate_bottlenecks(
        daily_max, recent, min_daily_passes=5, alert_days=3,
    )

    assert anomalies == [("credit_spread_v1", "lot_size_passed", "vix_passed")]


def test_does_not_flag_a_gate_that_was_never_part_of_the_pipeline():
    # vix_passed has NO nonzero entry anywhere in daily_max -- e.g. a
    # strategy that structurally never reaches this gate (not the same as
    # "used to work, now broken"). Must not be flagged.
    recent = [_days_ago(2), _days_ago(1), _days_ago(0)]
    daily_max = dict([
        _daily("iron_condor_v1", "dte_passed", recent[0], 30),
        _daily("iron_condor_v1", "vix_passed", recent[0], 0),
        _daily("iron_condor_v1", "dte_passed", recent[1], 25),
        _daily("iron_condor_v1", "vix_passed", recent[1], 0),
        _daily("iron_condor_v1", "dte_passed", recent[2], 20),
        _daily("iron_condor_v1", "vix_passed", recent[2], 0),
    ])

    anomalies = LiveTradingEngine._detect_gate_bottlenecks(
        daily_max, recent, min_daily_passes=5, alert_days=3,
    )

    assert anomalies == []


def test_does_not_flag_when_upstream_gate_itself_is_quiet():
    # gate_a below the min-daily-passes floor on one of the recent days --
    # this is "the whole strategy went quiet" (already covered by
    # _check_signal_staleness), not a specific downstream bottleneck.
    recent = [_days_ago(2), _days_ago(1), _days_ago(0)]
    daily_max = dict([
        _daily("credit_spread_v1", "lot_size_passed", _days_ago(10), 40),
        _daily("credit_spread_v1", "vix_passed", _days_ago(10), 12),
        _daily("credit_spread_v1", "lot_size_passed", recent[0], 30),
        _daily("credit_spread_v1", "vix_passed", recent[0], 0),
        _daily("credit_spread_v1", "lot_size_passed", recent[1], 2),  # below floor
        _daily("credit_spread_v1", "vix_passed", recent[1], 0),
        _daily("credit_spread_v1", "lot_size_passed", recent[2], 20),
        _daily("credit_spread_v1", "vix_passed", recent[2], 0),
    ])

    anomalies = LiveTradingEngine._detect_gate_bottlenecks(
        daily_max, recent, min_daily_passes=5, alert_days=3,
    )

    assert anomalies == []


def test_does_not_flag_an_occasional_zero_only_a_sustained_one():
    # gate_b had a nonzero day within the recent window itself -- a real,
    # occasional dip, not a sustained total blockage.
    recent = [_days_ago(2), _days_ago(1), _days_ago(0)]
    daily_max = dict([
        _daily("credit_spread_v1", "lot_size_passed", recent[0], 30),
        _daily("credit_spread_v1", "vix_passed", recent[0], 0),
        _daily("credit_spread_v1", "lot_size_passed", recent[1], 25),
        _daily("credit_spread_v1", "vix_passed", recent[1], 3),  # still alive here
        _daily("credit_spread_v1", "lot_size_passed", recent[2], 20),
        _daily("credit_spread_v1", "vix_passed", recent[2], 0),
    ])

    anomalies = LiveTradingEngine._detect_gate_bottlenecks(
        daily_max, recent, min_daily_passes=5, alert_days=3,
    )

    assert anomalies == []


def test_insufficient_recent_history_returns_no_anomalies():
    # Only 1 of the required 3 recent days has data -- too early to judge
    # (e.g. right after this feature is first deployed).
    recent = [_days_ago(0)]
    daily_max = dict([
        _daily("credit_spread_v1", "lot_size_passed", recent[0], 30),
        _daily("credit_spread_v1", "vix_passed", recent[0], 0),
    ])

    anomalies = LiveTradingEngine._detect_gate_bottlenecks(
        daily_max, recent, min_daily_passes=5, alert_days=3,
    )

    assert anomalies == []


def test_flags_each_affected_strategy_independently():
    recent = [_days_ago(2), _days_ago(1), _days_ago(0)]
    daily_max = {}
    for strategy in ("credit_spread_v1", "iron_condor_v1"):
        daily_max.update(dict([
            _daily(strategy, "dte_passed", _days_ago(10), 40),
            _daily(strategy, "iv_rank_passed", _days_ago(10), 15),
        ]))
        for d, n in zip(recent, (30, 25, 20)):
            daily_max[(strategy, "dte_passed", d)] = n
            daily_max[(strategy, "iv_rank_passed", d)] = 0

    anomalies = LiveTradingEngine._detect_gate_bottlenecks(
        daily_max, recent, min_daily_passes=5, alert_days=3,
    )

    assert sorted(anomalies) == [
        ("credit_spread_v1", "dte_passed", "iv_rank_passed"),
        ("iron_condor_v1", "dte_passed", "iv_rank_passed"),
    ]


def test_healthy_pipeline_produces_no_anomalies():
    recent = [_days_ago(2), _days_ago(1), _days_ago(0)]
    daily_max = {}
    for d, n in zip(recent, (30, 25, 20)):
        daily_max[("ema_crossover_v1", "signal_generated", d)] = n
        daily_max[("ema_crossover_v1", "dte_passed", d)] = n
        daily_max[("ema_crossover_v1", "trade_placed", d)] = max(1, n // 10)

    anomalies = LiveTradingEngine._detect_gate_bottlenecks(
        daily_max, recent, min_daily_passes=5, alert_days=3,
    )

    assert anomalies == []


# ── _check_gate_bottleneck_anomalies() -- DB fetch + notify wiring ──────────

class _FakeGateRow:
    def __init__(self, strategy_name, gate, snapshot_time, pass_count):
        self.strategy_name = strategy_name
        self.gate = gate
        self.snapshot_time = snapshot_time
        self.pass_count = pass_count


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_check_gate_bottleneck_anomalies_notifies_on_a_real_db_backed_bottleneck(monkeypatch):
    from src.core.utils import now_ist
    import datetime as _dt

    rows = []
    for days_back in range(4):
        ts = now_ist().replace(tzinfo=None) - _dt.timedelta(days=days_back, hours=1)
        rows.append(_FakeGateRow("credit_spread_v1", "lot_size_passed", ts, 20 - days_back))
        rows.append(_FakeGateRow("credit_spread_v1", "vix_passed", ts, 12 if days_back == 3 else 0))

    import src.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "AsyncSessionLocal", lambda: _FakeSession(rows))

    notifications = []
    fake = SimpleNamespace(
        _GATE_HEALTH_LOOKBACK_DAYS=LiveTradingEngine._GATE_HEALTH_LOOKBACK_DAYS,
        _GATE_HEALTH_ALERT_DAYS=LiveTradingEngine._GATE_HEALTH_ALERT_DAYS,
        _GATE_HEALTH_MIN_DAILY_PASSES=LiveTradingEngine._GATE_HEALTH_MIN_DAILY_PASSES,
        _detect_gate_bottlenecks=LiveTradingEngine._detect_gate_bottlenecks,
        _notify=AsyncMock(side_effect=lambda m: notifications.append(m)),
    )

    await LiveTradingEngine._check_gate_bottleneck_anomalies(fake)

    assert len(notifications) == 1
    assert "credit_spread_v1" in notifications[0]
    assert "lot_size_passed" in notifications[0]
    assert "vix_passed" in notifications[0]


@pytest.mark.asyncio
async def test_check_gate_bottleneck_anomalies_silent_on_no_history(monkeypatch):
    import src.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "AsyncSessionLocal", lambda: _FakeSession([]))

    notifications = []
    fake = SimpleNamespace(
        _GATE_HEALTH_LOOKBACK_DAYS=LiveTradingEngine._GATE_HEALTH_LOOKBACK_DAYS,
        _GATE_HEALTH_ALERT_DAYS=LiveTradingEngine._GATE_HEALTH_ALERT_DAYS,
        _GATE_HEALTH_MIN_DAILY_PASSES=LiveTradingEngine._GATE_HEALTH_MIN_DAILY_PASSES,
        _notify=AsyncMock(side_effect=lambda m: notifications.append(m)),
    )

    await LiveTradingEngine._check_gate_bottleneck_anomalies(fake)

    assert notifications == []


@pytest.mark.asyncio
async def test_check_gate_bottleneck_anomalies_swallows_db_errors(monkeypatch):
    class _BrokenSession:
        async def __aenter__(self):
            raise ConnectionError("db down")

        async def __aexit__(self, *a):
            return False

    import src.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "AsyncSessionLocal", lambda: _BrokenSession())

    fake = SimpleNamespace(
        _GATE_HEALTH_LOOKBACK_DAYS=LiveTradingEngine._GATE_HEALTH_LOOKBACK_DAYS,
        _GATE_HEALTH_ALERT_DAYS=LiveTradingEngine._GATE_HEALTH_ALERT_DAYS,
        _GATE_HEALTH_MIN_DAILY_PASSES=LiveTradingEngine._GATE_HEALTH_MIN_DAILY_PASSES,
        _notify=AsyncMock(),
    )

    await LiveTradingEngine._check_gate_bottleneck_anomalies(fake)  # must not raise

    fake._notify.assert_not_awaited()
