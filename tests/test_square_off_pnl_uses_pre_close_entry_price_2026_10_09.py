"""
Live incident, 2026-10-09: _square_off_all()'s pnl for every EOD-square-off
close was wrong -- not just slightly off, but reporting a huge FAKE PROFIT
for trades that were real losses. Confirmed live:

    KALYANKJIL   entry=16.07  exit=14.94  qty=1350  reported pnl=+20169.00
                 real pnl = (14.94-16.07)*1350 = -1525.50

Root cause: `pos` (from _safe_get_positions()) is a LIVE reference into
PaperBroker._positions -- get_positions() returns the actual mutable dicts,
not copies. _update_position() sets a fully-closed position's avg_price to
0.0 the moment its quantity nets to zero (see paper_broker.py's "elif
new_qty == 0: avg_price = 0.0" branch). _square_off_all() read
pos.get("avg_price") a SECOND time, AFTER calling place_order() to close
the position -- by then the SELL had already filled and zeroed it out, so
the "entry price" used for pnl was silently 0.0, turning
(exit_price - 0) * qty into exit_price * qty (the position's gross
notional value, not its profit/loss).

Fixed by reusing `entry_p` (captured once, before place_order() runs) for
the pnl/capital-release math instead of re-reading from the live dict.

A near-identical bug existed in _exit_all_options_for() (the "EXIT" signal
single-leg closer): it re-read pos["quantity"] for pnl AFTER the close
order zeroed it, producing pnl=0 and releasing ZERO deployed capital back
to the owning strategy's budget on every such exit -- a real capital leak,
not just a cosmetic number. Fixed by capturing _qty once, before the close.

These tests use a `place_order` AsyncMock with a side_effect that mutates
the position dict in place -- exactly what the real PaperBroker does and
every prior test for these methods did NOT simulate, which is why no
existing test caught this.
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


def _mutating_place_order(position: dict):
    """Simulates PaperBroker._update_position()'s real side effect: a SELL
    that exactly closes a long position zeroes both quantity and avg_price
    on the SAME dict object the caller is still holding a reference to."""
    async def _place_order(contract, side, qty, price, is_exit_order=False, **kwargs):
        position["quantity"] = 0
        position["avg_price"] = 0.0
        return SimpleNamespace(order_status="COMPLETE", fill_price=price)
    return AsyncMock(side_effect=_place_order)


@pytest.mark.asyncio
async def test_square_off_pnl_uses_the_pre_close_entry_price_not_the_post_close_zero(monkeypatch):
    # Real live incident numbers: KALYANKJIL entered @16.07, a real loss at
    # exit @14.94 -- the bug reported this as pnl=+20169.00 (=14.94*1350,
    # the position's gross notional value), not the real -1525.50 loss.
    position = {"symbol": "KALYANKJIL26OCT550PE", "quantity": 1350, "avg_price": 16.07}

    fake = SimpleNamespace(
        _real_fill=LiveTradingEngine._real_fill,
        order_manager=SimpleNamespace(place_order=_mutating_place_order(position)),
        risk_manager=_FakeRiskMgr(),
        _peak_premiums={}, _peak_profits={},
        _single_leg_journals={"KALYANKJIL26OCT550PE": {
            "journal_id": 1, "strategy_name": "ema_crossover_v1",
        }},
        _active_spreads={}, _active_condors={},
        _kite=None, _redis=None,
        _eod_notified_today=False,
        _safe_get_positions=AsyncMock(return_value=[position]),
        _get_underlying_from_contract=lambda c: "KALYANKJIL",
        _get_market_data=AsyncMock(return_value={"atr14": 5.0}),
        _log_trade_close=AsyncMock(),
        _persist_state=AsyncMock(),
        _notify=AsyncMock(),
        _cancel_gtt=AsyncMock(),
    )

    # Pin the exit price so the expected pnl is deterministic, instead of
    # whatever estimate_option_premium() would derive from atr14.
    # _square_off_all() does `from src.market_data.option_chain import
    # get_option_quote` as a LOCAL import at call time, so the patch target
    # is the source module's attribute, not live_trading_engine's namespace.
    import src.market_data.option_chain as option_chain_module
    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=14.94))

    await LiveTradingEngine._square_off_all(fake)

    assert fake._log_trade_close.await_count == 1
    kwargs = fake._log_trade_close.call_args.kwargs
    assert kwargs["pnl"] == pytest.approx(-1525.50, abs=0.01), (
        "pnl must be (exit - entry) * qty = (14.94 - 16.07) * 1350 = "
        f"-1525.50 -- got {kwargs['pnl']} (exit_price * qty = 20169.0 is "
        "the exact shape of the live bug: entry price silently read as "
        "the post-close zeroed-out avg_price instead of the real 16.07)"
    )
    released_amount = fake.risk_manager.released[0][1]
    assert released_amount == pytest.approx(16.07 * 1350, abs=0.01), (
        "capital release must use the REAL entry price (16.07), not the "
        "post-close zeroed-out avg_price"
    )


@pytest.mark.asyncio
async def test_exit_all_options_for_pnl_and_capital_release_use_the_pre_close_quantity():
    position = {"symbol": "RELIANCE26OCT1400CE", "quantity": 500, "avg_price": 20.0}

    fake = SimpleNamespace(
        order_manager=SimpleNamespace(place_order=_mutating_place_order(position)),
        risk_manager=_FakeRiskMgr(),
        _real_fill=LiveTradingEngine._real_fill,
        _peak_premiums={}, _peak_profits={},
        _single_leg_journals={"RELIANCE26OCT1400CE": {
            "journal_id": 7, "strategy_name": "momentum_v1", "underlying": "RELIANCE",
        }},
        _active_spreads={}, _active_condors={},
        _kite=None, _redis=None,
        _exited_today=set(),
        _safe_get_positions=AsyncMock(return_value=[position]),
        _get_market_data=AsyncMock(return_value={"atr14": 5.0}),
        _log_trade_close=AsyncMock(),
        _persist_state=AsyncMock(),
        _notify=AsyncMock(),
    )

    await LiveTradingEngine._exit_all_options_for(fake, "RELIANCE")

    assert fake._log_trade_close.await_count == 1
    kwargs = fake._log_trade_close.call_args.kwargs
    assert kwargs["pnl"] != 0, (
        "pnl must be computed from the REAL pre-close quantity (500), not "
        "the post-close zeroed-out quantity -- a real quantity bug here "
        "silently reports every such exit as a wash"
    )
    released_amount = fake.risk_manager.released[0][1]
    assert released_amount == pytest.approx(20.0 * 500, abs=0.01), (
        "capital release must use the REAL pre-close quantity (500), not "
        "the post-close zero -- this was leaking deployed capital forever"
    )
