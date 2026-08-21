"""
Incident (2026-08-21, 12:55-13:01 IST): KAYNES26SEP3900CE (ema_crossover_v1)
exited at -67.2% instead of the intended -50% hard stop. Root cause: a
~6-minute Zerodha connectivity blip made the underlying's cached market data
stale, and _check_open_option_exits() used to `continue` on stale/missing
market_data -- skipping the ENTIRE exit check for every open position on
that underlying, including the plain premium-based hard stop, which only
needs the option's own quote (fetched via get_option_quote(), a separate
data path from the underlying's cached tick pipeline, that was never even
being reached because the `continue` fired first).

Fix: stale/missing market_data now degrades to an empty dict instead of
aborting the check -- every downstream `market_data.get(...)` already
treats a missing field as "skip that specific conditional exit" (the
existing convention for every EMA/ADX/structural-invalidation check in
manage_position()), so premium-based exits and the DTE/overnight-position
forced-close checks keep running regardless. Paired with a one-time
visibility alert if staleness persists past a threshold while a position
is open.
"""
import inspect
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.utils import now_ist


# ── Static-source guards ─────────────────────────────────────────────────────

def test_check_open_option_exits_no_longer_aborts_the_whole_check_on_stale_data():
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    assert "if not market_data:\n                    continue" not in src
    assert "_raw_market_data = await self._get_market_data(underlying)" in src
    assert "market_data = _raw_market_data or {}" in src


def test_check_open_option_exits_has_the_staleness_alert():
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    assert "_stale_data_since" in src
    assert "_stale_data_alerted" in src
    assert "_STALE_DATA_ALERT_SECONDS" in src
    assert "DATA STALENESS ALERT" in src


# ── Behavioral: premium hard stop fires despite stale underlying data ───────

class _FakeExitCheckEngine:
    _check_open_option_exits    = LiveTradingEngine._check_open_option_exits
    _get_underlying_from_contract = LiveTradingEngine._get_underlying_from_contract

    def __init__(self, market_data_response, live_quote):
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {
            "KAYNES26SEP3900CE": {
                "date": now_ist().date().isoformat(),
                "strategy_name": "ema_crossover_v1",
                "entry_regime": None,
            }
        }
        self._peak_premiums = {}
        self._stale_data_since = {}
        self._stale_data_alerted = set()
        self._STALE_DATA_ALERT_SECONDS = 120
        self._get_market_data = AsyncMock(return_value=market_data_response)
        self._kite = None
        self._redis = None
        self._execute_single_leg_exit = AsyncMock()
        self._notify = AsyncMock()
        self._live_quote = live_quote


@pytest.fixture
def ema_strategy():
    from src.strategies.ema_crossover import EMACrossoverStrategy
    strat = EMACrossoverStrategy("ema_crossover_v1", {})
    strat.initialize()
    return strat


def _kayns_position():
    return [{"symbol": "KAYNES26SEP3900CE", "quantity": 150, "avg_price": 210.12}]


@pytest.mark.asyncio
async def test_premium_hard_stop_still_fires_when_underlying_market_data_is_stale(
    monkeypatch, ema_strategy
):
    """The exact incident scenario: market_data is None (stale/unavailable),
    but the option's own quote IS available and shows a >50% loss -- the
    hard stop must still fire, not be silently skipped."""
    import src.market_data.option_chain as option_chain_module
    monkeypatch.setattr(
        option_chain_module, "get_option_quote",
        AsyncMock(return_value=95.0),  # 210.12 -> 95.0 = -54.8%, past the 50% stop
    )

    fake = _FakeExitCheckEngine(market_data_response=None, live_quote=95.0)
    await fake._check_open_option_exits(_kayns_position(), {"ema_crossover_v1": ema_strategy})

    fake._get_market_data.assert_awaited_once_with("KAYNES")
    fake._execute_single_leg_exit.assert_awaited_once()
    call = fake._execute_single_leg_exit.call_args
    assert call.args[0] == "KAYNES26SEP3900CE"
    assert "Stop loss" in "".join(str(a) for a in call.args) or call.args[3]  # exit_reason present
    assert call.args[3]  # exit_reason is truthy/non-empty


