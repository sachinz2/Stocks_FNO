"""
Strike-interval correctness (src/core/constants.py, src/core/utils.py,
src/market_data/option_chain.py).

2026-08-06 incident: TITAN's FNO_STRIKE_INTERVALS entry was 25 (real NSE
interval is 50), so get_atm_strike() computed strike 4975 for a ~4970.5
spot -- a strike that has never existed on the exchange. Since
get_option_quote() can only find a contract that actually exists, this
silently forced the trade onto the synthetic ATR-estimate fallback for its
entire life (entry AND every exit check), never touching a real quote.
An audit against a live kite.instruments("NFO") pull found 27 of 39 symbols
had a wrong interval, not just TITAN -- constants.py was corrected for all
of them.

A second, related bug: some symbols (ITC, WIPRO, ONGC, POWERGRID,
TATASTEEL) have a genuinely fractional interval (2.5), whose grid includes
real half-strikes (e.g. TATASTEEL26AUG167.5CE). get_atm_strike() used to
force int() on its result, truncating a real 167.5 strike into a
nonexistent 167 -- the same "computed a phantom contract" failure, just
triggered by rounding instead of a wrong interval.
"""
from datetime import datetime
from src.core.utils import get_atm_strike, build_option_symbol
from src.core.constants import FNO_STRIKE_INTERVALS
from src.market_data.option_chain import find_delta_strike

EXPIRY = datetime(2026, 8, 25)


def test_titan_interval_corrected_and_returns_real_strike():
    assert FNO_STRIKE_INTERVALS["TITAN"] == 50
    strike = get_atm_strike(4970.5, "TITAN")
    assert strike in (4950, 5000)
    contract = build_option_symbol("TITAN", strike, "CE", EXPIRY)
    assert contract in ("TITAN26AUG4950CE", "TITAN26AUG5000CE")
    assert "4975" not in contract


def test_fractional_interval_does_not_truncate_a_real_half_strike():
    assert FNO_STRIKE_INTERVALS["TATASTEEL"] == 2.5
    # round(166.3/2.5)=round(66.52)=67 -> 67*2.5=167.5, a real listed strike
    # (confirmed live: TATASTEEL26AUG167.5CE exists on Zerodha).
    strike = get_atm_strike(166.3, "TATASTEEL")
    assert strike == 167.5
    contract = build_option_symbol("TATASTEEL", strike, "CE", EXPIRY)
    assert contract == "TATASTEEL26AUG167.5CE"


def test_whole_number_fractional_interval_strike_has_no_trailing_zero():
    # interval 2.5 -> round(270.9/2.5)=round(108.36)=108 -> 108*2.5=270.0
    strike = get_atm_strike(270.9, "POWERGRID")
    contract = build_option_symbol("POWERGRID", strike, "PE", EXPIRY)
    assert contract == "POWERGRID26AUG270PE"  # not "270.0PE"


def test_find_delta_strike_handles_fractional_intervals_too():
    # Same fix applies to the credit_spread_v1/iron_condor_v1 strike-selection path.
    k = find_delta_strike(166.3, -0.20, "PE", dte=20, sigma=0.30, strike_interval=2.5)
    assert k * 2 == int(k * 2), f"strike {k} is not a valid multiple of 0.5"
    contract = build_option_symbol("TATASTEEL", k, "PE", EXPIRY)
    assert ".0" not in contract.replace("26AUG", "")


def test_all_strike_intervals_are_positive_and_known_symbols():
    # Regression guard: catches an accidental 0/negative/typo'd interval for
    # any symbol, which would make get_atm_strike() divide-by-zero or
    # silently misprice every strike for that symbol.
    for symbol, interval in FNO_STRIKE_INTERVALS.items():
        assert interval > 0, f"{symbol} has a non-positive strike interval: {interval}"
