"""
Live incident, 2026-10-15 (found during the ema_crossover_v1 trade review,
user-flagged for a fix): _close_option_positions() (the "Reversal exit"
closer) never passed underlying_mfe_pct/underlying_mae_pct/option_mfe_pct/
option_mae_pct through to _log_trade_close(), unlike every OTHER exit path
(_execute_single_leg_exit, EOD square-off, EXIT-signal close), which all
read these same 4 fields off the single-leg journal dict
(mfe_underlying_pct/mae_underlying_pct/mfe_option_pct/mae_option_pct,
updated every cycle by the position-monitoring loop) and pass them through.

_log_trade_close() leaves these fields at their entry-time-seeded 0.0
default when the caller passes None (see its own "only overwrite ... when
the caller actually tracked these" comment) -- so every reversal-exit row
showed exactly 0.0 for all 4 fields regardless of real hold time or
symbol, making that entire bucket of trades (15/15 losses in the Oct 8-15
sample) impossible to analyze.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeRiskMgr:
    def __init__(self):
        self.released = []

    def release_deployed_capital(self, strategy_name, amount):
        self.released.append((strategy_name, amount))


@pytest.mark.asyncio
async def test_reversal_exit_passes_real_tracked_mfe_mae_through():
    position = {"symbol": "HDFCLIFE26OCT550PE", "quantity": 1100, "avg_price": 13.54}

    fake = SimpleNamespace(
        _real_fill=LiveTradingEngine._real_fill,
        order_manager=SimpleNamespace(
            place_order=AsyncMock(return_value=SimpleNamespace(order_status="COMPLETE", fill_price=12.32)),
        ),
        risk_manager=_FakeRiskMgr(),
        _peak_premiums={}, _peak_profits={},
        _single_leg_journals={
            "HDFCLIFE26OCT550PE": {
                "journal_id": 42, "strategy_name": "ema_crossover_v1",
                # Real tracked peak/trough values, as the position-monitoring
                # loop would have accumulated them over the hold.
                "mfe_underlying_pct": 0.85, "mae_underlying_pct": -0.31,
                "mfe_option_pct": 12.4, "mae_option_pct": -9.8,
            },
        },
        _kite=None, _redis=None,
        _safe_get_positions=AsyncMock(return_value=[position]),
        _get_market_data=AsyncMock(return_value={"atr14": 5.0}),
        _log_trade_close=AsyncMock(),
        _persist_state=AsyncMock(),
    )

    await LiveTradingEngine._close_option_positions(
        fake, "HDFCLIFE", "PE", {"atr14": 5.0}, owner_strategy_name=None,
    )

    assert fake._log_trade_close.await_count == 1
    kwargs = fake._log_trade_close.call_args.kwargs
    assert kwargs["underlying_mfe_pct"] == 0.85
    assert kwargs["underlying_mae_pct"] == -0.31
    assert kwargs["option_mfe_pct"] == 12.4
    assert kwargs["option_mae_pct"] == -9.8