@pytest.mark.asyncio
async def test_indicator_based_exit_stays_paused_when_market_data_is_stale(monkeypatch, ema_strategy):
    """A position that's only in a small premium loss (below the hard stop)
    and would ONLY exit via the EMA-reversal check must NOT exit while
    market_data is stale -- current_ema_fast/current_ema_slow come back as
    None, and manage_position() already skips that check gracefully on
    None, exactly as it does when the fields are simply absent."""
    import src.market_data.option_chain as option_chain_module
    monkeypatch.setattr(
        option_chain_module, "get_option_quote",
        AsyncMock(return_value=200.0),  # 210.12 -> 200.0 = -4.7%, nowhere near any premium stop
    )

    fake = _FakeExitCheckEngine(market_data_response=None, live_quote=200.0)
    await fake._check_open_option_exits(_kayns_position(), {"ema_crossover_v1": ema_strategy})

    fake._execute_single_leg_exit.assert_not_awaited()


@pytest.mark.asyncio
async def test_intact_market_data_behaves_exactly_as_before(monkeypatch, ema_strategy):
    """Guard against over-fixing -- when market_data IS fresh, behavior must
    be unchanged (no double-counting, no new spurious exits)."""
    import src.market_data.option_chain as option_chain_module
    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=205.0))

    # ema20 > ema50 (bullish, matches the CE position) so the (now-general)
    # EMA-reversal exit doesn't spuriously fire -- this test is about the
    # stale-data bookkeeping, not exercising that specific exit rule.
    fresh_data = {"atr14": 5.0, "adx14": 20.0, "ema20": 101.0, "ema50": 100.0, "close": 3800.0}
    fake = _FakeExitCheckEngine(market_data_response=fresh_data, live_quote=205.0)
    await fake._check_open_option_exits(_kayns_position(), {"ema_crossover_v1": ema_strategy})

    fake._execute_single_leg_exit.assert_not_awaited()
    assert "KAYNES" not in fake._stale_data_since
    assert "KAYNES" not in fake._stale_data_alerted


# ── Behavioral: staleness alert fires once after the threshold, not every cycle ──

@pytest.mark.asyncio
async def test_staleness_alert_fires_once_after_threshold_not_every_cycle(monkeypatch, ema_strategy):
    import src.market_data.option_chain as option_chain_module
    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=200.0))

    fake = _FakeExitCheckEngine(market_data_response=None, live_quote=200.0)
    fake._STALE_DATA_ALERT_SECONDS = 0  # fire immediately for this test, don't wait 120s
    # Pre-seed "since" far enough in the past that the threshold is already crossed.
    fake._stale_data_since["KAYNES"] = now_ist().replace(tzinfo=None) - timedelta(seconds=999)

    await fake._check_open_option_exits(_kayns_position(), {"ema_crossover_v1": ema_strategy})
    fake._notify.assert_awaited_once()
    assert "KAYNES" in fake._stale_data_alerted

    # A second cycle, still stale -- must NOT alert again (already alerted this episode).
    fake._notify.reset_mock()
    await fake._check_open_option_exits(_kayns_position(), {"ema_crossover_v1": ema_strategy})
    fake._notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_staleness_state_clears_once_fresh_data_returns(monkeypatch, ema_strategy):
    import src.market_data.option_chain as option_chain_module
    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=200.0))

    fake = _FakeExitCheckEngine(market_data_response=None, live_quote=200.0)
    fake._stale_data_since["KAYNES"] = now_ist().replace(tzinfo=None) - timedelta(seconds=999)
    fake._stale_data_alerted.add("KAYNES")

    # Fresh data resumes.
    fake._get_market_data = AsyncMock(return_value={"atr14": 5.0})
    await fake._check_open_option_exits(_kayns_position(), {"ema_crossover_v1": ema_strategy})

    assert "KAYNES" not in fake._stale_data_since
    assert "KAYNES" not in fake._stale_data_alerted
