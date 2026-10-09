"""
_check_multi_leg_liquidity() (2026-10-01) -- credit_spread_v1/iron_condor_v1
never had the bid-ask-spread liquidity check that single-leg strategies got
on 2026-09-16 ("option_quality_check"), despite placing real multi-leg
LIMIT orders the exact same way. Traced two real catastrophic single-leg
losses to thin, wide-spread contracts entered before that filter existed
(KAYNES -67.2% on a -0.13% underlying move; IDEA's stop-loss decided at
-18.6% but filled at -45.7%) -- both predate the single-leg fix by weeks.
This closes the same gap for multi-leg entries.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeEngine:
    _check_multi_leg_liquidity = LiveTradingEngine._check_multi_leg_liquidity
    _OPTION_MAX_SPREAD_PCT = LiveTradingEngine._OPTION_MAX_SPREAD_PCT

    def __init__(self, kite=object()):
        self._kite = kite
        self._last_gate_rejection = None


@pytest.mark.asyncio
async def test_passes_when_every_leg_is_within_spread_tolerance(monkeypatch):
    import src.live_trading.live_trading_engine as engine_mod

    async def _fake_quality(contract, kite):
        return {"spread_pct": 2.0, "oi": 1000, "volume": 500}

    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quality_metrics", _fake_quality,
    )
    fake = _FakeEngine()
    strategy = SimpleNamespace()

    ok = await fake._check_multi_leg_liquidity(strategy, "CreditSpread", ["RELIANCE-SHORT", "RELIANCE-LONG"])

    assert ok is True
    assert fake._last_gate_rejection is None


@pytest.mark.asyncio
async def test_fails_closed_on_the_first_too_wide_leg(monkeypatch):
    # EXPERIMENT (2026-10-08, explicit user instruction): _OPTION_MAX_SPREAD_PCT
    # raised 8.0 -> 40.0 -> 150.0, so the "wide" fixture value must exceed
    # 150.0 to still exercise the block. Restore 15.0 (and the 8.0 threshold)
    # after reviewing case-by-case losses.
    async def _fake_quality(contract, kite):
        if "LONG" in contract:
            return {"spread_pct": 180.0, "oi": 10, "volume": 2}  # wide -- illiquid
        return {"spread_pct": 2.0, "oi": 1000, "volume": 500}

    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quality_metrics", _fake_quality,
    )
    fake = _FakeEngine()
    strategy = SimpleNamespace()

    ok = await fake._check_multi_leg_liquidity(strategy, "CreditSpread", ["RELIANCE-SHORT", "RELIANCE-LONG"])

    assert ok is False
    assert fake._last_gate_rejection["reason"] == "OPTION_SPREAD_TOO_WIDE"
    assert fake._last_gate_rejection["value"] == 180.0
    assert fake._last_gate_rejection["threshold"] == LiveTradingEngine._OPTION_MAX_SPREAD_PCT


@pytest.mark.asyncio
async def test_checks_all_four_iron_condor_legs_not_just_the_first_two(monkeypatch):
    checked = []

    # EXPERIMENT (2026-10-08/09, explicit user instruction): fixture value
    # raised above the current 150.0 threshold (was 20.0 under the original
    # 8.0 threshold, then 50.0 under 40.0).
    async def _fake_quality(contract, kite):
        checked.append(contract)
        if contract == "CALL_LONG":
            return {"spread_pct": 180.0, "oi": 5, "volume": 1}
        return {"spread_pct": 1.5, "oi": 1000, "volume": 500}

    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quality_metrics", _fake_quality,
    )
    fake = _FakeEngine()
    strategy = SimpleNamespace()

    ok = await fake._check_multi_leg_liquidity(
        strategy, "IronCondor", ["PUT_SHORT", "PUT_LONG", "CALL_SHORT", "CALL_LONG"],
    )

    assert ok is False
    assert checked == ["PUT_SHORT", "PUT_LONG", "CALL_SHORT", "CALL_LONG"]  # checked in order, stopped at the bad one
    assert fake._last_gate_rejection["reason"] == "OPTION_SPREAD_TOO_WIDE"


@pytest.mark.asyncio
async def test_fails_open_when_spread_is_unmeasurable_same_as_single_leg_convention(monkeypatch):
    async def _fake_quality(contract, kite):
        return {"spread_pct": None, "oi": None, "volume": None}  # one-sided/empty book

    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quality_metrics", _fake_quality,
    )
    fake = _FakeEngine()
    strategy = SimpleNamespace()

    ok = await fake._check_multi_leg_liquidity(strategy, "CreditSpread", ["RELIANCE-SHORT", "RELIANCE-LONG"])

    assert ok is True


@pytest.mark.asyncio
async def test_strategy_can_opt_out_via_option_quality_check_false(monkeypatch):
    async def _fake_quality(contract, kite):
        return {"spread_pct": 50.0, "oi": 1, "volume": 0}  # would otherwise fail

    monkeypatch.setattr(
        "src.market_data.option_chain.get_option_quality_metrics", _fake_quality,
    )
    fake = _FakeEngine()
    strategy = SimpleNamespace(option_quality_check=False)

    ok = await fake._check_multi_leg_liquidity(strategy, "CreditSpread", ["RELIANCE-SHORT", "RELIANCE-LONG"])

    assert ok is True


@pytest.mark.asyncio
async def test_wired_into_both_credit_spread_and_iron_condor_before_net_credit_check():
    import inspect
    cs_src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    ic_src = inspect.getsource(LiveTradingEngine._process_iron_condor)

    for src, label in ((cs_src, "credit_spread"), (ic_src, "iron_condor")):
        liq_idx = src.find("_check_multi_leg_liquidity")
        credit_idx = src.find("MIN_SPREAD_NET_CREDIT" if label == "credit_spread" else "MIN_CONDOR_NET_CREDIT")
        assert liq_idx != -1, f"{label}: liquidity check not wired in"
        assert liq_idx < credit_idx, f"{label}: liquidity check must run before the net-credit check"
