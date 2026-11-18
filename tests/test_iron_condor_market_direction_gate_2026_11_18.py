"""
iron_condor_v1 market-direction entry gate (2026-11-18, data-driven, part of
the pre-2027-01-02-go-live strategy fine-tuning pass).

44% of iron_condor_v1's 27 lifetime closed trades (12) exited via "Regime
shift: TRENDING post-entry", and PUT-wing failures lost 6.4x more per trade
than CALL-wing failures (avg -Rs12,402 vs -Rs1,925). Strike selection itself
is confirmed symmetric (same delta targets/crowded-strike handling for both
wings) -- this isn't a code asymmetry, it's condors being sold while the
broader market already had a bearish lean that RANGE_BOUND's ATR%/EMA-spread%
classification alone doesn't see. New gate: skip the entry when
MarketRegimeDetector's already-cached market_direction is BEARISH, or
unavailable (fails closed, matching every other explicitly-chosen
entry-blocking filter in this file).

Source-inspection style, same convention already established for
_process_iron_condor() by test_gate_consolidation.py/
test_spread_condor_rejection_detail_2026_10_01.py -- too deep a precondition
chain to drive end-to-end with a mock.
"""
import inspect

from src.live_trading.live_trading_engine import LiveTradingEngine


def _block(src: str, anchor: str, window: int = 400) -> bool:
    idx = src.index(anchor)
    return "return" in src[idx: idx + window]


def test_bearish_market_direction_blocks_entry():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert _block(src, '_market_direction_ic == "BEARISH"', window=600), (
        "a BEARISH market_direction must block iron_condor_v1 entry"
    )


def test_unavailable_market_direction_fails_closed():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert _block(src, "_market_direction_ic is None", window=500), (
        "missing market_direction must fail closed (block entry), matching "
        "this file's established convention for every other explicitly-"
        "chosen entry-blocking filter (lot size, contract resolution, "
        "margin, VIX/IV rank)"
    )


def test_gate_reads_cached_market_direction_not_a_fresh_computation():
    # Must reuse MarketRegimeDetector.get_cached_market_direction() (same
    # cheap Redis read _maybe_record_paused_signal_outcome() already uses),
    # not recompute or call detect() again mid-function.
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert "get_cached_market_direction()" in src


def test_audit_gate_checkpoint_added_and_distinct_from_pcr_direction_gate():
    # "market_direction_passed" (this gate) must be distinct from the
    # existing PCR-based "direction_passed" gate -- they ask different
    # questions (market-wide direction vs this stock's own options
    # positioning) and must show up as separate funnel steps in
    # /analytics/gate-audit, not silently merged/double-counted.
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert 'self._audit_gate(strategy.name, "market_direction_passed")' in src
    assert 'self._audit_gate(strategy.name, "direction_passed")' in src
    idx_market = src.index('"market_direction_passed")')
    idx_pcr = src.index('"direction_passed")')
    assert idx_market < idx_pcr, (
        "market-wide direction should gate before the stock-specific PCR "
        "check (cheaper, higher-level question first)"
    )


def test_bullish_or_neutral_market_direction_does_not_block():
    # Guard against over-fixing: only BEARISH blocks. A BULLISH or NEUTRAL
    # market_direction must fall through to the audit_gate checkpoint, not
    # get swept up by an overly broad condition.
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("_regime_detector_ic = getattr(self, \"regime_detector\"")
    end = src.index('self._audit_gate(strategy.name, "market_direction_passed")')
    block = src[idx:end]
    # Exactly two returns in this block: the None (unavailable) case and the
    # BEARISH case -- nothing else should short-circuit before the audit gate.
    assert block.count("return") == 2, (
        "expected exactly 2 blocking returns (unavailable, BEARISH) between "
        "the market_direction read and its audit_gate checkpoint"
    )


def test_credit_spread_v1_unaffected():
    # credit_spread_v1 is DIRECTIONAL (picks BULL_PUT/BEAR_CALL to align
    # with the trend), so a market-wide neutrality filter doesn't apply to
    # it -- this gate must only exist in _process_iron_condor.
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    assert "market_direction_passed" not in src
    assert '"MARKET_DIRECTION_BEARISH"' not in src
