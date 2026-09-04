"""
Live incident, 2026-09-04: _square_off_all()'s EOD force-close on expiry day
used to gate EVERY tracked spread/condor on the GLOBAL near-month
`get_near_month_expiry()` DTE, not each structure's OWN stored
`expiry_date`. But _process_credit_spread/_process_iron_condor deliberately
roll fresh entries to NEXT month's contract once near-month DTE falls below
the strategy's entry_min_dte -- so for a real, recurring ~2-3 week window
every cycle, some open structures legitimately hold a next-month contract
weeks from their own real expiry while the near-month contract everyone else
is on approaches ITS expiry. When the global near-month DTE hit <=1, every
position -- including next-month structures -- was swept into the
force-close loop, cutting off the theta-decay runway the roll-forward logic
exists to protect. This is the exact bug class already fixed in the restore
path (see the "spread_expiry = datetime.fromisoformat(...)" fix a few
hundred lines earlier in live_trading_engine.py) but never applied here.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.utils import now_ist
from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeRiskMgr:
    def __init__(self):
        self.released = []

    def release_deployed_capital(self, strategy_name, amount):
        self.released.append((strategy_name, amount))


def _condor(expiry_date: str, prefix: str) -> dict:
    return {
        "journal_id": 1, "expiry_date": expiry_date,
        "put_short_contract": f"{prefix}PS", "put_long_contract": f"{prefix}PL",
        "call_short_contract": f"{prefix}CS", "call_long_contract": f"{prefix}CL",
        "put_short_strike": 100, "put_long_strike": 90,
        "call_short_strike": 110, "call_long_strike": 120,
        "put_short_premium": 5, "put_long_premium": 2,
        "call_short_premium": 5, "call_long_premium": 2,
        "lot_size": 50, "strategy_name": "iron_condor_v1",
        "put_short_gtt_id": 1, "call_short_gtt_id": 2,
    }


def _fake_engine(active_condors, active_spreads=None):
    return SimpleNamespace(
        _real_fill=LiveTradingEngine._real_fill,
        order_manager=SimpleNamespace(
            place_order=AsyncMock(return_value=SimpleNamespace(order_status="OPEN", fill_price=5.0))
        ),
        risk_manager=_FakeRiskMgr(),
        _peak_premiums={}, _single_leg_journals={},
        _active_spreads=active_spreads or {}, _active_condors=active_condors,
        _kite=None, _redis=None,
        _eod_notified_today=False,
        _get_market_data=AsyncMock(return_value={"atr14": 5.0}),
        _get_underlying_from_contract=lambda c: c[:-2],
        _log_trade_close=AsyncMock(),
        _persist_state=AsyncMock(),
        _notify=AsyncMock(),
        _cancel_gtt=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_square_off_holds_a_condor_rolled_to_next_month_on_near_month_expiry_day(monkeypatch):
    """The core bug: near-month contract is at DTE<=1 (global expiry day),
    but this condor rolled to NEXT month at entry and is genuinely weeks
    from its own real expiry -- must be held overnight, untouched."""
    from src.live_trading import live_trading_engine as lte_module

    near_month_expiry = now_ist().replace(tzinfo=None) + timedelta(days=1)  # global DTE=1
    condor_own_expiry = now_ist().replace(tzinfo=None) + timedelta(days=28)  # real DTE~28
    condor = _condor(condor_own_expiry.isoformat(), "ROLLED")

    fake = _fake_engine({"ROLLED": condor})

    async def _safe_get_positions():
        return [
            {"symbol": "ROLLEDPS", "quantity": -50, "avg_price": 5.0},
            {"symbol": "ROLLEDPL", "quantity": 50, "avg_price": 2.0},
            {"symbol": "ROLLEDCS", "quantity": -50, "avg_price": 5.0},
            {"symbol": "ROLLEDCL", "quantity": 50, "avg_price": 2.0},
        ]
    fake._safe_get_positions = _safe_get_positions

    monkeypatch.setattr(lte_module, "get_near_month_expiry", lambda: near_month_expiry)

    await LiveTradingEngine._square_off_all(fake)

    # Not force-closed: no order placed for any of its 4 legs, structure
    # stays tracked, no journal/capital-release/GTT-cancel.
    fake.order_manager.place_order.assert_not_called()
    assert "ROLLED" in fake._active_condors
    fake._log_trade_close.assert_not_called()
    assert fake.risk_manager.released == []
    fake._cancel_gtt.assert_not_called()


@pytest.mark.asyncio
async def test_square_off_still_force_closes_a_condor_genuinely_at_its_own_expiry(monkeypatch):
    """Guard against over-fixing: a condor that did NOT roll (its own
    expiry_date matches the global near-month expiry) must still be
    force-closed on expiry day exactly as before."""
    from src.live_trading import live_trading_engine as lte_module

    near_month_expiry = now_ist().replace(tzinfo=None) + timedelta(days=1)  # DTE=1
    condor = _condor(near_month_expiry.isoformat(), "NORMAL")

    fake = _fake_engine({"NORMAL": condor})

    async def _safe_get_positions():
        return [
            {"symbol": "NORMALPS", "quantity": -50, "avg_price": 5.0},
            {"symbol": "NORMALPL", "quantity": 50, "avg_price": 2.0},
            {"symbol": "NORMALCS", "quantity": -50, "avg_price": 5.0},
            {"symbol": "NORMALCL", "quantity": 50, "avg_price": 2.0},
        ]
    fake._safe_get_positions = _safe_get_positions

    monkeypatch.setattr(lte_module, "get_near_month_expiry", lambda: near_month_expiry)

    await LiveTradingEngine._square_off_all(fake)

    assert fake.order_manager.place_order.call_count == 4
    assert "NORMAL" not in fake._active_condors
    fake._log_trade_close.assert_called_once()
    assert fake.risk_manager.released
    fake._cancel_gtt.assert_called()


@pytest.mark.asyncio
async def test_square_off_can_both_hold_a_rolled_condor_and_close_a_normal_one_same_cycle(monkeypatch):
    """A mixed portfolio on global expiry day: one condor rolled forward
    (held), one condor genuinely at its own expiry (force-closed) -- both
    must be handled correctly in the same _square_off_all() call, which the
    old global is_expiry either/or branch could not represent."""
    from src.live_trading import live_trading_engine as lte_module

    near_month_expiry = now_ist().replace(tzinfo=None) + timedelta(days=1)
    rolled_expiry = now_ist().replace(tzinfo=None) + timedelta(days=28)

    rolled = _condor(rolled_expiry.isoformat(), "ROLLED")
    normal = _condor(near_month_expiry.isoformat(), "NORMAL")

    fake = _fake_engine({"ROLLED": rolled, "NORMAL": normal})

    async def _safe_get_positions():
        return [
            {"symbol": "ROLLEDPS", "quantity": -50, "avg_price": 5.0},
            {"symbol": "ROLLEDPL", "quantity": 50, "avg_price": 2.0},
            {"symbol": "ROLLEDCS", "quantity": -50, "avg_price": 5.0},
            {"symbol": "ROLLEDCL", "quantity": 50, "avg_price": 2.0},
            {"symbol": "NORMALPS", "quantity": -50, "avg_price": 5.0},
            {"symbol": "NORMALPL", "quantity": 50, "avg_price": 2.0},
            {"symbol": "NORMALCS", "quantity": -50, "avg_price": 5.0},
            {"symbol": "NORMALCL", "quantity": 50, "avg_price": 2.0},
        ]
    fake._safe_get_positions = _safe_get_positions

    monkeypatch.setattr(lte_module, "get_near_month_expiry", lambda: near_month_expiry)

    await LiveTradingEngine._square_off_all(fake)

    assert fake.order_manager.place_order.call_count == 4  # only NORMAL's 4 legs
    assert "ROLLED" in fake._active_condors
    assert "NORMAL" not in fake._active_condors
    fake._notify.assert_called()  # EOD summary must mention both closed and held
