"""
_refresh_iv_history_for_universe() (2026-09-25, live incident: 5 straight
trading days of zero credit_spread_v1/iron_condor_v1 trades).

update_iv_history() used to only be reached from deep inside the
VIX>=12-gated credit_spread_v1/iron_condor_v1 pipeline -- a multi-day
LOW_VOL stretch (VIX<12) silently starved IV history for most of the
universe, so get_iv_rank()'s <20-day fail-closed check then blocked nearly
every real candidate the moment VIX allowed trading again. This job
seeds/refreshes history for the whole tracked universe once daily,
independent of VIX/regime.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeUniverseEngine:
    _refresh_iv_history_for_universe = LiveTradingEngine._refresh_iv_history_for_universe

    def __init__(self, symbols, market_data_by_symbol):
        self._symbols = symbols
        self._market_data_by_symbol = market_data_by_symbol
        self._get_iv_rank = AsyncMock(return_value=0.5)

    async def _get_market_data(self, symbol):
        return self._market_data_by_symbol.get(symbol)


@pytest.mark.asyncio
async def test_seeds_iv_history_for_every_symbol_with_valid_market_data():
    fake = _FakeUniverseEngine(
        symbols=["RELIANCE", "TCS"],
        market_data_by_symbol={
            "RELIANCE": {"close": 2500.0, "atr14": 25.0},
            "TCS": {"close": 3500.0, "atr14": 30.0},
        },
    )

    await fake._refresh_iv_history_for_universe()

    assert fake._get_iv_rank.await_count == 2
    called_symbols = {c.args[0] for c in fake._get_iv_rank.await_args_list}
    assert called_symbols == {"RELIANCE", "TCS"}


@pytest.mark.asyncio
async def test_passes_no_live_sigma_uses_atr_fallback_path():
    """Deliberately doesn't pass live_sigma -- this is a once-daily
    floor-fill using the ATR proxy, not a live option-quote fetch for the
    whole universe. See the method's docstring."""
    fake = _FakeUniverseEngine(
        symbols=["RELIANCE"],
        market_data_by_symbol={"RELIANCE": {"close": 2500.0, "atr14": 25.0}},
    )

    await fake._refresh_iv_history_for_universe()

    args, kwargs = fake._get_iv_rank.await_args
    assert "live_sigma" not in kwargs
    assert args == ("RELIANCE", 2500.0, 25.0)
    assert kwargs.get("dte") == 0


@pytest.mark.asyncio
async def test_skips_a_symbol_with_no_market_data():
    fake = _FakeUniverseEngine(
        symbols=["RELIANCE", "GHOSTSYM"],
        market_data_by_symbol={"RELIANCE": {"close": 2500.0, "atr14": 25.0}},
    )

    await fake._refresh_iv_history_for_universe()

    assert fake._get_iv_rank.await_count == 1
    assert fake._get_iv_rank.await_args.args[0] == "RELIANCE"


@pytest.mark.asyncio
async def test_skips_a_symbol_with_missing_or_zero_atr():
    fake = _FakeUniverseEngine(
        symbols=["RELIANCE", "ZEROATR"],
        market_data_by_symbol={
            "RELIANCE": {"close": 2500.0, "atr14": 25.0},
            "ZEROATR": {"close": 100.0, "atr14": 0.0},
        },
    )

    await fake._refresh_iv_history_for_universe()

    assert fake._get_iv_rank.await_count == 1
    assert fake._get_iv_rank.await_args.args[0] == "RELIANCE"


@pytest.mark.asyncio
async def test_skips_a_symbol_with_missing_or_zero_price():
    fake = _FakeUniverseEngine(
        symbols=["RELIANCE", "ZEROPRICE"],
        market_data_by_symbol={
            "RELIANCE": {"close": 2500.0, "atr14": 25.0},
            "ZEROPRICE": {"close": 0.0, "atr14": 5.0},
        },
    )

    await fake._refresh_iv_history_for_universe()

    assert fake._get_iv_rank.await_count == 1


@pytest.mark.asyncio
async def test_one_symbol_failing_does_not_abort_the_rest_of_the_universe():
    fake = _FakeUniverseEngine(
        symbols=["BROKEN", "RELIANCE"],
        market_data_by_symbol={
            "BROKEN": {"close": 100.0, "atr14": 1.0},
            "RELIANCE": {"close": 2500.0, "atr14": 25.0},
        },
    )

    async def _raise_then_ok(symbol, *args, **kwargs):
        if symbol == "BROKEN":
            raise ConnectionError("redis blip")
        return 0.5

    fake._get_iv_rank = AsyncMock(side_effect=_raise_then_ok)

    await fake._refresh_iv_history_for_universe()  # must not raise

    called_symbols = {c.args[0] for c in fake._get_iv_rank.await_args_list}
    assert called_symbols == {"BROKEN", "RELIANCE"}
