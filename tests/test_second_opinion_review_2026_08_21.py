"""
A second-opinion review (2026-08-21, same day as the KAYNES stale-data
incident fix) flagged 5 residual issues in code already touched earlier the
same day. All 5 verified against the actual code before fixing -- every one
was real:

1. momentum_v1: the bar_key=None fix (2026-08-20) stopped REPEATED None
   cycles from advancing the confirmation count, but the transition from an
   unidentified bar to the FIRST real bar_key was still silently counted as
   advancing -- a single real candle could masquerade as two confirming
   bars right at the moment a fresh candidate emerges.
2. ema_crossover_v1: identical pattern, same fix.
3. Order timeout reconciliation: _reconcile_after_timeout() swallowed its
   OWN verification failures (get_orders() itself erroring) and returned
   None -- indistinguishable from "checked, genuinely not there." Both
   outcomes fell through to the same FAILED status, silently concluding
   failure for an order whose real fate might be a genuinely live,
   completely untracked broker position.
4. Single-leg live entries could still place a real LIMIT order at a crude
   ATR-derived estimate when no real option quote was available.
5. credit_spread_v1/iron_condor_v1: _resolve_contract() can itself snap a
   strike to a different real, listed strike than what was delta-targeted
   (or delta-verified via the crowded-OI fix earlier the same day) -- the
   FINAL, actually-resolved strike's delta was never re-checked.
"""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.strategies.momentum import MomentumStrategy
from src.strategies.ema_crossover import EMACrossoverStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine
from src.orders.order_manager import OrderManager
from src.risk.risk_manager import RiskManager
from src.core.utils import broker_order_tag


# ── 1 & 2. bar_key=None -> first-real-bar_key transition ────────────────────

def _mom(**overrides):
    strat = MomentumStrategy("momentum_v1", overrides)
    strat.initialize()
    return strat


def _ema(**overrides):
    strat = EMACrossoverStrategy("ema_crossover_v1", overrides)
    strat.initialize()
    return strat


def test_momentum_legacy_first_real_bar_key_after_none_does_not_advance():
    strat = _mom(signal_confirm_bars=2, adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0, use_pullback_continuation_model=False)
    bar = dict(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx14=40.0)

    assert strat.generate_signal({**bar, "ohlc_bar_key": None}) == "HOLD"
    assert strat._pending_count["RELIANCE"] == 1

    # First real bar_key -- must NOT advance to 2 (might be the same candle).
    assert strat.generate_signal({**bar, "ohlc_bar_key": "live:t0"}) == "HOLD"
    assert strat._pending_count["RELIANCE"] == 1
    assert strat._pending_bar_key["RELIANCE"] == "live:t0"

    # A second, genuinely different, known bar_key correctly fires.
    assert strat.generate_signal({**bar, "ohlc_bar_key": "live:t1"}) == "BUY"


def test_momentum_legacy_signal_confirm_bars_1_still_fires_immediately_with_bar_key_none():
    """Backtest-engine compatibility guard -- signal_confirm_bars=1 must
    still fire on the very cycle a candidate first appears, regardless of
    bar_key, since there's no "second bar" ambiguity when only one bar of
    confirmation is required at all."""
    strat = _mom(signal_confirm_bars=1, adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0, use_pullback_continuation_model=False)
    bar = dict(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx14=40.0, ohlc_bar_key=None)
    assert strat.generate_signal(bar) == "BUY"


def test_momentum_pullback_model_first_real_bar_key_after_none_does_not_fast_track():
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    # Seed ESTABLISHED with bar_key=None.
    strat.generate_signal(dict(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx14=30.0,
                                close=110.0, atr14=2.0, rvol=1.5, ohlc_bar_key=None))
    assert "RELIANCE" not in strat._trend_state  # not seeded yet -- bar_key unknown

    # First real bar_key now seeds ESTABLISHED (this IS the seed cycle for
    # the pullback model -- unlike the legacy debounce, seeding here can't
    # itself fire a signal, so no extra "record but don't advance" state is
    # needed; is_new_bar for the NEXT real bar_key is what matters).
    strat.generate_signal(dict(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx14=30.0,
                                close=110.0, atr14=2.0, rvol=1.5, ohlc_bar_key="live:t0"))
    assert strat._trend_state.get("RELIANCE") == "ESTABLISHED"


