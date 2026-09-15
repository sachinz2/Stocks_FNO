"""
Regression tests for bugs found during the 2026-09-15 unscoped deep review
(the user explicitly asked to widen the hunt beyond the RVOL/ADX validation-
flag bug class already fixed this week: "i was not talking about just
similar bugs, any bugs... can you do a deep review").

Covers: expire_stale_orders() falling through to EXPIRE+RETRY an order that
a re-sync just confirmed is still genuinely OPEN at the broker (order_manager.py).
The BaseRepository.update() session.merge() bug has its own regression test
in tests/test_repositories.py.
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.risk.risk_manager import RiskManager
from src.orders.order_manager import OrderManager, ORDER_EXPIRY_MINUTES
from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.utils import now_ist


# ── Event/earnings blackout must count TRADING days, not calendar days ─────

def test_event_blackout_cutoff_skips_weekends_not_just_calendar_days():
    # Fixed 2026-09-15 (deep review): has_event_within_days()'s cutoff used
    # to be `today + timedelta(days=days)` -- plain calendar days -- while
    # every call site's comment promises "5 trading days". An earnings date
    # 4 TRADING days away (Fri/Mon/Tue/Wed) but 6 CALENDAR days away from a
    # Thursday used to fall outside a naive today+5 cutoff, letting
    # credit_spread_v1/iron_condor_v1 open a new position just 1 trading
    # day before earnings.
    from datetime import date, timedelta
    from src.market_data.event_calendar import _add_trading_days, _is_nse_holiday

    # Thursday 2026-09-17 (2026-09-15 is a Tuesday per system clock; pick a
    # known Thursday explicitly rather than relying on "today").
    thursday = date(2026, 9, 17)
    assert thursday.weekday() == 3
    cutoff_5td = _add_trading_days(thursday, 5)

    # Independently recount the 5 trading days using the same holiday
    # checker _add_trading_days delegates to, rather than hand-picking an
    # expected date (the installed exchange_calendars backend, if present,
    # may differ slightly from the hardcoded fallback list).
    d, counted = thursday, 0
    while counted < 5:
        d += timedelta(days=1)
        if d.weekday() < 5 and not _is_nse_holiday(d):
            counted += 1
    assert cutoff_5td == d

    # Old (buggy) calendar-day cutoff was a fixed +5, oblivious to the
    # weekend/holidays in between -- must always land strictly earlier than
    # (or at best equal to) the real trading-day cutoff once a weekend is
    # crossed, never later.
    old_buggy_cutoff = thursday + timedelta(days=5)
    assert cutoff_5td > old_buggy_cutoff, (
        "a 5-trading-day window that crosses a weekend must extend further "
        "than a naive 5-calendar-day window"
    )


# ── ComboNotifier.enabled must reflect EMAIL specifically ──────────────────

def test_combo_notifier_enabled_reflects_email_only_not_telegram():
    # Fixed 2026-09-15 (deep review): enabled used to be
    # `self.email.enabled or self.telegram.enabled`, directly contradicting
    # this class's own documented intent (see the comment on `paused`) that
    # both properties must reflect EMAIL specifically -- admin_router's
    # /email-alerts endpoints and the dashboard's "Email Alerts" panel are
    # named and documented as email-specific. With only Telegram configured,
    # the old code reported email as "configured"/active while
    # EmailNotifier.send() was silently returning False on every call.
    from types import SimpleNamespace
    from src.notifications.combo_notifier import ComboNotifier

    notifier = ComboNotifier.__new__(ComboNotifier)
    notifier.email = SimpleNamespace(paused=False, enabled=False)
    notifier.telegram = SimpleNamespace(enabled=True)

    assert notifier.enabled is False, (
        "enabled must reflect email specifically -- a Telegram-only setup "
        "must not report email as configured/active"
    )


# ── stocks_router / market_data_router: honest 501, not fabricated data ────

def test_stocks_router_endpoints_return_501_not_fabricated_data():
    # Fixed 2026-09-15 (deep review): GET /stocks always returned one
    # hardcoded SBIN row; GET /stocks/{symbol} returned "State Bank of
    # India" for ANY symbol -- fabricated 200s indistinguishable from real
    # data. Same fix pattern as risk_router/backtest_router (2026-08-21).
    import asyncio
    from fastapi import HTTPException
    from src.api.routers.stocks_router import get_stocks, get_stock

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_stocks())
    assert exc_info.value.status_code == 501

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_stock("RELIANCE"))
    assert exc_info.value.status_code == 501


def test_market_data_router_endpoints_return_501_not_fabricated_data():
    import asyncio
    from fastapi import HTTPException
    from src.api.routers.market_data_router import get_market_data, load_historical_data
    from src.api.dto.schemas import MarketDataLoadRequest

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_market_data("RELIANCE"))
    assert exc_info.value.status_code == 501

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(load_historical_data(MarketDataLoadRequest(
            symbol="RELIANCE", from_date="2024-01-01", to_date="2024-06-01",
        )))
    assert exc_info.value.status_code == 501


# ── Max pain: call/put payout formulas were swapped ─────────────────────────

def test_max_pain_call_put_payout_formulas_are_not_swapped():
    # Fixed 2026-09-15 (deep review): the old code computed max(0, k -
    # test_price) (the PUT payout shape) against call_oi, and max(0,
    # test_price - k) (the CALL payout shape) against put_oi -- backwards.
    # With only heavy OTM-call OI at 90/100/110 and no puts, the true max
    # pain (minimizing total payout to option holders) is strike 90 -- the
    # buggy code minimized the wrong-signed total and picked strike 110,
    # the opposite end of the range.
    from src.market_data.nse_oi import _calculate_max_pain

    call_oi = {90: 100, 100: 1000, 110: 100}
    put_oi: dict = {}
    assert _calculate_max_pain(call_oi, put_oi) == 90


# ── signals_router: manual generation must not fabricate signal rows ───────

def test_generate_signals_returns_501_not_fabricated_rows():
    # Fixed 2026-09-15 (deep review): POST /signals/generate inserted one
    # hardcoded signal_type="BUY", confidence=0.75 row per fno_enabled stock
    # regardless of any real strategy logic, and was the ONLY writer to the
    # `signals` table -- every row GET /signals ever returned was fake, with
    # no request.strategy validation against real registered strategies.
    import asyncio
    from fastapi import HTTPException
    from src.api.routers.signals_router import generate_signals
    from src.api.dto.schemas import SignalGenerateRequest

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(generate_signals(SignalGenerateRequest(strategy="momentum_v1")))
    assert exc_info.value.status_code == 501


class _FakeRiskMgr:
    def __init__(self):
        self.released = []

    def release_deployed_capital(self, strategy_name, amount):
        self.released.append((strategy_name, amount))


class _FakeMultiLegOrder:
    def __init__(self, status, fill_price):
        self.order_status = status
        self.fill_price = fill_price


class _Row:
    _next_id = 1

    def __init__(self, **kw):
        self.id = _Row._next_id
        _Row._next_id += 1
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeRepo:
    def __init__(self):
        self.rows = []

    async def create(self, data):
        row = _Row(**data)
        self.rows.append(row)
        return row

    async def update(self, obj, data):
        for k, v in data.items():
            setattr(obj, k, v)
        return obj

    async def get_by_id(self, id):
        return next((r for r in self.rows if r.id == id), None)

    async def filter(self, limit=None, order_by=None, **kwargs):
        out = [r for r in self.rows if all(getattr(r, k, None) == v for k, v in kwargs.items())]
        if limit is not None:
            out = out[:limit]
        return out


class _FakeBrokerCancelFailsStillOpen:
    """A broker whose cancel_order() always fails (transient outage), and
    whose get_orders() genuinely still reports the order OPEN -- i.e. the
    cancel failed for a real reason, not because the order already resolved.
    """

    def __init__(self):
        self.placed = []

    async def place_order(self, symbol, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None, client_order_id=None):
        self.placed.append((symbol, side, qty, price))
        return f"bo-{len(self.placed)}"

    async def cancel_order(self, order_id):
        return False

    async def get_positions(self):
        return []

    async def get_orders(self):
        return [{"order_id": "bo-1", "status": "OPEN"}]


def test_expire_stale_orders_does_not_expire_and_retry_a_genuinely_still_open_order():
    # Fixed 2026-09-15 (deep review): previously, when cancel_order() failed
    # and the re-sync confirmed the order was STILL "OPEN" at the broker
    # (a real, resolved failure to cancel -- not the "it already resolved"
    # case the surrounding comment describes), the code fell through anyway
    # and marked the order EXPIRED, then retried it -- placing a genuine
    # duplicate order at the broker while the original stayed live. Worse,
    # the original order's local status ("EXPIRED") permanently removes it
    # from sync_orders()'s OPEN-only query, so a real fill on it afterward
    # would go untracked forever. Only a successful cancel, or a re-sync
    # that positively confirms a terminal status, may lead to expiry+retry.
    order_repo = _FakeRepo()
    rm = RiskManager(initial_capital=300_000.0)
    broker = _FakeBrokerCancelFailsStillOpen()
    om = OrderManager(broker, rm, order_repo, order_repo)

    db_order = asyncio.run(om.place_order(
        "SBIN26AUG800CE", "BUY", 500, 40.0, strategy_name="momentum_v1",
    ))
    db_order.created_at = datetime.utcnow() - timedelta(minutes=ORDER_EXPIRY_MINUTES + 1)
    assert db_order.order_status == "OPEN"

    cancelled_count = asyncio.run(om.expire_stale_orders())

    assert db_order.order_status == "OPEN", (
        "an order confirmed still OPEN at the broker after a failed cancel "
        "must not be marked EXPIRED"
    )
    assert cancelled_count == 0
    assert len(broker.placed) == 1, (
        "no retry order should be placed for an order that is still "
        "genuinely live at the broker -- doing so creates a duplicate position"
    )


# ── Stale GTT backstop must be cancelled the moment its leg goes flat, ─────
# ── not only once the whole structure fully closes ─────────────────────────

@pytest.mark.asyncio
async def test_spread_exit_cancels_short_leg_gtt_immediately_on_partial_failure(monkeypatch):
    # Fixed 2026-09-15 (deep review): the GTT backstop is placed on the
    # short leg only. If the short leg's exit fills but the long leg's
    # exit is rejected (a real, previously-tested scenario -- see
    # test_spread_exit_retry_does_not_resend_order_for_already_closed_leg
    # in test_deep_review_2026_08_20.py), the structure correctly stays
    # tracked for retry -- but the GTT on the now-flat short contract used
    # to stay armed until BOTH legs closed. If the long leg's rejection
    # persists for multiple cycles while the underlying keeps moving, that
    # stale GTT can fire on its own at the exchange, opening a brand-new
    # position this engine never tracks. It must be cancelled the moment
    # the short leg itself goes flat.
    cancel_calls = []

    async def _fake_cancel_gtt(gtt_id, contract=""):
        cancel_calls.append((gtt_id, contract))

    async def _fake_place_order(contract, side, qty, price, is_spread_leg=False, is_exit_order=False):
        if contract == "TITAN26SEP4650PE":
            return _FakeMultiLegOrder("OPEN", 37.44)   # short leg: succeeds
        return _FakeMultiLegOrder("REJECTED_BY_RISK", None)  # long leg: rejected

    spread = {
        "short_contract": "TITAN26SEP4650PE", "long_contract": "TITAN26SEP4400PE",
        "short_premium": 82.0, "long_premium": 13.0, "net_credit": 69.0,
        "short_strike": 4650, "long_strike": 4400, "option_type": "PE",
        "spread_type": "BULL_PUT_SPREAD", "lot_size": 175,
        "entry_vix": 0.0, "gtt_id": 555, "strategy_name": "credit_spread_v1",
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
        _notify=AsyncMock(), _cancel_gtt=_fake_cancel_gtt,
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

    await LiveTradingEngine._check_spread_exits(fake, active_strategies={})

    assert "TITAN" in fake._active_spreads  # stays tracked, long leg retried next cycle
    assert cancel_calls == [(555, "TITAN26SEP4650PE")], (
        "the short leg's GTT must be cancelled the moment it closes, "
        "not deferred until the whole spread is flat"
    )
    assert fake._active_spreads["TITAN"]["gtt_id"] is None


@pytest.mark.asyncio
async def test_condor_exit_cancels_each_short_legs_gtt_independently_on_partial_failure(monkeypatch):
    # Same fix as the credit-spread case, but a condor has TWO independent
    # short-leg GTTs (put side and call side) -- each must be cancelled as
    # soon as ITS OWN short leg closes, regardless of the other wing's state.
    cancel_calls = []

    async def _fake_cancel_gtt(gtt_id, contract=""):
        cancel_calls.append((gtt_id, contract))

    async def _fake_place_order(contract, side, qty, price, is_spread_leg=False, is_exit_order=False):
        if contract in ("TITAN26SEP4650PE", "TITAN26SEP5200CE"):
            return _FakeMultiLegOrder("OPEN", 20.0)  # both short legs: succeed
        return _FakeMultiLegOrder("REJECTED_BY_RISK", None)  # both long legs: rejected

    condor = {
        "put_short_contract": "TITAN26SEP4650PE", "put_long_contract": "TITAN26SEP4400PE",
        "call_short_contract": "TITAN26SEP5200CE", "call_long_contract": "TITAN26SEP5400CE",
        "put_short_premium": 82.0, "put_long_premium": 13.0,
        "call_short_premium": 79.0, "call_long_premium": 12.0,
        "put_short_strike": 4650, "put_long_strike": 4400,
        "call_short_strike": 5200, "call_long_strike": 5400,
        "net_credit": 136.0, "lot_size": 175, "entry_vix": 0.0,
        "strategy_name": "iron_condor_v1",
        "put_short_gtt_id": 111, "call_short_gtt_id": 222,
    }
    fake = SimpleNamespace(
        _real_fill=LiveTradingEngine._real_fill,
        order_manager=SimpleNamespace(place_order=_fake_place_order),
        risk_manager=_FakeRiskMgr(),
        _active_spreads={}, _active_condors={"TITAN": condor},
        _exited_today=set(), _profit_closed_today=set(), _close_on_first_cycle=set(),
        _kite=None, _redis=None, _ltp_poller=None,
        _get_market_data=AsyncMock(return_value={"close": 4900.0, "atr14": 20.0}),
        _get_cached_vix=AsyncMock(return_value=None),
        _log_trade_close=AsyncMock(), _persist_state=AsyncMock(),
        _notify=AsyncMock(), _cancel_gtt=_fake_cancel_gtt,
        _safe_get_positions=AsyncMock(return_value=[
            {"symbol": "TITAN26SEP4650PE", "quantity": -175, "avg_price": 82.0},
            {"symbol": "TITAN26SEP4400PE", "quantity": 175, "avg_price": 13.0},
            {"symbol": "TITAN26SEP5200CE", "quantity": -175, "avg_price": 79.0},
            {"symbol": "TITAN26SEP5400CE", "quantity": 175, "avg_price": 12.0},
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
            "TITAN26SEP5200CE": 34.00, "TITAN26SEP5400CE": 10.50,
        }[contract]),
    )

    await LiveTradingEngine._check_condor_exits(fake, active_strategies={})

    assert "TITAN" in fake._active_condors
    assert sorted(cancel_calls) == sorted([
        (111, "TITAN26SEP4650PE"), (222, "TITAN26SEP5200CE"),
    ]), "both short legs' GTTs must be cancelled independently once each goes flat"
    assert fake._active_condors["TITAN"]["put_short_gtt_id"] is None
    assert fake._active_condors["TITAN"]["call_short_gtt_id"] is None
