"""
Deep review (2026-09-16) -- six silent bugs found by a 3-way parallel audit
of the live trading pipeline (exit/position management, market-data
pipeline, risk/order management), each independently verified against the
real code before being fixed here. See individual fix comments in the
source files for full failure-scenario writeups; these tests pin the
corrected behavior.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.market_data.ltp_poller import LTPPoller
from src.market_data.rs_ranker import RSRanker, _RS_HISTORY_DAYS
from src.orders.order_manager import OrderManager
from src.risk.risk_manager import RiskManager
from src.strategies.credit_spread import CreditSpreadStrategy
from src.strategies.iron_condor import IronCondorStrategy


# ── 1. Unscaled 5-min ATR compared against a daily-scale threshold ──────────
# credit_spread_v1 / iron_condor_v1's "only trade in low volatility" gate was
# structurally a no-op: atr14 is the raw 5-min-bar ATR (~0.2-0.4% typical),
# compared directly against low_vol_threshold=1.2 (daily-equivalent scale) --
# a raw 5-min ATR% of 1.2% is a ~10.4%-daily-equivalent move, essentially
# never happens.

def test_credit_spread_blocks_on_genuinely_high_daily_equivalent_atr():
    strat = CreditSpreadStrategy("credit_spread_v1", {})
    strat.initialize()
    # atr14=1.0 on close=500 -> raw 0.2%, but daily-equivalent (*8.66) ~= 1.73%,
    # above low_vol_threshold=1.2% -- must now correctly HOLD.
    signal = strat.generate_signal({
        "ema20": 105.0, "ema50": 100.0, "close": 500.0, "atr14": 1.0,
    })
    assert signal == "HOLD"


def test_credit_spread_still_fires_on_genuinely_low_daily_equivalent_atr():
    strat = CreditSpreadStrategy("credit_spread_v1", {})
    strat.initialize()
    # atr14=0.3 on close=500 -> daily-equivalent ~= 0.52%, comfortably low.
    signal = strat.generate_signal({
        "ema20": 105.0, "ema50": 100.0, "close": 500.0, "atr14": 0.3,
    })
    assert signal == "BULL_PUT_SPREAD"


def test_iron_condor_blocks_on_genuinely_high_daily_equivalent_atr():
    strat = IronCondorStrategy("iron_condor_v1", {})
    strat.initialize()
    signal = strat.generate_signal({
        "ema20": 100.02, "ema50": 100.0, "close": 500.0, "atr14": 1.0,
    })
    assert signal == "HOLD"


def test_iron_condor_still_fires_on_genuinely_low_daily_equivalent_atr():
    strat = IronCondorStrategy("iron_condor_v1", {})
    strat.initialize()
    signal = strat.generate_signal({
        "ema20": 100.02, "ema50": 100.0, "close": 500.0, "atr14": 0.3,
    })
    assert signal == "IRON_CONDOR"


def test_score_all_spread_and_condor_scores_zero_out_on_high_daily_atr():
    # atr14=1.0 on close=100 -> daily-equivalent ~= 8.66%, way above
    # _LOW_VOL_THRESHOLD -- spread/condor scores must both be zero.
    tick = {"close": 100.0, "atr14": 1.0, "ema20": 100.0, "ema50": 100.0, "adx14": 10.0}
    ema_score, spread_score, condor_score, _ = LTPPoller._score_all(tick)
    assert spread_score == 0.0
    assert condor_score == 0.0


def test_score_all_ema_score_still_uses_raw_unscaled_atr():
    # ema_score's own weighting (atr_pct * 0.3) was independently calibrated
    # to raw 5-min-bar ATR% magnitude by the 2026-08-21 proximity-dominant
    # redesign -- it must NOT be daily-scaled (that would let the ATR term
    # swamp the proximity term, undoing that redesign). Two ticks with
    # comparable ATR (1.5% vs 1.0% raw) where the ONLY meaningful difference
    # is proximity to crossing -- proximity must still decide the winner.
    far  = {"close": 100.0, "atr14": 1.5, "ema20": 100.4,  "ema50": 100.0, "adx14": 10.0}
    near = {"close": 100.0, "atr14": 1.0, "ema20": 100.05, "ema50": 100.0, "adx14": 10.0}
    ema_far, _, _, _ = LTPPoller._score_all(far)
    ema_near, _, _, _ = LTPPoller._score_all(near)
    assert ema_near > ema_far


# ── 2. Capital reserved at quote price, released at fill price ──────────────

class _FillDivergesFromQuoteBroker:
    """PaperBroker-like: accepts the order, then reports a DIFFERENT real
    fill price on the immediate get_orders() reconciliation -- simulating
    bid-ask slippage on a real fill vs. the pre-slippage quote passed in."""
    def __init__(self, real_fill_price):
        self._real_fill_price = real_fill_price

    async def place_order(self, symbol, side, quantity, price, is_exit_order=False,
                           strategy_name=None, product_override=None, client_order_id=None):
        return "bo-1"

    async def get_orders(self):
        return [{
            "order_id": "bo-1", "status": "COMPLETE",
            "fill_price": self._real_fill_price, "filled_quantity": 500,
        }]


class _FakeCapitalRepo:
    def __init__(self):
        self.rows = []

    async def create(self, data):
        row = SimpleNamespace(id=len(self.rows) + 1, **data)
        self.rows.append(row)
        return row

    async def update(self, obj, data):
        for k, v in data.items():
            setattr(obj, k, v)
        return obj

    async def get_by_id(self, id):
        return next((r for r in self.rows if r.id == id), None)

    async def filter(self, limit=None, order_by=None, **kwargs):
        # Used as both order_repo and audit_repo in these tests -- also
        # backs _get_retry_context()'s ORDER_RECEIVED audit-row lookup.
        out = [r for r in self.rows if all(getattr(r, k, None) == v for k, v in kwargs.items())]
        out.reverse()  # id DESC, matching real repo's order_by="id DESC"
        if limit is not None:
            out = out[:limit]
        return out


@pytest.mark.asyncio
async def test_deployed_capital_uses_the_real_fill_not_the_quote():
    rm = RiskManager(initial_capital=300_000.0)
    repo = _FakeCapitalRepo()
    om = OrderManager(_FillDivergesFromQuoteBroker(real_fill_price=42.50), rm, repo, repo)

    await om.place_order("SBIN26SEP800CE", "BUY", 500, 40.0, strategy_name="momentum_v1")

    # Must reserve 500 * 42.50 (real fill), NOT 500 * 40.0 (quote) -- matching
    # the basis every exit path already releases at (pos["avg_price"]).
    assert rm._strategy_deployed.get("momentum_v1") == pytest.approx(500 * 42.50)


@pytest.mark.asyncio
async def test_deployed_capital_falls_back_to_quote_when_fill_not_yet_known():
    # Real broker, order genuinely still pending -- get_orders() finds
    # nothing yet. Must still reserve capital at the quote (unchanged
    # behavior for this case).
    class _StillPendingBroker:
        async def place_order(self, *a, **kw):
            return "bo-1"

        async def get_orders(self):
            return []

    rm = RiskManager(initial_capital=300_000.0)
    repo = _FakeCapitalRepo()
    om = OrderManager(_StillPendingBroker(), rm, repo, repo)

    await om.place_order("SBIN26SEP800CE", "BUY", 500, 40.0, strategy_name="momentum_v1")

    assert rm._strategy_deployed.get("momentum_v1") == pytest.approx(500 * 40.0)


# ── 3. Manual cancel_order() never released deployed capital ────────────────

class _FakeCancelBroker:
    def __init__(self):
        self.cancelled = []

    async def place_order(self, *a, **kw):
        return "bo-1"

    async def get_orders(self):
        return []  # still pending -- capital reserved at quote

    async def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)
        return True


@pytest.mark.asyncio
async def test_cancel_order_releases_deployed_capital():
    rm = RiskManager(initial_capital=300_000.0)
    repo = _FakeCapitalRepo()
    broker = _FakeCancelBroker()
    om = OrderManager(broker, rm, repo, repo)

    db_order = await om.place_order("SBIN26SEP800CE", "BUY", 500, 40.0, strategy_name="momentum_v1")
    assert rm._strategy_deployed.get("momentum_v1") == pytest.approx(500 * 40.0)

    ok = await om.cancel_order(db_order.id)

    assert ok is True
    assert broker.cancelled == ["bo-1"]
    assert rm._strategy_deployed.get("momentum_v1", 0) == pytest.approx(0.0), (
        "cancelling a resting order must release its reserved capital, "
        "same as expire_stale_orders()/sync_orders() already do on the "
        "identical OPEN -> CANCELLED transition"
    )


@pytest.mark.asyncio
async def test_cancel_order_on_a_spread_leg_does_not_touch_capital_tracking():
    # is_spread_leg orders were never counted by add_deployed_capital() in
    # the first place (the engine's own explicit max-loss-based call owns
    # that) -- cancelling one must not spuriously release anything.
    rm = RiskManager(initial_capital=300_000.0)
    repo = _FakeCapitalRepo()
    broker = _FakeCancelBroker()
    om = OrderManager(broker, rm, repo, repo)

    db_order = await om.place_order(
        "SBIN26SEP800PE", "BUY", 500, 5.0,
        strategy_name="credit_spread_v1", is_spread_leg=True,
    )
    await om.cancel_order(db_order.id)

    assert rm._strategy_deployed.get("credit_spread_v1", 0) == pytest.approx(0.0)


# ── 4. sync_orders() had no lock against its own concurrent invocation ──────

def test_order_manager_has_a_sync_orders_lock():
    rm = RiskManager(initial_capital=300_000.0)
    repo = _FakeCapitalRepo()
    om = OrderManager(SimpleNamespace(), rm, repo, repo)
    assert isinstance(om._sync_orders_lock, asyncio.Lock)


@pytest.mark.asyncio
async def test_concurrent_sync_orders_calls_are_serialized_not_interleaved():
    """Two overlapping sync_orders() calls (the 30s scheduled job and
    expire_stale_orders()'s internal 60s call) must never run their bodies
    concurrently -- otherwise both could observe the same OPEN->REJECTED
    transition and double-release capital for the same order."""
    entered = []

    class _SlowBroker:
        async def get_orders(self):
            entered.append("enter")
            await asyncio.sleep(0.05)
            entered.append("exit")
            return []

    class _NoOrdersRepo:
        async def filter(self, **kwargs):
            return [SimpleNamespace(id=1, broker_order_id="bo-1", order_status="OPEN")]

    rm = RiskManager(initial_capital=300_000.0)
    om = OrderManager(_SlowBroker(), rm, _NoOrdersRepo(), _NoOrdersRepo())

    await asyncio.gather(om.sync_orders(), om.sync_orders())

    # If serialized correctly: enter, exit, enter, exit (never enter, enter, ...).
    assert entered == ["enter", "exit", "enter", "exit"], (
        "the two sync_orders() calls interleaved instead of running one at a "
        "time -- the lock isn't actually serializing them"
    )


# ── 5. RS Ranker's EMA-stack component (25% of score) was dead code ─────────

def test_rs_history_window_covers_at_least_50_trading_days():
    # ema50_d needs len(s) >= 50; 32 calendar days (~22 trading days) never
    # reached that, so ema50_d always fell back to ema20_d, making
    # `ema20_d > ema50_d` compare a value to itself -- always False.
    assert _RS_HISTORY_DAYS >= 70, (
        "history window must comfortably clear 50 trading-day bars "
        "(accounting for NSE holidays) or the EMA-stack bonus is dead code again"
    )


def _synthetic_ranker(closes, nifty_closes=None):
    ranker = RSRanker(redis_client=None, symbols=["SBIN"])
    ranker._cache["SBIN"] = pd.DataFrame({"close": closes})
    ranker._nifty = pd.DataFrame({"close": nifty_closes or [100.0] * len(closes)})
    return ranker


def test_ema_stack_bonus_fires_on_a_genuine_60_bar_uptrend():
    # A real, sustained uptrend over 60 bars gives a real EMA20 > EMA50.
    closes = [90.0 + i * 1.0 for i in range(60)]  # steadily rising 90 -> 149
    ranker = _synthetic_ranker(closes)
    score = ranker._compute_rs("SBIN")
    assert score is not None
    # With a strong uptrend the EMA-stack bonus (25 pts) must be included --
    # score must exceed what's achievable from the 5d/20d return components
    # alone (40+35=75 max), proving the +25 bonus fired.
    assert score > 75.0


def test_ema_stack_bonus_does_not_fire_on_a_genuine_60_bar_downtrend():
    closes = [149.0 - i * 1.0 for i in range(60)]  # steadily falling
    ranker = _synthetic_ranker(closes)
    score = ranker._compute_rs("SBIN")
    assert score is not None
    # A genuine downtrend must NOT get the bullish EMA-stack bonus.
    assert score <= 75.0


# ── 6. _exit_all_options_for() silently dropped spread/condor tracking ──────

class _FakeExitAllTrackingOrder:
    def __init__(self, fill_price):
        self.fill_price = fill_price
        self.order_status = "OPEN"


class _FakeExitAllTrackingOrderManager:
    def __init__(self, fill_price):
        self.fill_price = fill_price

    async def place_order(self, contract, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None):
        return _FakeExitAllTrackingOrder(self.fill_price)


class _FakeExitAllTrackingEngine:
    _real_fill = staticmethod(LiveTradingEngine._real_fill)

    def __init__(self, active_spreads=None, active_condors=None):
        self.order_manager = _FakeExitAllTrackingOrderManager(fill_price=25.0)
        self.risk_manager = SimpleNamespace(release_deployed_capital=lambda *a, **kw: None)
        self._peak_premiums = {}
        self._peak_profits = {}
        self._single_leg_journals = {}
        self._active_spreads = active_spreads if active_spreads is not None else {}
        self._active_condors = active_condors if active_condors is not None else {}
        self._exited_today = set()
        self._kite = None
        self._redis = None

    async def _safe_get_positions(self):
        return []  # no single-leg positions on this underlying this time

    async def _get_market_data(self, symbol):
        return {"atr14": 3.0}

    async def _persist_state(self):
        pass


@pytest.mark.asyncio
async def test_exit_all_options_for_no_longer_silently_drops_active_spread_tracking():
    spread_state = {"symbol": "CIPLA", "type": "BULL_PUT_SPREAD"}
    fake = _FakeExitAllTrackingEngine(active_spreads={"CIPLA": spread_state})

    await LiveTradingEngine._exit_all_options_for(fake, "CIPLA")

    assert fake._active_spreads.get("CIPLA") == spread_state, (
        "an active spread on this underlying must stay tracked -- this "
        "function only closes single-leg longs, it must not discard "
        "multi-leg state it never actually closed"
    )
    assert "CIPLA" in fake._exited_today


@pytest.mark.asyncio
async def test_exit_all_options_for_no_longer_silently_drops_active_condor_tracking():
    condor_state = {"symbol": "CIPLA", "put_wing": {}, "call_wing": {}}
    fake = _FakeExitAllTrackingEngine(active_condors={"CIPLA": condor_state})

    await LiveTradingEngine._exit_all_options_for(fake, "CIPLA")

    assert fake._active_condors.get("CIPLA") == condor_state


@pytest.mark.asyncio
async def test_exit_all_options_for_still_clears_when_nothing_active():
    # Guard against over-fixing: an underlying with no active spread/condor
    # must behave exactly as before -- both dicts stay empty, no crash.
    fake = _FakeExitAllTrackingEngine()

    await LiveTradingEngine._exit_all_options_for(fake, "CIPLA")

    assert fake._active_spreads == {}
    assert fake._active_condors == {}
    assert "CIPLA" in fake._exited_today
