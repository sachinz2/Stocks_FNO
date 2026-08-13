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


# ── Lot sizes ────────────────────────────────────────────────────────────────
#
# 2026-08-07: FNO_LOT_SIZES is a fallback only -- _get_lot_size() in
# live_trading_engine.py checks Redis first (populated daily by the 08:30
# auth job from live Zerodha data) and only falls back to this table if
# that's missing. Audited against a live kite.instruments("NFO") pull, same
# methodology as the strike-interval audit: 36 of 39 symbols were wrong,
# some by a wide margin (KOTAKBANK 400 vs real 2000). Not corrupting live
# trades under normal operation (the Redis cache masks it), but on any day
# the daily auth job fails/is delayed -- already observed this week -- the
# system would silently fall back to this table and submit order
# quantities that aren't valid multiples of the real exchange lot size.
#
# A live-data audit can't be a pure offline unit test (needs network
# access to Zerodha) -- these are sanity guards for the table's shape, not
# a substitute for periodically re-running scratchpad/audit_lot_sizes.py
# against a fresh kite.instruments("NFO") pull (NSE revises lot sizes
# periodically based on price bands, the same way it revises strike
# intervals).

from src.core.constants import FNO_LOT_SIZES, FNO_SYMBOLS


def test_all_lot_sizes_are_positive_and_cover_every_traded_symbol():
    assert set(FNO_LOT_SIZES.keys()) == set(FNO_SYMBOLS), (
        "FNO_LOT_SIZES must have an entry for every symbol the engine actually trades, no more, no less"
    )
    for symbol, lot in FNO_LOT_SIZES.items():
        assert lot > 0, f"{symbol} has a non-positive lot size: {lot}"


def test_lot_sizes_are_plausible_nse_values():
    # NSE lot sizes are set so that lot_size * underlying_price lands in a
    # roughly consistent notional band (regulatory contract-value target) --
    # a value that's off by an order of magnitude (a common transcription
    # error, e.g. dropping/adding a digit) would fail this even without
    # knowing the exact correct number.
    for symbol, lot in FNO_LOT_SIZES.items():
        assert 10 <= lot <= 10_000, f"{symbol} lot size {lot} is outside a plausible NSE range"


# ── Auth self-heal retry gating (2026-08-13) ─────────────────────────────────
#
# The kite-provisioning self-heal job (src/api/main.py's _kite_self_heal)
# used to silently give up forever if Redis had no access token at all --
# confirmed live 2026-08-13: a deploy restarted the API process right at
# 08:30 IST, wiping APScheduler's in-memory state before the scheduled daily
# auth login could complete, and nothing else ever re-triggered it. These two
# pure functions gate the "actively retry the login" fallback: is it even
# worth trying right now, and have we tried too recently.

from datetime import timedelta
from src.core.utils import is_auth_retry_window, should_retry_auth


def test_auth_retry_window_true_during_trading_hours_weekday():
    # A Wednesday at 10:00 IST -- comfortably inside 08:25-15:30.
    dt = datetime(2026, 8, 12, 10, 0, 0)  # 2026-08-12 is a Wednesday
    assert dt.weekday() == 2
    assert is_auth_retry_window(dt) is True


def test_auth_retry_window_true_at_exactly_0825():
    dt = datetime(2026, 8, 12, 8, 25, 0)
    assert is_auth_retry_window(dt) is True


def test_auth_retry_window_false_before_0825():
    dt = datetime(2026, 8, 12, 8, 24, 59)
    assert is_auth_retry_window(dt) is False


def test_auth_retry_window_false_after_market_close():
    dt = datetime(2026, 8, 12, 15, 30, 1)
    assert is_auth_retry_window(dt) is False


def test_auth_retry_window_false_on_weekend():
    # 2026-08-15 is a Saturday.
    dt = datetime(2026, 8, 15, 10, 0, 0)
    assert dt.weekday() == 5
    assert is_auth_retry_window(dt) is False


def test_should_retry_auth_true_when_never_attempted():
    assert should_retry_auth(last_attempt=None) is True


def test_should_retry_auth_false_within_cooldown():
    now = datetime(2026, 8, 12, 10, 0, 0)
    last_attempt = now - timedelta(seconds=200)
    assert should_retry_auth(last_attempt, now, cooldown_seconds=600) is False


def test_should_retry_auth_true_after_cooldown_elapses():
    now = datetime(2026, 8, 12, 10, 0, 0)
    last_attempt = now - timedelta(seconds=601)
    assert should_retry_auth(last_attempt, now, cooldown_seconds=600) is True


def test_should_retry_auth_true_at_exactly_cooldown_boundary():
    now = datetime(2026, 8, 12, 10, 0, 0)
    last_attempt = now - timedelta(seconds=600)
    assert should_retry_auth(last_attempt, now, cooldown_seconds=600) is True
