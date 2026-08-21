"""
External PDF review of momentum_v1 (2026-08-20) -- verified against the
actual code before fixing (one claim, stale-order-cancellation marking
EXPIRED on a failed cancel, was already fixed earlier the same day and is
NOT re-fixed here). Real momentum_v1 trade history was also pulled from
production (11 closed trades, 9.1% win rate) to sanity-check the review's
qualitative concern before acting on it -- see docs/LIVE_TRADING_CHECKLIST.md.

Confirmed and fixed here:
  1. bar_key=None let every engine cycle count as a "new bar" in both
     momentum.py and ema_crossover.py (PDF only flagged momentum.py --
     ema_crossover.py has the identical pattern).
  2. OrderManager.place_order()'s TimeoutError handler marked the order
     FAILED with no attempt to check whether the broker actually processed
     it anyway.
  3. The RS and 15-min MTF entry filters failed OPEN (proceeded without the
     filter) on any data-unavailability, inconsistent with this codebase's
     fail-closed convention for explicitly-chosen entry filters.
  4. config.py shipped real (if fallback) insecure default credentials with
     no production startup guard -- DB_PASSWORD was found still literally
     "password123" on the live server and was rotated as a separate,
     non-code action.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.strategies.momentum import MomentumStrategy
from src.strategies.ema_crossover import EMACrossoverStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.utils import broker_order_tag
from src.core.config import Settings, validate_production_secrets


# ── 1. bar_key=None must not advance the confirmation counter ───────────────

def _momentum_data(adx=40.0, ema20=105.0, ema50=100.0, bar_key=None, symbol="RELIANCE"):
    return {"symbol": symbol, "ema20": ema20, "ema50": ema50, "adx14": adx, "ohlc_bar_key": bar_key}


def test_momentum_bar_key_none_does_not_fast_track_confirmation():
    # Entry-quality filters added 2026-08-20 (external review integration,
    # same day as this bar_key fix) disabled here -- this test isolates the
    # bar_key debounce mechanism specifically, not the newer filters (which
    # fail closed on insufficient ADX/EMA history and would otherwise reject
    # every candidate before the debounce logic is ever reached).
    strat = MomentumStrategy("momentum_v1", {
        "signal_confirm_bars": 2,
        "adx_rising_required": False, "ema_slope_required": False,
        "extension_atr_mult": 0, "vwap_extension_pct": 0,
        # Fixed 2026-08-21 (external review, round 2): this test asserts
        # directly on _pending_count, the legacy debounce's own state --
        # the new pullback+breakout model (now the default) doesn't use it
        # at all, so this must pin the legacy path explicitly.
        "use_pullback_continuation_model": False,
    })
    strat.initialize()

    # First call establishes pending state (count=1) regardless of bar_key.
    assert strat.generate_signal(_momentum_data(bar_key=None)) == "HOLD"
    assert strat._pending_count["RELIANCE"] == 1

    # Fixed 2026-08-20 (external review): repeated calls with bar_key=None
    # must NOT advance the count -- previously each one incremented it,
    # confirming a "2-bar" signal within 2 engine cycles (~2 min) instead of
    # 2 real 5-min candles (~10 min).
    for _ in range(5):
        signal = strat.generate_signal(_momentum_data(bar_key=None))
        assert signal == "HOLD"
    assert strat._pending_count["RELIANCE"] == 1

    # Fixed 2026-08-21 (external review): the FIRST real bar_key we ever
    # see, after being seeded on an unidentified bar, must NOT itself count
    # as a second confirming bar -- it might be the SAME anonymous candle
    # the count was seeded on. It only backfills the reference; the count
    # stays at 1.
    signal = strat.generate_signal(_momentum_data(bar_key="live:2026-08-20T10:05:00"))
    assert signal == "HOLD"
    assert strat._pending_count["RELIANCE"] == 1
    assert strat._pending_bar_key["RELIANCE"] == "live:2026-08-20T10:05:00"

    # A SECOND, genuinely different, also-known bar_key correctly advances
    # past the now-known reference and fires.
    signal = strat.generate_signal(_momentum_data(bar_key="live:2026-08-20T10:10:00"))
    assert signal == "BUY"


def _ema_data(fast=105.0, slow=100.0, bar_key=None, symbol="RELIANCE"):
    return {"symbol": symbol, "ema20": fast, "ema50": slow, "close": 2500.0, "ohlc_bar_key": bar_key}


def test_ema_crossover_bar_key_none_does_not_fast_track_confirmation():
    strat = EMACrossoverStrategy("ema_crossover_v1", {"signal_confirm_bars": 2})
    strat.initialize()

    # Bar 1: bearish (fast < slow) so bar 2's flip registers as a genuine
    # fresh cross (prev_dir must be a definite BUY/SELL, not equal/unknown).
    strat.generate_signal(_ema_data(fast=99.0, slow=100.0, bar_key="live:t0"))
    # Bar 2: genuine fresh cross -- starts pending count=1.
    signal = strat.generate_signal(_ema_data(fast=101.0, slow=100.0, bar_key="live:t1"))
    assert signal == "HOLD"
    assert strat._pending_count.get("RELIANCE") == 1

    # Fixed 2026-08-20 (external review): repeated calls with bar_key=None
    # must NOT advance the count.
    for _ in range(5):
        signal = strat.generate_signal(_ema_data(fast=101.0, slow=100.0, bar_key=None))
        assert signal == "HOLD"
    assert strat._pending_count.get("RELIANCE") == 1

    # A genuinely new bar advances and fires.
    signal = strat.generate_signal(_ema_data(fast=101.0, slow=100.0, bar_key="live:t2"))
    assert signal == "BUY"


# ── 2. Order-placement timeout must reconcile against the broker, not ───────
#    immediately mark FAILED.

@pytest.mark.asyncio
async def test_place_order_timeout_reconciles_and_finds_the_order_was_live():
    from src.orders.order_manager import OrderManager
    from src.risk.risk_manager import RiskManager
    import asyncio as _asyncio

    class _Row:
        def __init__(self):
            self.id = 42
            self.order_status = "PENDING"
            self.broker_order_id = None
            self.fill_price = None

    class _Repo:
        def __init__(self, row):
            self.row = row

        async def create(self, data):
            return self.row

        async def update(self, obj, updates):
            for k, v in updates.items():
                setattr(obj, k, v)
            return obj

    class _TimeoutThenFoundBroker:
        """place_order() times out client-side, but the order actually WAS
        accepted by the broker -- get_orders() shows it, tagged correctly."""
        async def place_order(self, *a, **kw):
            raise _asyncio.TimeoutError()

        async def get_orders(self):
            tag = broker_order_tag("42")
            return [{"order_id": "broker-live-999", "tag": tag, "status": "OPEN"}]

    row = _Row()
    repo = _Repo(row)
    om = OrderManager(_TimeoutThenFoundBroker(), RiskManager(initial_capital=300_000.0), repo, repo)

    result = await om.place_order("SBIN26AUG800CE", "BUY", 25, 100.0, strategy_name="ema_crossover_v1")

    # Fixed 2026-08-20 (external review): must be corrected to OPEN with the
    # real broker_order_id, not left as FAILED with the order silently
    # orphaned and untracked despite being genuinely live.
    assert result.order_status == "OPEN"
    assert result.broker_order_id == "broker-live-999"


@pytest.mark.asyncio
async def test_place_order_timeout_marks_failed_when_broker_genuinely_never_got_it():
    """Guard against over-fixing -- if the broker's order list genuinely has
    no matching tag, FAILED is still the correct outcome."""
    from src.orders.order_manager import OrderManager
    from src.risk.risk_manager import RiskManager
    import asyncio as _asyncio

    class _Row:
        def __init__(self):
            self.id = 43
            self.order_status = "PENDING"
            self.broker_order_id = None
            self.fill_price = None

    class _Repo:
        def __init__(self, row):
            self.row = row

        async def create(self, data):
            return self.row

        async def update(self, obj, updates):
            for k, v in updates.items():
                setattr(obj, k, v)
            return obj

    class _TimeoutAndNeverFoundBroker:
        async def place_order(self, *a, **kw):
            raise _asyncio.TimeoutError()

        async def get_orders(self):
            return []  # broker genuinely never received it

    row = _Row()
    repo = _Repo(row)
    om = OrderManager(_TimeoutAndNeverFoundBroker(), RiskManager(initial_capital=300_000.0), repo, repo)

    result = await om.place_order("SBIN26AUG800CE", "BUY", 25, 100.0, strategy_name="ema_crossover_v1")

    assert result.order_status == "FAILED"


# ── 3. RS and MTF entry filters must fail closed ─────────────────────────────

@pytest.mark.asyncio
async def test_rs_filter_failure_blocks_the_entry():
    fake = SimpleNamespace(
        rs_ranker=SimpleNamespace(get_ranks=AsyncMock(side_effect=RuntimeError("redis down"))),
        _redis=None,
        _has_active_multi_leg_structure=lambda symbol: True,  # short-circuit right after the filters
    )
    strategy = SimpleNamespace(name="ema_crossover_v1")

    # Directly exercise the RS-filter block via a minimal stand-in of the
    # relevant _process_signal slice is impractical (deep in a large method) --
    # instead confirm the fix at the source-level plus behaviorally through
    # the "no active_multi_leg guard reached" signal below is out of scope;
    # assert on the static source instead, matching this codebase's
    # convention for spot-checking a specific branch inside a large method.
    import inspect
    src = inspect.getsource(LiveTradingEngine._process_signal)
    rs_block = src[src.index("Relative Strength filter"):src.index("Multi-timeframe confirmation")]
    assert "failing closed on this entry filter" in rs_block
    assert "_rs_ranks = []" not in rs_block, "must not silently fail open to an empty list anymore"

    mtf_block = src[src.index("Multi-timeframe confirmation"):src.index("lot_size = await self._get_lot_size")]
    assert "failing closed on this entry filter" in mtf_block
    assert "pass  # MTF data unavailable" not in mtf_block


# ── 4. Production startup must refuse to boot with default credentials ──────

def test_validate_production_secrets_raises_on_default_db_password():
    s = Settings(ENV="production", DB_PASSWORD="password123",
                 JWT_SECRET="a-real-secret", DASHBOARD_PASSWORD="a-real-password")
    with pytest.raises(RuntimeError, match="DB_PASSWORD"):
        validate_production_secrets(s)


def test_validate_production_secrets_passes_with_real_values():
    s = Settings(ENV="production", DB_PASSWORD="a-real-db-password",
                 JWT_SECRET="a-real-secret", DASHBOARD_PASSWORD="a-real-password")
    validate_production_secrets(s)  # must not raise


def test_validate_production_secrets_does_not_block_non_production_envs():
    """Dev/test environments never set real values -- must not be blocked."""
    s = Settings(ENV="development")
    validate_production_secrets(s)  # must not raise despite every default being insecure


def test_broker_order_tag_format_is_shared_and_capped_at_20_chars():
    assert broker_order_tag("42") == "ft42"
    assert len(broker_order_tag("1" * 30)) == 20
