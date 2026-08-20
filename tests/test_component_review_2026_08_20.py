"""
Component-level health audit (2026-08-20), following the full-system deep
review earlier the same day. User remained skeptical after that review and
asked for the system to be bifurcated into components and each reviewed for
standalone quality/robustness, not just re-hunted for the same class of bug.
6 parallel agents each audited one component (market data, order execution/
brokers, risk/capital, strategies, the live trading engine orchestration
layer, api/main.py lifespan+scheduling) against a rubric of correctness,
robustness, internal consistency after the day's earlier churn, test
coverage, and operational readiness. 4 new concrete bugs survived
verification against the actual code; this file covers them.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.strategies.credit_spread import CreditSpreadStrategy


class _FakeRiskMgr:
    def __init__(self):
        self.released = []

    def release_deployed_capital(self, strategy_name, amount):
        self.released.append((strategy_name, amount))


# ── 1. order_manager.place_order(): DB failure after a successful broker ────
#    call must not downgrade a live order to FAILED.

@pytest.mark.asyncio
async def test_place_order_db_persist_failure_after_broker_success_does_not_mark_failed():
    from src.orders.order_manager import OrderManager
    from src.risk.risk_manager import RiskManager

    class _Row:
        def __init__(self):
            self.id = 1
            self.order_status = "PENDING"
            self.broker_order_id = None
            self.fill_price = None

    class _Repo:
        def __init__(self, row):
            self.row = row
            self.update_calls = 0

        async def create(self, data):
            return self.row

        async def update(self, obj, updates):
            self.update_calls += 1
            # Every attempt to persist the post-broker-success update fails --
            # simulates a sustained transient DB outage right after the order
            # reached the broker.
            raise RuntimeError("simulated DB outage")

    class _Broker:
        async def place_order(self, *a, **kw):
            return "broker-order-live-1"

        async def get_orders(self):
            raise RuntimeError("simulated DB/network hiccup during reconciliation too")

    row = _Row()
    repo = _Repo(row)
    om = OrderManager(_Broker(), RiskManager(initial_capital=300_000.0), repo, repo)

    result = await om.place_order("RELIANCE26AUG2900CE", "BUY", 25, 40.0, strategy_name="ema_crossover_v1")

    # Fixed 2026-08-20 (component review): must NOT be silently orphaned as
    # FAILED with no broker_order_id -- the order is genuinely live at the
    # broker. The in-memory record returned to the caller must reflect that.
    assert result.order_status != "FAILED"
    assert result.broker_order_id == "broker-order-live-1"
    # All 3 retry attempts were made before giving up on the DB write.
    assert repo.update_calls == 3


# ── 2. credit_spread.py must not fire in iron_condor's flat-EMA zone ────────

def test_credit_spread_holds_in_flat_ema_zone_that_belongs_to_iron_condor():
    """Both strategies' docstrings claim 'never fight each other' -- credit_spread
    had no flat_threshold floor, so it could fire a directional spread in the
    exact ATR%/EMA-spread band iron_condor claims exclusively."""
    strat = CreditSpreadStrategy("credit_spread_v1", {})
    strat.initialize()

    # atr_pct = 0.5/500*100 = 0.1% (< low_vol_threshold=1.2, credit-spread territory)
    # ema_spread_pct = |100.05-100|/100*100 = 0.05% (< iron_condor's flat_threshold=0.1%)
    signal = strat.generate_signal({
        "ema20": 100.05, "ema50": 100.0, "close": 500.0, "atr14": 0.5,
    })
    assert signal == "HOLD"


def test_credit_spread_still_fires_outside_the_flat_zone():
    """Guard against over-fixing -- a genuinely trending, low-ATR case must
    still fire as before."""
    strat = CreditSpreadStrategy("credit_spread_v1", {})
    strat.initialize()

    # ema_spread_pct = |105-100|/100*100 = 5% -- well above flat_threshold
    signal = strat.generate_signal({
        "ema20": 105.0, "ema50": 100.0, "close": 500.0, "atr14": 2.0,
    })
    assert signal == "BULL_PUT_SPREAD"


# ── 3. run_signal_cycle: an early failure must not block exits/entries ──────

@pytest.mark.asyncio
async def test_expire_stale_orders_failure_does_not_block_exit_checks():
    """expire_stale_orders() runs before the exit checks in run_signal_cycle --
    an exception there used to abort the whole cycle, including SL/target/
    breach checks for every open position."""
    from src.core.enums import TradingMode

    calls = []

    fake = SimpleNamespace(
        is_running=True, mode=TradingMode.PAPER,
        order_manager=SimpleNamespace(
            expire_stale_orders=AsyncMock(side_effect=RuntimeError("simulated DB blip")),
        ),
        _get_cached_vix=AsyncMock(return_value=None),
        _safe_get_positions=AsyncMock(return_value=[]),
        _refresh_risk_state=AsyncMock(),
        _exit_cycle_lock=__import__("asyncio").Lock(),
        _check_spread_exits=AsyncMock(side_effect=lambda *a: calls.append("spread_exits")),
        _check_condor_exits=AsyncMock(side_effect=lambda *a: calls.append("condor_exits")),
        _check_open_option_exits=AsyncMock(side_effect=lambda *a: calls.append("open_option_exits")),
        _log_portfolio_delta=AsyncMock(),
        _sync_must_track_underlyings=lambda: None,
        strategy_monitor=None,
        regime_detector=None,
        portfolio_analyzer=None,
        _broker_position_state_known=True,
        _persist_ema_state=AsyncMock(),
        _persist_momentum_state=AsyncMock(),
        _ENTRY_WARMUP_MINUTES=0,
        _get_active_symbols=AsyncMock(return_value=[]),
    )

    import src.live_trading.live_trading_engine as lte_module
    import unittest.mock as _mock
    with _mock.patch.object(lte_module, "is_market_open", return_value=True), \
         _mock.patch.object(lte_module, "is_square_off_time", return_value=False), \
         _mock.patch.object(lte_module, "StrategyRegistry") as _sr:
        _sr.get_active_strategies.return_value = {"ema_crossover_v1": object()}
        # Must not raise, and exit checks must still run despite the failure above.
        await LiveTradingEngine.run_signal_cycle(fake)

    assert "spread_exits" in calls
    assert "condor_exits" in calls
    assert "open_option_exits" in calls


# ── 4. Expiry-day square-off must not fabricate a close for a partially- ────
#    closed spread/condor.

class _FakeExpiryPartialFailOrder:
    def __init__(self, contract_that_fails):
        self._fail_contract = contract_that_fails

    def for_contract(self, contract):
        status = "REJECTED_BY_RISK" if contract == self._fail_contract else "OPEN"
        return SimpleNamespace(order_status=status, fill_price=5.0)


@pytest.mark.asyncio
async def test_expiry_day_partial_leg_failure_does_not_clear_gtt_journal_or_capital(monkeypatch):
    from datetime import timedelta
    from src.core.utils import now_ist

    failing_contract = "SPR26AUG200PE"  # the short leg

    async def _fake_place_order(contract, side, qty, price, is_exit_order=False,
                                 strategy_name=None, product_override=None):
        status = "REJECTED_BY_RISK" if contract == failing_contract else "OPEN"
        return SimpleNamespace(order_status=status, fill_price=5.0)

    spread = {
        "journal_id": 400, "short_contract": "SPR26AUG200PE", "long_contract": "SPR26AUG190PE",
        "short_premium": 8.0, "long_premium": 3.0, "lot_size": 50,
        "short_strike": 200, "long_strike": 190, "net_credit": 5.0,
        "strategy_name": "credit_spread_v1", "gtt_id": 999,
    }
    fake = SimpleNamespace(
        _real_fill=LiveTradingEngine._real_fill,
        order_manager=SimpleNamespace(place_order=_fake_place_order),
        risk_manager=_FakeRiskMgr(),
        _peak_premiums={}, _single_leg_journals={},
        _active_spreads={"SPR": spread}, _active_condors={},
        _kite=None, _redis=None,
        _eod_notified_today=False,
    )
    _closed_journals = []
    _notifications = []
    _gtt_cancels = []

    async def _safe_get_positions():
        return [
            {"symbol": "SPR26AUG200PE", "quantity": -50, "avg_price": 8.0},
            {"symbol": "SPR26AUG190PE", "quantity": 50, "avg_price": 3.0},
        ]

    async def _get_market_data(symbol):
        return {"atr14": 5.0}

    def _get_underlying_from_contract(contract):
        return "SPR"

    async def _log_trade_close(journal_id, exit_price, pnl, exit_reason, **kw):
        _closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _persist_state():
        pass

    async def _notify(msg):
        _notifications.append(msg)

    async def _cancel_gtt(gtt_id, contract=""):
        _gtt_cancels.append((gtt_id, contract))

    fake._safe_get_positions = _safe_get_positions
    fake._get_market_data = _get_market_data
    fake._get_underlying_from_contract = _get_underlying_from_contract
    fake._log_trade_close = _log_trade_close
    fake._persist_state = _persist_state
    fake._notify = _notify
    fake._cancel_gtt = _cancel_gtt

    monkeypatch.setattr(
        "src.live_trading.live_trading_engine.get_near_month_expiry",
        lambda: now_ist().replace(tzinfo=None) + timedelta(days=1),  # DTE=1 -> expiry day
    )

    await LiveTradingEngine._square_off_all(fake)

    # Fixed 2026-08-20 (component review): a rejected short-leg close order
    # on expiry day used to still cancel the GTT backstop, journal a
    # fabricated close (falling back to the entry premium for the failed
    # leg), release deployed capital, and drop the structure from tracking
    # -- all while the real short leg was still open with its exchange-level
    # stop now gone. None of that must happen for a partially-closed structure.
    assert _gtt_cancels == []
    assert _closed_journals == []
    assert fake.risk_manager.released == []
    assert "SPR" in fake._active_spreads  # stays tracked for retry

    # A loud, specific alert must fire instead.
    critical = [m for m in _notifications if "CRITICAL" in m and "SPR" in m]
    assert len(critical) == 1


@pytest.mark.asyncio
async def test_expiry_day_full_leg_success_still_clears_normally(monkeypatch):
    """Guard against over-fixing -- a fully-successful expiry-day close must
    still cancel GTTs, journal the close, release capital, and clear tracking."""
    from datetime import timedelta
    from src.core.utils import now_ist

    async def _fake_place_order(contract, side, qty, price, is_exit_order=False,
                                 strategy_name=None, product_override=None):
        return SimpleNamespace(order_status="OPEN", fill_price=5.0)

    spread = {
        "journal_id": 401, "short_contract": "OKSPR26AUG200PE", "long_contract": "OKSPR26AUG190PE",
        "short_premium": 8.0, "long_premium": 3.0, "lot_size": 50,
        "short_strike": 200, "long_strike": 190, "net_credit": 5.0,
        "strategy_name": "credit_spread_v1", "gtt_id": 999,
    }
    _closed_journals = []
    _gtt_cancels = []

    fake = SimpleNamespace(
        _real_fill=LiveTradingEngine._real_fill,
        order_manager=SimpleNamespace(place_order=_fake_place_order),
        risk_manager=_FakeRiskMgr(),
        _peak_premiums={}, _single_leg_journals={},
        _active_spreads={"OKSPR": spread}, _active_condors={},
        _kite=None, _redis=None,
        _eod_notified_today=False,
    )

    async def _safe_get_positions():
        return [
            {"symbol": "OKSPR26AUG200PE", "quantity": -50, "avg_price": 8.0},
            {"symbol": "OKSPR26AUG190PE", "quantity": 50, "avg_price": 3.0},
        ]

    async def _log_trade_close(journal_id, exit_price, pnl, exit_reason, **kw):
        _closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _cancel_gtt(gtt_id, contract=""):
        _gtt_cancels.append((gtt_id, contract))

    fake._safe_get_positions = _safe_get_positions
    fake._get_market_data = AsyncMock(return_value={"atr14": 5.0})
    fake._get_underlying_from_contract = lambda c: "OKSPR"
    fake._log_trade_close = _log_trade_close
    fake._persist_state = AsyncMock()
    fake._notify = AsyncMock()
    fake._cancel_gtt = _cancel_gtt

    monkeypatch.setattr(
        "src.live_trading.live_trading_engine.get_near_month_expiry",
        lambda: now_ist().replace(tzinfo=None) + timedelta(days=1),
    )

    await LiveTradingEngine._square_off_all(fake)

    assert len(_gtt_cancels) == 1
    assert len(_closed_journals) == 1
    assert fake.risk_manager.released == [("credit_spread_v1", (10 - 5.0) * 50)]
    assert "OKSPR" not in fake._active_spreads
