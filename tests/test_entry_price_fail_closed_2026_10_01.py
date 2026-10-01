"""
get_entry_prices_for_spread() (2026-10-01, external review -- confirmed
against the actual code).

Previously fell back to an ATR-based ESTIMATE (estimate_option_premium())
for whichever leg's real Zerodha quote was unavailable, and only failed
closed on the resulting INVERSION edge case. A single missing quote whose
estimate still happened to produce a non-inverted pair sailed through as a
real tradeable price -- directly contradicting the caller's own log
message ("fail-closed, not placing a LIMIT order on a guessed price").
Now both legs must have a real, positive quote or the whole call fails
closed, matching the identical fix already applied to
vix_allows_selling()/iv_rank_allows_selling() on 2026-08-28.
"""
import asyncio

import src.market_data.option_chain as oc
from src.market_data.option_chain import get_entry_prices_for_spread


def _run(short_quote, long_quote, atr=10.0, dte=5):
    async def _fake_quote(contract, kite, redis):
        if "SHORT" in contract:
            return short_quote
        return long_quote

    orig = oc.get_option_quote
    oc.get_option_quote = _fake_quote
    try:
        return asyncio.run(
            get_entry_prices_for_spread(
                "RELIANCE", "RELIANCE-SHORT", "RELIANCE-LONG", None, None, atr=atr, dte=dte,
            )
        )
    finally:
        oc.get_option_quote = orig


def test_returns_none_when_short_quote_missing():
    # A large ATR/dte here would previously have produced a plausible
    # non-inverted ESTIMATE for the short leg -- must still fail closed now.
    result = _run(short_quote=None, long_quote=5.0, atr=50.0, dte=20)
    assert result is None


def test_returns_none_when_long_quote_missing():
    result = _run(short_quote=10.0, long_quote=None, atr=50.0, dte=20)
    assert result is None


def test_returns_none_when_both_quotes_missing():
    result = _run(short_quote=None, long_quote=None)
    assert result is None


def test_returns_none_when_a_quote_is_zero_or_negative():
    """0 is Zerodha's own 'no last traded price yet' sentinel, same
    fail-closed treatment as a genuinely missing quote."""
    result = _run(short_quote=0.0, long_quote=5.0)
    assert result is None


def test_returns_real_prices_when_both_quotes_are_valid_and_not_inverted():
    result = _run(short_quote=10.0, long_quote=5.0)
    assert result == (10.0, 5.0)


def test_returns_none_on_inversion_with_two_real_quotes():
    result = _run(short_quote=5.0, long_quote=8.0)
    assert result is None
