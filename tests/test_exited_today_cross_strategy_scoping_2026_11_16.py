"""
_exited_today cross-strategy interference (2026-11-16, live incident).

_exited_today used to be a plain set of bare symbol strings, shared across
ALL 4 strategies, serving two purposes: (1) "this strategy adverse-exited
here today, block its own same-day re-entry" and (2) "the risk manager
rejected this strategy's candidate here, stop retrying every cycle." Because
it was keyed by symbol alone, one strategy's rejection/exit silently blocked
a completely different strategy's unrelated signal on the same underlying.

Confirmed live: iron_condor_v1's own sector-concentration REJECTED_BY_RISK
rejection on RELIANCE (09:39:30) added "RELIANCE" to the set to stop ITSELF
retrying -- which then blocked ema_crossover_v1's own, genuinely confirmed
BUY signal on RELIANCE at 11:25:27 ("already exited today, no re-entry"),
even though ema_crossover_v1 never exited anything on RELIANCE. Same for
GAIL. Fixed by keying the set with (strategy_name, symbol) tuples so a
rejection/adverse-exit only blocks re-entry for the SAME strategy that
triggered it.
"""
import datetime as _dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.enums import SignalType


@pytest.fixture(autouse=True)
def _pin_clock_outside_the_hour13_exclusion(monkeypatch):
    import src.live_trading.live_trading_engine as lte_module
    monkeypatch.setattr(lte_module, "now_ist", lambda: _dt.datetime(2026, 10, 15, 10, 0, 0))


class _FakeEngine:
    _audit_gate = LiveTradingEngine._audit_gate
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure
    _ELEVATED_VIX_ENTRY_CEILING = LiveTradingEngine._ELEVATED_VIX_ENTRY_CEILING

    def __init__(self, market_data, exited_today=None):
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {}
        self._exited_today = exited_today if exited_today is not None else set()
        self._max_daily_orders = 0
        self._max_concurrent_intraday = 999
        self._last_signal_date = {}
        self._signal_gate_stats = {}
        self._last_gate_rejection = None
        self._close_option_positions = AsyncMock()
        self._has_open_option = AsyncMock(return_value=False)
        self._get_market_data = AsyncMock(return_value=market_data)
        self._redis = None
        self.rs_ranker = None


@pytest.mark.asyncio
async def test_one_strategys_rejection_no_longer_blocks_a_different_strategys_entry():
    # Reproduces the live RELIANCE incident: iron_condor_v1 rejected itself
    # here (sector cap), ema_crossover_v1 never exited anything here.
    fake = _FakeEngine(
        {
            "close": 1200.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        },
        exited_today={("iron_condor_v1", "RELIANCE")},
    )
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        generate_signal=lambda market_data: SignalType.BUY,
    )
    fake._get_lot_size = AsyncMock(return_value=None)  # stop gracefully right after the re-entry check

    await LiveTradingEngine._process_signal(fake, strategy, "RELIANCE", vix=15.0, regime="TRENDING")

    # Got past the same-day re-entry block and reached the next real step.
    fake._close_option_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_strategys_own_prior_rejection_still_blocks_its_own_re_entry():
    # Guard against over-fixing: the ORIGINAL protection must still work when
    # it's the SAME strategy that was blocked earlier today.
    fake = _FakeEngine(
        {
            "close": 1200.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        },
        exited_today={("ema_crossover_v1", "RELIANCE")},
    )
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True, min_dte=0, max_dte=999,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "RELIANCE", vix=15.0, regime="TRENDING")

    fake._close_option_positions.assert_not_awaited()
