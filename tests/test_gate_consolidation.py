"""
credit_spread_v1 / iron_condor_v1 entry-gate consolidation (2026-08-07).

Both strategies had ~5 independent gates all re-asking the same underlying
question from different angles -- e.g. credit_spread_v1 required its own
EMA-based signal AND VWAP alignment AND PCR alignment AND market breadth
alignment AND ADX-in-range to all independently agree before entering.
Requiring N correlated confirmations to each pass independently shrinks the
joint pass rate multiplicatively even on genuinely good setups -- found live:
credit_spread_v1 had 0 trades in 2.5+ weeks, iron_condor_v1 had 0 in 3+ weeks,
despite the underlying market regime supporting entries on many of those days.

Consolidated down to: base signal + ADX (a genuinely distinct trend-strength
dimension) + ONE external direction/neutrality confirmation (VWAP for
credit_spread since it's tied to this stock's actual intraday price action;
PCR for iron_condor since it's stock-specific vs breadth's market-wide
average). PCR/breadth/IV-HV-ratio are now logged for visibility but no
longer block entries.

These tests use source inspection (matching the style of
test_data_outage_fail_closed.py) rather than full end-to-end mocking --
_process_credit_spread/_process_iron_condor are large methods with heavy
external dependencies (order_manager, redis, live option quotes), and what
matters here is structural: does a given check still `return` (block) or not.
"""
import inspect
from src.live_trading.live_trading_engine import LiveTradingEngine


def _block(src: str, anchor: str, window: int = 400) -> bool:
    """True if a `return` appears within `window` chars after `anchor`."""
    idx = src.index(anchor)
    return "return" in src[idx: idx + window]


def test_credit_spread_pcr_no_longer_blocks():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    assert not _block(src, "pcr_allows_spread(oi_data.get(\"pcr\")"), (
        "PCR opposing spread direction must no longer be a blocking gate "
        "(consolidated into VWAP as the sole direction-confirmation)"
    )
    assert "no longer blocking" in src


def test_credit_spread_market_breadth_no_longer_blocks():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("_breadth_cs is not None")
    # Scan the whole breadth-handling block (both BEAR_CALL/BULL_PUT arms) up
    # to the next gate (ADX filter) for any `return`.
    end = src.index("# ADX filter", idx)
    assert "return" not in src[idx:end], (
        "market breadth opposing spread direction must no longer block entry"
    )


def test_credit_spread_iv_hv_ratio_no_longer_blocks():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("_iv_hv_ratio = _market_iv / _atr_sigma")
    end = src.index("# Fee viability check", idx)
    assert "return" not in src[idx:end], (
        "IV/HV ratio being too low must no longer block entry -- VIX + IV "
        "Rank already gate on premium richness"
    )


def test_credit_spread_vwap_still_blocks():
    # VWAP is the one direction-confirmation kept as a blocking gate.
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    assert _block(src, "below VWAP Rs{vwap:.2f}"), "VWAP must remain a blocking gate"
    assert _block(src, "above VWAP Rs{vwap:.2f}"), "VWAP must remain a blocking gate"


def test_credit_spread_adx_still_blocks():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    assert _block(src, "ADX={_adx_cs:.1f} < 15"), "ADX range gate must remain blocking"
    assert _block(src, "ADX={_adx_cs:.1f} > 30"), "ADX range gate must remain blocking"


def test_iron_condor_pcr_still_blocks():
    # PCR is the one neutrality-confirmation kept as a blocking gate for iron_condor.
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert _block(src, "PCR={pcr:.2f} is extreme"), "PCR neutrality gate must remain blocking"


def test_iron_condor_market_breadth_no_longer_blocks():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("_breadth_ic is not None")
    end = idx + 400
    assert "return" not in src[idx:end], (
        "market breadth outside the neutral zone must no longer block iron_condor entry"
    )


def test_iron_condor_adx_still_blocks():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert _block(src, "ADX={_adx_ic:.1f} >= 20"), "ADX gate must remain blocking for iron_condor"


def test_oi_data_still_fetched_for_crowded_strike_check():
    # Dropping PCR as a blocking gate must not remove the oi_data fetch itself --
    # it's also used by the crowded-strike avoidance logic further down.
    for method_name in ("_process_credit_spread", "_process_iron_condor"):
        src = inspect.getsource(getattr(LiveTradingEngine, method_name))
        assert "oi_data = await get_oi_data(symbol, redis, kite=self._kite)" in src
        assert "is_strike_crowded(" in src
