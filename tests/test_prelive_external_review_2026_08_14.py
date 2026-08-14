"""
External code review (2026-08-14), verified against actual code before
fixing -- 5 confirmed gaps, all pre-live P0/P1 per docs/LIVE_TRADING_CHECKLIST.md:

1. _safe_get_positions() conflated "broker call failed" with "broker says
   zero positions" -- both returned []. Now sets a separate
   _broker_position_state_known flag; run_signal_cycle() blocks all new
   entries for the cycle when it's False, without changing exit behavior.
2. _check_available_margin() failed OPEN on a live kite.margins() error
   ("Allowing trade"). Both call sites are entry paths (never called on an
   exit), so the original "never blocks a legitimate exit" justification
   didn't apply -- now fails closed (blocks the entry).
3. Found while verifying #2: the paper-mode branch read
   getattr(settings, "initial_capital", 300_000) -- lowercase, which never
   matched the real case-sensitive "INITIAL_CAPITAL" Settings attribute, so
   it silently used the hardcoded 300_000 fallback regardless of the
   actually configured value.
4. _get_market_data() let a malformed/unparseable timestamp through
   unchanged (except: pass) instead of treating it as invalid data; a
   missing timestamp skipped the staleness check entirely; a future
   timestamp was never rejected. All four cases (missing/malformed/future/
   stale) now return None.
5. Condor regime-shift exit check silently no-op'd on any Redis/JSON error
   (already flagged in the checklist's A4 as a confirmed fail-open gap) --
   now logs a warning; other exit checks for the same position are
   unaffected either way.
"""
import json
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.enums import TradingMode


# ── 1. Broker position state: unknown vs. confirmed-zero ────────────────────

@pytest.mark.asyncio
async def test_safe_get_positions_marks_state_unknown_on_broker_failure():
    class _FailingBroker:
        async def get_positions(self):
            raise RuntimeError("Zerodha API timeout")

    stub = SimpleNamespace(broker=_FailingBroker(), _broker_position_state_known=True)

    result = await LiveTradingEngine._safe_get_positions(stub)

    assert result == []
    assert stub._broker_position_state_known is False


@pytest.mark.asyncio
async def test_safe_get_positions_marks_state_known_on_confirmed_zero():
    class _OkBroker:
        async def get_positions(self):
            return []

    # Simulate a prior failure to prove success actually flips it back.
    stub = SimpleNamespace(broker=_OkBroker(), _broker_position_state_known=False)

    result = await LiveTradingEngine._safe_get_positions(stub)

    assert result == []
    assert stub._broker_position_state_known is True


def test_run_signal_cycle_blocks_new_entries_when_broker_state_unknown():
    # run_signal_cycle() has too many live dependencies (strategies, redis,
    # scheduler timing) to drive behaviorally in a unit test -- static-source
    # regression guard, matching this project's convention (see
    # test_data_outage_fail_closed.py / test_zerodha_sync.py).
    src = inspect.getsource(LiveTradingEngine.run_signal_cycle)
    assert "if not self._broker_position_state_known:" in src
    warmup_idx    = src.index("Market open warm-up")
    guard_idx     = src.index("if not self._broker_position_state_known:")
    entry_loop_idx = src.index("for strategy_id, strategy in active_strategies.items():")
    assert warmup_idx < guard_idx < entry_loop_idx, (
        "the broker-state guard must sit between warm-up and the entry loop "
        "so it blocks entries without touching the exit checks earlier in the cycle"
    )


# ── 2 & 3. Margin check: fail closed on API error, real INITIAL_CAPITAL ─────

@pytest.mark.asyncio
async def test_check_available_margin_fails_closed_on_live_api_error():
    class _BrokenKite:
        def margins(self):
            raise RuntimeError("Zerodha margins() timeout")

    stub = SimpleNamespace(mode=TradingMode.LIVE, _kite=_BrokenKite())

    result = await LiveTradingEngine._check_available_margin(stub, 10_000.0)

    assert result is False


