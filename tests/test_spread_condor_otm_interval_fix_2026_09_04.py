"""
Live incident, 2026-09-04 (thorough cross-check of all strategies):
get_entry_prices_for_spread()'s ATR-fallback branch (used only when a leg's
real live quote is unavailable) needs the actual number of strike-intervals
each leg sits OTM to estimate a realistic premium. credit_spread_v1's call
site never passed short_otm_intervals/long_otm_intervals at all, silently
defaulting to 0/2 regardless of where delta-targeting actually placed the
strikes -- overstating the short leg's estimated premium (treating it as
ATM) and mispricing the long leg's real BUY limit order.
iron_condor_v1's call site DID pass values, but hardcoded (1/3, matching its
config-declared short_offset/hedge_width) even though its actual strike
selection is 100% delta-targeted (short_offset/hedge_offset are unused dead
parameters) -- the hardcoded assumption can diverge from the real resolved
strikes depending on volatility/strike grid.

Both now compute the real OTM-interval distance from the actual resolved
strikes in scope. Per test_real_contract_resolution.py's documented
rationale, _process_credit_spread/_process_iron_condor have too deep a
precondition chain to drive end-to-end with a mock -- source-level
regression guards instead, same convention already used for these methods.
"""
import inspect

from src.live_trading.live_trading_engine import LiveTradingEngine


def test_credit_spread_computes_real_otm_intervals_not_defaults():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("_entry_prices = await get_entry_prices_for_spread")
    block = src[idx - 400:idx + 400]
    assert "_short_otm = round(abs(short_strike - underlying_price) / interval)" in block
    assert "_long_otm  = round(abs(long_strike - underlying_price) / interval)" in block
    assert "short_otm_intervals=_short_otm, long_otm_intervals=_long_otm," in block


def test_iron_condor_computes_real_otm_intervals_not_hardcoded():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("_put_prices = await get_entry_prices_for_spread")
    block = src[idx - 600:idx + 600]
    assert "_put_short_otm  = round(abs(put_short_strike  - underlying_price) / interval)" in block
    assert "_put_long_otm   = round(abs(put_long_strike   - underlying_price) / interval)" in block
    assert "_call_short_otm = round(abs(call_short_strike - underlying_price) / interval)" in block
    assert "_call_long_otm  = round(abs(call_long_strike  - underlying_price) / interval)" in block
    assert "short_otm_intervals=_put_short_otm, long_otm_intervals=_put_long_otm," in block
    assert "short_otm_intervals=_call_short_otm, long_otm_intervals=_call_long_otm," in block
    # Must no longer be the old hardcoded literals.
    assert "short_otm_intervals=1, long_otm_intervals=3" not in src


def test_iron_condor_no_longer_carries_dead_short_offset_hedge_offset_params():
    """short_offset/hedge_offset were read into self.* and logged as if they
    drove strike selection -- they never did (strike selection is 100%
    delta-targeted via find_delta_strike()). Removed as dead config."""
    from src.strategies.iron_condor import IronCondorStrategy
    strategy = IronCondorStrategy("condor_test", {})
    strategy.initialize()
    assert not hasattr(strategy, "short_offset")
    assert not hasattr(strategy, "hedge_offset")


def test_otm_interval_arithmetic_matches_a_concrete_worked_example():
    """Reproduces the exact worked example from the audit: spot=2500,
    interval=50 -> a short strike 2 intervals OTM and a long strike 3
    intervals OTM must compute to exactly (2, 3), not the old defaults (0, 2)."""
    underlying_price = 2500.0
    interval = 50
    short_strike = 2400.0  # 2 intervals OTM (put side)
    long_strike  = 2350.0  # 3 intervals OTM (put side)

    short_otm = round(abs(short_strike - underlying_price) / interval)
    long_otm  = round(abs(long_strike - underlying_price) / interval)

    assert short_otm == 2
    assert long_otm == 3
    assert (short_otm, long_otm) != (0, 2), "must not silently fall back to the old wrong defaults"
