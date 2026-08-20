"""
Full-system deep review (2026-08-20) -- not diff-scoped like prior rounds.
Seven parallel agents read every core subsystem end-to-end (live_trading_engine.py
in full, order_manager/risk_manager/paper_broker/zerodha broker, the market data
pipeline, all five strategies, and api/main.py's lifespan+scheduling), each
verified against the actual code before fixing. Covers the highest-severity
confirmed findings; see docs/LIVE_TRADING_CHECKLIST.md for the full list.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.strategies.credit_spread import CreditSpreadStrategy
from src.strategies.iron_condor import IronCondorStrategy
from src.risk.strategy_monitor import StrategyMonitor


# ── 1. Exit paths must not fabricate a close on a rejected/failed order ─────
#
# _close_option_positions (reversal exit) / _square_off_all (EOD close) /
# _exit_all_options_for (EXIT-signal close) all placed a SELL order and then
# treated the position as closed unconditionally -- journal popped, capital
# released, trade_journal written -- even if the broker rejected/failed the
# order. Unlike _execute_single_leg_exit (which already checked order_status),
# these three paths had no such check.

class _FakeOrder:
    def __init__(self, status, fill_price=None):
        self.order_status = status
        self.fill_price = fill_price


class _FakeRiskMgr:
    def __init__(self):
        self.released = []

    def release_deployed_capital(self, strategy_name, amount):
        self.released.append((strategy_name, amount))


def _reversal_engine(order_status):
    stub = SimpleNamespace(
        order_manager=SimpleNamespace(place_order=AsyncMock(return_value=_FakeOrder(order_status, 40.0))),
        risk_manager=_FakeRiskMgr(),
        _peak_premiums={"RELIANCE26AUG2900CE": 45.0},
        _single_leg_journals={"RELIANCE26AUG2900CE": {"journal_id": 1, "strategy_name": "ema_crossover_v1"}},
        _kite=None, _redis=None,
        _real_fill=LiveTradingEngine._real_fill,
        _log_trade_close=AsyncMock(),
        _persist_state=AsyncMock(),
    )
    return stub


@pytest.mark.asyncio
async def test_reversal_exit_does_not_journal_close_when_broker_rejects(monkeypatch):
    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quote",
        AsyncMock(return_value=38.0),
    )
    fake = _reversal_engine("REJECTED_BY_RISK")

    async def _safe_get_positions():
        return [{"symbol": "RELIANCE26AUG2900CE", "quantity": 25, "avg_price": 40.0}]
    fake._safe_get_positions = _safe_get_positions

    await LiveTradingEngine._close_option_positions(fake, "RELIANCE", "CE", {"atr14": 20.0})

    # Position must stay tracked -- NOT popped, NOT journaled, NOT capital-released.
    assert "RELIANCE26AUG2900CE" in fake._single_leg_journals
    fake._log_trade_close.assert_not_called()
    assert fake.risk_manager.released == []


@pytest.mark.asyncio
async def test_reversal_exit_closes_normally_when_broker_accepts(monkeypatch):
    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quote",
        AsyncMock(return_value=38.0),
    )
    fake = _reversal_engine("OPEN")

    async def _safe_get_positions():
        return [{"symbol": "RELIANCE26AUG2900CE", "quantity": 25, "avg_price": 40.0}]
    fake._safe_get_positions = _safe_get_positions

    await LiveTradingEngine._close_option_positions(fake, "RELIANCE", "CE", {"atr14": 20.0})

    assert "RELIANCE26AUG2900CE" not in fake._single_leg_journals
    fake._log_trade_close.assert_called_once()
    assert fake.risk_manager.released == [("ema_crossover_v1", 1000.0)]


@pytest.mark.asyncio
async def test_square_off_does_not_journal_close_when_broker_rejects(monkeypatch):
    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quote",
        AsyncMock(return_value=None),
    )
    fake = SimpleNamespace(
        order_manager=SimpleNamespace(place_order=AsyncMock(return_value=_FakeOrder("FAILED"))),
        risk_manager=_FakeRiskMgr(),
        _peak_premiums={"TCS26AUG3800CE": 55.0},
        _single_leg_journals={"TCS26AUG3800CE": {"journal_id": 2, "strategy_name": "momentum_v1"}},
        _active_spreads={}, _active_condors={},
        _kite=None, _redis=None,
        _real_fill=LiveTradingEngine._real_fill,
        _get_underlying_from_contract=lambda self, c: "TCS",
        _get_market_data=AsyncMock(return_value=None),
        _log_trade_close=AsyncMock(),
        _persist_state=AsyncMock(),
        _eod_notified_today=False,
        _notify=AsyncMock(),
    )
    fake._get_underlying_from_contract = LiveTradingEngine._get_underlying_from_contract.__get__(fake)

    async def _safe_get_positions():
        return [{"symbol": "TCS26AUG3800CE", "quantity": 50, "avg_price": 55.0}]
    fake._safe_get_positions = _safe_get_positions

    await LiveTradingEngine._square_off_all(fake)

    # Rejected close -- position must stay tracked, no fabricated journal close.
    assert "TCS26AUG3800CE" in fake._single_leg_journals
    fake._log_trade_close.assert_not_called()
    assert fake.risk_manager.released == []


@pytest.mark.asyncio
async def test_exit_all_options_for_does_not_journal_close_when_broker_rejects(monkeypatch):
    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quote",
        AsyncMock(return_value=None),
    )
    fake = SimpleNamespace(
        order_manager=SimpleNamespace(place_order=AsyncMock(return_value=_FakeOrder("FAILED"))),
        risk_manager=_FakeRiskMgr(),
        _peak_premiums={},
        _single_leg_journals={"CIPLA26AUG1500CE": {"journal_id": 3, "strategy_name": "ema_crossover_v1"}},
        _active_spreads={}, _active_condors={}, _exited_today=set(),
        _kite=None, _redis=None,
        _real_fill=LiveTradingEngine._real_fill,
        _get_market_data=AsyncMock(return_value=None),
        _log_trade_close=AsyncMock(),
        _persist_state=AsyncMock(),
    )

    async def _safe_get_positions():
        return [{"symbol": "CIPLA26AUG1500CE", "quantity": 50, "avg_price": 30.0}]
    fake._safe_get_positions = _safe_get_positions

    await LiveTradingEngine._exit_all_options_for(fake, "CIPLA")

    assert "CIPLA26AUG1500CE" in fake._single_leg_journals
    fake._log_trade_close.assert_not_called()
    assert fake.risk_manager.released == []


# ── 2. Daily-loss kill switch must see real realized/unrealized P&L ─────────
#
# _refresh_risk_state used to sum positions[i]['realized_pnl']/['unrealized_pnl']
# -- keys neither broker ever populates (ZerodhaBroker passes through Kite's
# raw dict, real fields are 'realised'/'unrealised'/'pnl'; PaperBroker's
# positions only carry symbol/quantity/avg_price) -- so daily_realized_pnl was
# silently always 0.0 regardless of real losses.

@pytest.mark.asyncio
async def test_refresh_risk_state_ignores_bogus_position_dict_keys_uses_journal_instead():
    calls = {}

    class _RM:
        def update_state(self, positions, realized, unrealized):
            calls["realized"] = realized
            calls["unrealized"] = unrealized

    fake = SimpleNamespace(
        risk_manager=_RM(), _redis=object(),
        _todays_realized_pnl=AsyncMock(return_value=-12_345.67),
        _compute_engine_unrealized_pnl=AsyncMock(return_value=-500.0),
    )
    # Positions carry the WRONG keys a broker would never actually populate --
    # if the fix regresses, these would get summed instead of the journal-based figure.
    positions = [
        {"symbol": "X", "quantity": 10, "avg_price": 100.0, "realized_pnl": 999999, "unrealized_pnl": 999999},
    ]

    await LiveTradingEngine._refresh_risk_state(fake, positions)

    assert calls["realized"] == -12_345.67
    assert calls["unrealized"] == -500.0


@pytest.mark.asyncio
async def test_compute_engine_unrealized_pnl_covers_single_leg_positions_too():
    """Previously only summed _active_spreads/_active_condors -- a single-leg
    position with no spread/condor open contributed nothing at all."""
    redis = SimpleNamespace(get=AsyncMock(side_effect=lambda k: "45.0" if "optltp:XCE" in k else None))
    fake = SimpleNamespace()
    positions = [{"symbol": "XCE", "quantity": 25, "avg_price": 40.0}]  # long, up Rs5/share

    unrealized = await LiveTradingEngine._compute_engine_unrealized_pnl(fake, redis, positions)

    assert abs(unrealized - 125.0) < 0.01  # (45-40)*25


# ── 3. expire_stale_orders() must not retry an order that already filled ────

class _SyncOrderManagerLike:
    """Minimal OrderManager stand-in exercising the real sync_orders()/
    expire_stale_orders() bound methods against a fake repo/broker."""
    pass


@pytest.mark.asyncio
async def test_expire_stale_orders_syncs_before_treating_an_order_as_expired(monkeypatch):
    from src.orders.order_manager import OrderManager
    from src.risk.risk_manager import RiskManager
    from datetime import datetime, timedelta

    class _Row:
        def __init__(self):
            self.id = 1
            self.symbol = "SBIN26AUG800CE"
            self.side = "BUY"
            self.quantity = 25
            self.price = 100.0
            self.broker_order_id = "bo-1"
            self.order_status = "OPEN"
            self.fill_price = None
            self.created_at = datetime.utcnow() - timedelta(minutes=10)

    class _Repo:
        def __init__(self, row):
            self.row = row
            self.updates = []

        async def filter(self, **kw):
            return [self.row] if self.row.order_status == "OPEN" else []

        async def get_by_id(self, oid):
            return self.row

        async def update(self, obj, updates):
            self.updates.append(updates)
            for k, v in updates.items():
                setattr(obj, k, v)
            return obj

        async def create(self, data):
            return self.row

    class _Broker:
        """cancel_order fails (order already filled at the broker); get_orders
        reflects that real COMPLETE status for sync_orders() to pick up."""
        async def cancel_order(self, order_id):
            return False

        async def get_orders(self):
            return [{"order_id": "bo-1", "status": "COMPLETE", "fill_price": 101.0}]

        async def place_order(self, *a, **kw):
            raise AssertionError("must not place a duplicate retry order for an already-filled order")

    row = _Row()
    repo = _Repo(row)
    om = OrderManager(_Broker(), RiskManager(initial_capital=300_000.0), repo, repo)

    cancelled = await om.expire_stale_orders()

    # The order resolved to COMPLETED via sync -- must not be marked EXPIRED
    # or retried (which would have placed a genuine duplicate order).
    assert row.order_status == "COMPLETED"
    assert cancelled == 0


# ── 4. Multi-leg exit retry must not resend orders for already-closed legs ──

class _FakeMultiLegOrder:
    def __init__(self, status, fill_price):
        self.order_status = status
        self.fill_price = fill_price


@pytest.mark.asyncio
async def test_spread_exit_retry_does_not_resend_order_for_already_closed_leg(monkeypatch):
    from datetime import timedelta
    from src.core.utils import now_ist

    calls = []

    async def _fake_place_order(contract, side, qty, price, is_spread_leg=False, is_exit_order=False):
        calls.append(contract)
        if contract == "TITAN26SEP4650PE":
            return _FakeMultiLegOrder("OPEN", 37.44)   # short leg: succeeds
        return _FakeMultiLegOrder("REJECTED_BY_RISK", None)  # long leg: rejected

    spread = {
        "short_contract": "TITAN26SEP4650PE", "long_contract": "TITAN26SEP4400PE",
        "short_premium": 82.0, "long_premium": 13.0, "net_credit": 69.0,
        "short_strike": 4650, "long_strike": 4400, "option_type": "PE",
        "spread_type": "BULL_PUT_SPREAD", "lot_size": 175,
        "entry_vix": 0.0, "gtt_id": None, "strategy_name": "credit_spread_v1",
    }
    fake = SimpleNamespace(
        _real_fill=LiveTradingEngine._real_fill,
        order_manager=SimpleNamespace(place_order=_fake_place_order),
        risk_manager=_FakeRiskMgr(),
        _active_spreads={"TITAN": spread}, _active_condors={},
        _exited_today=set(), _profit_closed_today=set(), _close_on_first_cycle=set(),
        _kite=None, _redis=None, _ltp_poller=None,
        _get_market_data=AsyncMock(return_value={"close": 4900.0, "atr14": 20.0}),
        _get_cached_vix=AsyncMock(return_value=None),
        _log_trade_close=AsyncMock(), _persist_state=AsyncMock(),
        _notify=AsyncMock(), _cancel_gtt=AsyncMock(),
        _safe_get_positions=AsyncMock(return_value=[
            {"symbol": "TITAN26SEP4650PE", "quantity": -175, "avg_price": 82.0},
            {"symbol": "TITAN26SEP4400PE", "quantity": 175, "avg_price": 13.0},
        ]),
    )
    monkeypatch.setattr(
        "src.live_trading.live_trading_engine.get_near_month_expiry",
        lambda: now_ist().replace(tzinfo=None) + timedelta(days=11),
    )
    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quote",
        AsyncMock(side_effect=lambda contract, kite, redis: {
            "TITAN26SEP4650PE": 36.35, "TITAN26SEP4400PE": 11.00,
        }[contract]),
    )

    # First pass: short leg fills, long leg rejected -- must stay tracked,
    # remembering the short leg's real fill.
    await LiveTradingEngine._check_spread_exits(fake, active_strategies={})

    assert "TITAN" in fake._active_spreads
    assert fake._active_spreads["TITAN"]["_short_exit_fill"] == pytest.approx(37.44)
    assert calls.count("TITAN26SEP4650PE") == 1
    fake._log_trade_close.assert_not_called()

    # Second pass (retry cycle): long leg now succeeds too -- the short leg
    # must NOT be resubmitted (it's already flat; resubmitting would open an
    # unintended new position on it).
    calls.clear()

    async def _fake_place_order_2(contract, side, qty, price, is_spread_leg=False, is_exit_order=False):
        calls.append(contract)
        assert contract != "TITAN26SEP4650PE", "already-closed leg must not be resubmitted"
        return _FakeMultiLegOrder("OPEN", 10.67)

    fake.order_manager.place_order = _fake_place_order_2

    await LiveTradingEngine._check_spread_exits(fake, active_strategies={})

    assert calls == ["TITAN26SEP4400PE"]
    assert "TITAN" not in fake._active_spreads
    fake._log_trade_close.assert_called_once()


# ── 5. NaN/None atr14 must not bypass the low-volatility gate ───────────────

_NAN = float("nan")


@pytest.mark.parametrize("atr_value", [_NAN, None])
def test_credit_spread_holds_on_nan_or_none_atr(atr_value):
    strat = CreditSpreadStrategy("credit_spread_v1", {})
    strat.initialize()
    signal = strat.generate_signal({
        "ema20": 105.0, "ema50": 100.0, "close": 500.0, "atr14": atr_value,
    })
    assert signal == "HOLD"


@pytest.mark.parametrize("atr_value", [_NAN, None])
def test_iron_condor_holds_on_nan_or_none_atr(atr_value):
    strat = IronCondorStrategy("iron_condor_v1", {})
    strat.initialize()
    signal = strat.generate_signal({
        "ema20": 100.1, "ema50": 100.0, "close": 500.0, "atr14": atr_value,
    })
    assert signal == "HOLD"


def test_credit_spread_still_fires_on_valid_low_atr():
    """Guard against over-fixing -- a genuinely low, valid ATR must still
    fire a directional signal as before."""
    strat = CreditSpreadStrategy("credit_spread_v1", {})
    strat.initialize()
    signal = strat.generate_signal({
        "ema20": 105.0, "ema50": 100.0, "close": 500.0, "atr14": 2.0,  # 0.4% of close
    })
    assert signal == "BULL_PUT_SPREAD"


# ── 6. StrategyMonitor must not crash (and must isolate) on pf=None ─────────

@pytest.mark.asyncio
async def test_strategy_monitor_recovery_log_survives_pf_none():
    """_profit_factor() legitimately returns None with zero losing trades in
    the window -- the 'now healthy' log line used to format it with `:.3f`
    unconditionally, raising an uncaught TypeError."""
    monitor = StrategyMonitor(trade_journal_repo=None)
    monitor._pause_reasons["ema_crossover_v1"] = "previously paused for testing"

    # All-winning trades -> _profit_factor() returns None (no losing trades
    # to form a denominator), _rolling_drawdown() returns a real number.
    trades = [{"pnl": 100.0} for _ in range(10)]
    monitor._load_recent_trades = AsyncMock(return_value=trades)

    # Must not raise.
    await monitor._evaluate_strategy("ema_crossover_v1")


@pytest.mark.asyncio
async def test_evaluate_all_isolates_one_strategys_exception_from_the_rest(monkeypatch):
    monitor = StrategyMonitor(trade_journal_repo=None)

    class _FakeStrategy:
        is_active = True

    monkeypatch.setattr(
        "src.risk.strategy_monitor.StrategyRegistry.get_active_strategies",
        lambda: {"broken_strategy": _FakeStrategy(), "healthy_strategy": _FakeStrategy()},
    )

    evaluated = []

    async def _fake_evaluate(strategy_id):
        evaluated.append(strategy_id)
        if strategy_id == "broken_strategy":
            raise TypeError("simulated pf=None style crash")

    monitor._evaluate_strategy = _fake_evaluate

    # Must not raise, and must still evaluate the second strategy.
    await monitor.evaluate_all()

    assert evaluated == ["broken_strategy", "healthy_strategy"]