def test_ema_crossover_first_real_bar_key_after_none_does_not_advance():
    strat = _ema(signal_confirm_bars=2)
    # Bar 1: bearish baseline (definite prev_dir).
    strat.generate_signal({"symbol": "RELIANCE", "ema20": 99.0, "ema50": 100.0, "ohlc_bar_key": None})
    # Fresh cross, bar_key still None -- seeds count=1 unconditionally.
    signal = strat.generate_signal({"symbol": "RELIANCE", "ema20": 101.0, "ema50": 100.0, "ohlc_bar_key": None})
    assert signal == "HOLD"
    assert strat._pending_count.get("RELIANCE") == 1

    # First real bar_key -- must NOT advance to 2.
    signal = strat.generate_signal({"symbol": "RELIANCE", "ema20": 101.0, "ema50": 100.0, "ohlc_bar_key": "live:t0"})
    assert signal == "HOLD"
    assert strat._pending_count.get("RELIANCE") == 1
    assert strat._pending_bar_key.get("RELIANCE") == "live:t0"

    # A second, genuinely different bar_key correctly fires.
    signal = strat.generate_signal({"symbol": "RELIANCE", "ema20": 101.0, "ema50": 100.0, "ohlc_bar_key": "live:t1"})
    assert signal == "BUY"


def test_ema_crossover_signal_confirm_bars_1_still_fires_immediately_with_bar_key_none():
    """Same backtest-compatibility guard as momentum's -- this is the exact
    scenario test_backtest.py::test_backtest_engine_ema_crossover exercises
    (ohlc_bar_key is never set in backtest data)."""
    strat = _ema(signal_confirm_bars=1)
    strat.generate_signal({"symbol": "RELIANCE", "ema20": 99.0, "ema50": 100.0, "ohlc_bar_key": None})
    signal = strat.generate_signal({"symbol": "RELIANCE", "ema20": 101.0, "ema50": 100.0, "ohlc_bar_key": None})
    assert signal == "BUY"


def test_ema_crossover_direction_flip_does_not_leak_stale_count():
    """A direction flip must reset the count to 1 (via the unconditional
    reseed), not let a stale count from the OLD direction survive under the
    new one."""
    strat = _ema(signal_confirm_bars=2)
    strat.generate_signal({"symbol": "RELIANCE", "ema20": 99.0, "ema50": 100.0, "ohlc_bar_key": "live:t0"})
    strat.generate_signal({"symbol": "RELIANCE", "ema20": 101.0, "ema50": 100.0, "ohlc_bar_key": "live:t1"})  # BUY pending, count=1
    assert strat._pending_signal.get("RELIANCE") == "BUY"
    # Flip back to bearish -- genuine fresh cross the other way.
    strat.generate_signal({"symbol": "RELIANCE", "ema20": 99.0, "ema50": 100.0, "ohlc_bar_key": "live:t2"})
    assert strat._pending_signal.get("RELIANCE") == "SELL"
    assert strat._pending_count.get("RELIANCE") == 1


# ── 3. Order timeout: "couldn't verify" must not become FAILED ──────────────

def _row(id_=1):
    return SimpleNamespace(id=id_, symbol="SBIN26AUG800CE", order_status="PENDING", broker_order_id=None, fill_price=None)


class _Repo:
    def __init__(self, rows):
        self.rows = {r.id: r for r in rows}

    async def create(self, data):
        row = SimpleNamespace(**data, id=len(self.rows) + 1)
        self.rows[row.id] = row
        return row

    async def update(self, obj, updates):
        for k, v in updates.items():
            setattr(obj, k, v)
        return obj

    async def filter(self, **kw):
        wanted = kw.get("order_status")
        return [r for r in self.rows.values() if wanted is None or getattr(r, "order_status", None) == wanted]

    async def get_by_id(self, oid):
        return self.rows.get(oid)


@pytest.mark.asyncio
async def test_timeout_with_unverifiable_reconciliation_becomes_pending_verification_not_failed():
    class _Broker:
        async def place_order(self, *a, **kw):
            import asyncio as _asyncio
            raise _asyncio.TimeoutError()

        async def get_orders(self):
            raise ConnectionError("network is unreachable")

    row = _row()
    repo = _Repo([])
    om = OrderManager(_Broker(), RiskManager(initial_capital=300_000.0), repo, repo)
    result = await om.place_order("SBIN26AUG800CE", "BUY", 25, 100.0, strategy_name="ema_crossover_v1")

    assert result.order_status == "PENDING_VERIFICATION"


