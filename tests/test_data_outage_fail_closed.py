"""
Realistic pre-live scenario: what happens to entry gating if the market-data
pipeline itself goes dark (Redis outage, LTPPoller crash, a symbol simply
missing from the tick cache) -- not just "not enough bars yet" (already
covered in test_ltp_poller.py's rvol_valid/adx_valid tests), but a
completely empty market_data dict, the shape _get_market_data() effectively
degrades to on a hard failure.

Every entry gate that depends on ltp_poller-computed fields must treat
"we have no data at all" as "can't confirm, don't enter" -- the same
fail-closed principle behind the 2026-08-06 RVOL/ADX validity fix, exercised
here against the more severe (total outage) case rather than the partial
(insufficient history) case.
"""
import inspect
from src.live_trading.live_trading_engine import LiveTradingEngine


def test_process_signal_returns_early_on_empty_market_data():
    # _get_market_data() returning None/{} (Redis down, symbol not yet
    # polled) must short-circuit before ever reaching a strategy's
    # generate_signal() -- confirms the very first line of defense against
    # a data outage is in place, independent of the RVOL/ADX gates further
    # down (which only run once a signal has already fired).
    src = inspect.getsource(LiveTradingEngine._process_signal)
    market_data_idx = src.index("market_data = await self._get_market_data(symbol)")
    guard_idx = src.index("if not market_data:")
    assert market_data_idx < guard_idx < src.index("strategy.generate_signal(market_data)")


def test_rvol_gate_blocks_on_totally_missing_data_not_just_insufficient_history():
    # market_data.get("rvol_valid", False) -- the explicit `False` default
    # means a market_data dict with NO rvol_valid key at all (e.g. built
    # from a degraded/partial tick during an outage) fails closed, same as
    # a dict that legitimately computed rvol_valid=False.
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert 'market_data.get("rvol_valid", False)' in src


def test_adx_gates_block_on_totally_missing_data_across_all_three_strategies():
    for method_name in ("_process_signal", "_process_credit_spread", "_process_iron_condor"):
        method = getattr(LiveTradingEngine, method_name)
        src = inspect.getsource(method)
        assert 'market_data.get("adx_valid", False)' in src, (
            f"{method_name} must fail closed (default False) when adx_valid is missing entirely, "
            "not just when it's present and False"
        )
