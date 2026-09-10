"""
Investigated 2026-09-10 at the user's request after noticing momentum_v1
trades in the journal showing regime_label=RANGE_BOUND despite momentum_v1
only ever being eligible to trade in TRENDING regime.

Root cause: regime_label was never set at entry (_log_trade_open()) -- it
was fabricated at EXIT time in _log_trade_close(), using a private, ad-hoc
classifier based purely on that ONE underlying's own ATR% at exit
(>=2.5%->VOLATILE, >=1.2%->TRENDING, else->RANGE_BOUND) -- completely
unrelated to the real, market-wide regime (VIX + market-wide ATR% + EMA
spread) that actually gates which strategies can trade. Every trade in the
system's history has had a regime_label that looks authoritative but wasn't.

Fix: the real regime (already computed once per cycle in run_signal_cycle()
and threaded through _process_signal() as `regime`) is now passed through
to _process_credit_spread()/_process_iron_condor() and persisted via
_log_trade_open() at entry, immutably -- _log_trade_close() no longer
touches it at all.

Per test_real_contract_resolution.py's documented rationale, these engine
methods have too deep a precondition chain to drive end-to-end with a mock
-- source-level regression guards instead, same convention already used
for _process_signal/_process_credit_spread/_process_iron_condor elsewhere.
"""
import inspect

from src.live_trading.live_trading_engine import LiveTradingEngine


def test_process_signal_threads_regime_into_credit_spread_and_iron_condor():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert 'await self._process_credit_spread(strategy, symbol, signal_str, market_data, vix=vix, regime=regime)' in src
    assert 'await self._process_iron_condor(strategy, symbol, market_data, vix=vix, regime=regime)' in src


def test_process_signal_passes_regime_to_its_own_log_trade_open():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    idx = src.index("journal_id = await self._log_trade_open(")
    block = src[idx:idx + 400]
    assert "regime=regime" in block


def test_process_credit_spread_accepts_and_forwards_regime():
    sig = inspect.signature(LiveTradingEngine._process_credit_spread)
    assert "regime" in sig.parameters
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("journal_id = await self._log_trade_open(")
    block = src[idx:idx + 400]
    assert "regime=regime" in block


def test_process_iron_condor_accepts_and_forwards_regime():
    sig = inspect.signature(LiveTradingEngine._process_iron_condor)
    assert "regime" in sig.parameters
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("journal_id  = await self._log_trade_open(")
    block = src[idx:idx + 400]
    assert "regime=regime" in block


def test_log_trade_open_persists_regime_as_regime_label():
    sig = inspect.signature(LiveTradingEngine._log_trade_open)
    assert "regime" in sig.parameters
    src = inspect.getsource(LiveTradingEngine._log_trade_open)
    idx = src.index('"regime_atr_pct"')
    block = src[idx:idx + 600]
    assert '"regime_label":   regime,' in block


def test_log_trade_close_no_longer_fabricates_or_overwrites_regime_label():
    src = inspect.getsource(LiveTradingEngine._log_trade_close)
    assert '"regime_label"' not in src, (
        "exit must never touch regime_label -- it's set once, immutably, at entry"
    )
    # The removed ad-hoc per-stock ATR%-based classifier must be gone too.
    assert 'regime = "VOLATILE"' not in src
    assert 'regime = "RANGE_BOUND"' not in src
