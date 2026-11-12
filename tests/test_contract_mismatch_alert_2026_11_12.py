"""
Live incident (2026-11-12): ONGC's open credit_spread_v1 position was found
trading on strikes 222.5/215 -- neither is a real, listed NSE contract (the
real strikes near there are 222.0/224.5 and 214.5/217.0). _resolve_contract()'s
fail-closed validation only ever runs at ENTRY time; nothing re-checked an
already-open position against the real-contract cache afterward, so this
went unnoticed until a human spotted a stuck market_price=0.0 on the
dashboard. LiveTradingEngine._check_contract_still_real() closes that gap --
called every exit-check cycle from _check_spread_exits()/_check_condor_exits(),
it re-validates each leg's stored strike against get_real_contract() and
fires a one-time CRITICAL alert + notification if they've drifted apart.

Deliberately alert-only, never a forced close -- see
LiveTradingEngine.__init__'s _contract_mismatch_alerted comment for why.
"""
import inspect

import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeMismatchEngine:
    _check_contract_still_real = LiveTradingEngine._check_contract_still_real

    def __init__(self):
        self._contract_mismatch_alerted = set()
        self._redis = object()  # just needs to be truthy
        self._notify_calls = []

    async def _notify(self, message):
        self._notify_calls.append(message)


@pytest.mark.asyncio
async def test_fires_when_stored_strike_is_not_real(monkeypatch):
    async def _fake_get_real_contract(symbol, expiry, strike, option_type, redis):
        # Real cache resolves to a DIFFERENT strike than what's stored.
        return ("ONGC26DEC222PE", 222.0)

    monkeypatch.setattr(
        "src.market_data.option_chain.get_real_contract", _fake_get_real_contract,
    )
    fake = _FakeMismatchEngine()

    await fake._check_contract_still_real(
        "ONGC", None, [("short", 222.5, "PE")],
    )

    assert "ONGC" in fake._contract_mismatch_alerted
    assert len(fake._notify_calls) == 1
    assert "222.5" in fake._notify_calls[0]
    assert "222.0" in fake._notify_calls[0]


@pytest.mark.asyncio
async def test_does_not_fire_when_stored_strike_matches_real(monkeypatch):
    async def _fake_get_real_contract(symbol, expiry, strike, option_type, redis):
        return ("ONGC26DEC222.5PE", strike)  # exact match, same strike back

    monkeypatch.setattr(
        "src.market_data.option_chain.get_real_contract", _fake_get_real_contract,
    )
    fake = _FakeMismatchEngine()

    await fake._check_contract_still_real(
        "ONGC", None, [("short", 222.5, "PE")],
    )

    assert fake._contract_mismatch_alerted == set()
    assert fake._notify_calls == []


@pytest.mark.asyncio
async def test_cache_miss_is_inconclusive_not_a_mismatch(monkeypatch):
    """A symbol the real-contract cache has no data for at all (e.g. a
    transient Redis blip) must NOT be treated as evidence of a mismatch --
    there's nothing to compare against."""
    async def _fake_get_real_contract(symbol, expiry, strike, option_type, redis):
        return None

    monkeypatch.setattr(
        "src.market_data.option_chain.get_real_contract", _fake_get_real_contract,
    )
    fake = _FakeMismatchEngine()

    await fake._check_contract_still_real(
        "ONGC", None, [("short", 222.5, "PE")],
    )

    assert fake._contract_mismatch_alerted == set()
    assert fake._notify_calls == []


@pytest.mark.asyncio
async def test_alert_fires_only_once_per_underlying(monkeypatch):
    calls = []

    async def _fake_get_real_contract(symbol, expiry, strike, option_type, redis):
        calls.append(strike)
        return ("ONGC26DEC222PE", 222.0)

    monkeypatch.setattr(
        "src.market_data.option_chain.get_real_contract", _fake_get_real_contract,
    )
    fake = _FakeMismatchEngine()

    await fake._check_contract_still_real("ONGC", None, [("short", 222.5, "PE")])
    await fake._check_contract_still_real("ONGC", None, [("short", 222.5, "PE")])

    assert len(fake._notify_calls) == 1, "must not re-alert on every cycle once already flagged"
    assert len(calls) == 1, "must not even re-check once already alerted for this underlying"


@pytest.mark.asyncio
async def test_checks_every_leg_not_just_the_first(monkeypatch):
    """A matching short leg must not short-circuit the check for the long leg."""
    async def _fake_get_real_contract(symbol, expiry, strike, option_type, redis):
        if strike == 222.5:
            return ("ONGC26DEC222.5PE", 222.5)  # short leg: matches, real
        return ("ONGC26DEC214.5PE", 214.5)  # long leg: stored 215 doesn't match

    monkeypatch.setattr(
        "src.market_data.option_chain.get_real_contract", _fake_get_real_contract,
    )
    fake = _FakeMismatchEngine()

    await fake._check_contract_still_real(
        "ONGC", None, [("short", 222.5, "PE"), ("long", 215, "PE")],
    )

    assert "ONGC" in fake._contract_mismatch_alerted
    assert "long" in fake._notify_calls[0]
    assert "214.5" in fake._notify_calls[0]


@pytest.mark.asyncio
async def test_no_op_without_redis():
    fake = _FakeMismatchEngine()
    fake._redis = None

    await fake._check_contract_still_real("ONGC", None, [("short", 222.5, "PE")])

    assert fake._contract_mismatch_alerted == set()
    assert fake._notify_calls == []


def test_check_spread_exits_calls_the_mismatch_check():
    src = inspect.getsource(LiveTradingEngine._check_spread_exits)
    assert "await self._check_contract_still_real(" in src
    idx_check = src.index("await self._check_contract_still_real(")
    idx_quote = src.index("get_option_quote")
    assert idx_check < idx_quote, (
        "contract-mismatch check should run before the quote-fetch/exit-decision "
        "logic, not after"
    )


def test_check_condor_exits_calls_the_mismatch_check():
    src = inspect.getsource(LiveTradingEngine._check_condor_exits)
    assert "await self._check_contract_still_real(" in src
    idx_check = src.index("await self._check_contract_still_real(")
    idx_quote = src.index("get_option_quote")
    assert idx_check < idx_quote