@pytest.mark.asyncio
async def test_check_available_margin_still_blocks_on_genuinely_insufficient_live_margin():
    class _OkKite:
        def margins(self):
            return {"equity": {"net": 5_000.0}}

    stub = SimpleNamespace(mode=TradingMode.LIVE, _kite=_OkKite())

    assert await LiveTradingEngine._check_available_margin(stub, 10_000.0) is False
    assert await LiveTradingEngine._check_available_margin(stub, 1_000.0) is True


@pytest.mark.asyncio
async def test_check_available_margin_paper_mode_reads_real_initial_capital(monkeypatch):
    import src.live_trading.live_trading_engine as live_engine_module
    monkeypatch.setattr(live_engine_module.settings, "INITIAL_CAPITAL", 50_000.0)

    stub = SimpleNamespace(mode=TradingMode.PAPER, _kite=None, _active_spreads={}, _active_condors={})

    # capital=50,000 (patched), required=60,000 -- only rejected if the
    # function actually reads the patched value instead of the old hardcoded
    # 300_000 fallback (which would have incorrectly approved this).
    result = await LiveTradingEngine._check_available_margin(stub, 60_000.0)
    assert result is False


# ── 4. Market-data timestamp validation ──────────────────────────────────────

class _FakeTickRedis:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload)

    async def get(self, key):
        return self._raw


def _stub_engine():
    return SimpleNamespace(_MARKET_DATA_MAX_AGE_SECONDS=LiveTradingEngine._MARKET_DATA_MAX_AGE_SECONDS)


@pytest.mark.asyncio
async def test_get_market_data_accepts_fresh_valid_timestamp():
    stub = _stub_engine()
    stub._redis = _FakeTickRedis({"close": 100.0, "timestamp": datetime.now().isoformat()})
    result = await LiveTradingEngine._get_market_data(stub, "RELIANCE")
    assert result is not None
    assert result["close"] == 100.0


@pytest.mark.asyncio
async def test_get_market_data_rejects_missing_timestamp():
    stub = _stub_engine()
    stub._redis = _FakeTickRedis({"close": 100.0})  # no "timestamp" key at all
    assert await LiveTradingEngine._get_market_data(stub, "RELIANCE") is None


@pytest.mark.asyncio
async def test_get_market_data_rejects_unparseable_timestamp():
    stub = _stub_engine()
    stub._redis = _FakeTickRedis({"close": 100.0, "timestamp": "not-a-timestamp"})
    assert await LiveTradingEngine._get_market_data(stub, "RELIANCE") is None


@pytest.mark.asyncio
async def test_get_market_data_rejects_future_timestamp():
    stub = _stub_engine()
    future_ts = (datetime.now() + timedelta(seconds=60)).isoformat()
    stub._redis = _FakeTickRedis({"close": 100.0, "timestamp": future_ts})
    assert await LiveTradingEngine._get_market_data(stub, "RELIANCE") is None


@pytest.mark.asyncio
async def test_get_market_data_tolerates_small_clock_drift():
    stub = _stub_engine()
    near_future_ts = (datetime.now() + timedelta(seconds=2)).isoformat()
    stub._redis = _FakeTickRedis({"close": 100.0, "timestamp": near_future_ts})
    assert await LiveTradingEngine._get_market_data(stub, "RELIANCE") is not None


@pytest.mark.asyncio
async def test_get_market_data_rejects_stale_timestamp():
    stub = _stub_engine()
    stale_ts = (datetime.now() - timedelta(seconds=200)).isoformat()
    stub._redis = _FakeTickRedis({"close": 100.0, "timestamp": stale_ts})
    assert await LiveTradingEngine._get_market_data(stub, "RELIANCE") is None


# ── 5. Condor regime-shift Redis/JSON failure now logs ───────────────────────

def test_condor_regime_shift_exit_check_logs_on_redis_json_failure():
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    idx = src.index('_regime_raw = await _r.get("market:regime")')
    window = src[idx:idx + 1600]
    assert "except Exception as exc:" in window
    assert "logger.warning" in window
    assert "Regime-shift exit check failed" in window