@pytest.mark.asyncio
async def test_timeout_with_verified_not_found_is_still_failed():
    """Guard against over-fixing -- a genuine, verified 'not there' answer
    must still conclude FAILED."""
    class _Broker:
        async def place_order(self, *a, **kw):
            import asyncio as _asyncio
            raise _asyncio.TimeoutError()

        async def get_orders(self):
            return []  # broker reachable, genuinely no matching order

    repo = _Repo([])
    om = OrderManager(_Broker(), RiskManager(initial_capital=300_000.0), repo, repo)
    result = await om.place_order("SBIN26AUG800CE", "BUY", 25, 100.0, strategy_name="ema_crossover_v1")

    assert result.order_status == "FAILED"


@pytest.mark.asyncio
async def test_pending_verification_retry_finds_order_live_corrects_to_open():
    row = SimpleNamespace(id=42, symbol="SBIN26AUG800CE", order_status="PENDING_VERIFICATION", broker_order_id=None)
    repo = _Repo([row])
    tag = broker_order_tag("42")

    class _Broker:
        async def get_orders(self):
            return [{"order_id": "broker-live-1", "tag": tag, "status": "OPEN"}]

    om = OrderManager(_Broker(), RiskManager(initial_capital=300_000.0), repo, repo)
    await om._retry_pending_verification_orders()

    assert row.order_status == "OPEN"
    assert row.broker_order_id == "broker-live-1"


@pytest.mark.asyncio
async def test_pending_verification_retry_now_verified_not_found_marks_failed():
    row = SimpleNamespace(id=43, symbol="SBIN26AUG800CE", order_status="PENDING_VERIFICATION", broker_order_id=None)
    repo = _Repo([row])

    class _Broker:
        async def get_orders(self):
            return []  # now reachable, genuinely nothing there

    om = OrderManager(_Broker(), RiskManager(initial_capital=300_000.0), repo, repo)
    await om._retry_pending_verification_orders()

    assert row.order_status == "FAILED"


@pytest.mark.asyncio
async def test_pending_verification_retry_still_unverifiable_stays_pending():
    row = SimpleNamespace(id=44, symbol="SBIN26AUG800CE", order_status="PENDING_VERIFICATION", broker_order_id=None)
    repo = _Repo([row])

    class _Broker:
        async def get_orders(self):
            raise ConnectionError("still unreachable")

    om = OrderManager(_Broker(), RiskManager(initial_capital=300_000.0), repo, repo)
    await om._retry_pending_verification_orders()

    assert row.order_status == "PENDING_VERIFICATION"  # never guesses FAILED


def test_reconcile_after_timeout_raises_instead_of_swallowing():
    src = inspect.getsource(OrderManager._reconcile_after_timeout)
    assert "except Exception as e:" not in src
    assert "return None" in src  # still the genuine "verified not found" path


def test_expire_stale_orders_retries_pending_verification_first():
    src = inspect.getsource(OrderManager.expire_stale_orders)
    assert "_retry_pending_verification_orders" in src


# ── 4. Single-leg entry: no LIMIT order on an estimated price ───────────────

def test_entry_skips_when_real_quote_unavailable():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    block = src[src.index("get_option_quote(contract"):src.index("order = await self.order_manager.place_order")]
    assert "estimate_option_premium" not in block
    assert "if not (_real_p and _real_p > 0):" in block


# ── 5. Resolved strike delta re-verification ─────────────────────────────────

def test_resolved_strike_delta_ok_accepts_a_strike_within_tolerance():
    from src.market_data.option_chain import find_delta_strike
    strike = find_delta_strike(20000, -0.20, "PE", 21, 0.20, 50)
    assert LiveTradingEngine._resolved_strike_delta_ok(strike, "PE", -0.20, 20000, 21, 0.20)


def test_resolved_strike_delta_ok_rejects_a_strike_that_drifted_too_far():
    # A strike 2000 points OTM from a ~19300 20-delta strike has a wildly
    # different real delta (near 0) -- well outside any reasonable tolerance.
    assert not LiveTradingEngine._resolved_strike_delta_ok(17300, "PE", -0.20, 20000, 21, 0.20)


def test_credit_spread_reverifies_resolved_short_strike_delta():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("_entry_prices = await get_entry_prices_for_spread")
    block = src[:idx]
    assert "_resolved_strike_delta_ok" in block


def test_iron_condor_reverifies_both_resolved_short_strike_deltas():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    start = src.index('put_short_strike,  psc = _resolved_legs["put_short"]')
    end = src.index("_put_prices = await get_entry_prices_for_spread")
    block = src[start:end]
    assert block.count("self._resolved_strike_delta_ok(") == 2
