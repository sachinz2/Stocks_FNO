"""
ema_crossover_v1 redesign (2026-08-21), from an external PDF review of the
strategy's real result (1 trade in ~2 months, +Rs59) -- the review's central
thesis is the mirror image of the one that drove momentum_v1's redesign the
day before: momentum_v1 was entering too LATE, while ema_crossover_v1 was
filtered so aggressively (ADX>=25 + RVOL>=1.3 hard gates + strict 15m
agreement, stacked on the 2-bar confirmation) that its opportunity set was
almost eliminated. Integrated directly into ema_crossover_v1 (no separate
v2), matching the same choice already made for momentum_v1. See
ema_crossover.py's class docstring for the full change list and what's
deliberately NOT done, and docs/LIVE_TRADING_CHECKLIST.md for the accounting
(including one review claim -- the 15-min MTF fail-open bug -- that was
already outdated by the time this was read, having been fixed the day
before in momentum_v1's own external review).
"""
import inspect

from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.momentum import MomentumStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine


def _ema(**overrides):
    strat = EMACrossoverStrategy("ema_crossover_v1", overrides)
    strat.initialize()
    return strat


def _bar(symbol="RELIANCE", fast=101.0, slow=100.0, adx=20.0, bar_key="live:t0", adx_valid=True):
    # adx_valid=True by default: these tests pass real numeric ADX values
    # meant to represent genuine readings, not ltp_poller's adx14=0.0/
    # adx_valid=False "insufficient history" sentinel -- see
    # test_adx_invalid_sentinel_does_not_register_as_a_rising_data_point
    # (2026-09-04) for that sentinel-specific case.
    return {
        "symbol": symbol, "ema20": fast, "ema50": slow, "adx14": adx,
        "adx_valid": adx_valid, "ohlc_bar_key": bar_key,
    }


# ── Internal ADX gate (>=threshold OR rising) ────────────────────────────────

def test_adx_defaults_to_22_and_checked_internally():
    # Fixed 2026-08-27 (trade review): raised from 18 -- ADX>=18 is inside
    # conventional "no trend" territory, and combined with the other
    # loosened gates let through enough marginal crossovers to produce a
    # 17.9% win rate over 28 trades (Aug 24-26). 22 keeps most of this
    # redesign's "don't require an already-strong trend" intent while
    # screening out the weakest setups.
    strat = _ema()
    assert strat.adx_entry_threshold == 22
    assert strat.adx_checked_internally is True


def test_crossover_fires_when_adx_already_above_threshold():
    strat = _ema(signal_confirm_bars=1)
    # Bar 1: bearish baseline.
    strat.generate_signal(_bar(fast=99.0, slow=100.0, adx=24.0, bar_key="live:t0"))
    # Bar 2: fresh cross, ADX=24 >= 22 -- fires immediately.
    signal = strat.generate_signal(_bar(fast=101.0, slow=100.0, adx=24.0, bar_key="live:t1"))
    assert signal == "BUY"


def test_crossover_confirmed_but_low_adx_holds_pending_not_discarded():
    strat = _ema(signal_confirm_bars=1)
    strat.generate_signal(_bar(fast=99.0, slow=100.0, adx=10.0, bar_key="live:t0"))
    # Fresh cross confirmed (signal_confirm_bars=1), but ADX=10 < 22 and no
    # history yet to check "rising" -- holds, doesn't fire, doesn't discard.
    signal = strat.generate_signal(_bar(fast=101.0, slow=100.0, adx=10.0, bar_key="live:t1"))
    assert signal == "HOLD"
    assert strat._pending_signal.get("RELIANCE") == "BUY"


def test_low_but_rising_adx_still_fires_once_confirmed():
    strat = _ema(signal_confirm_bars=1)
    strat.generate_signal(_bar(fast=99.0, slow=100.0, adx=10.0, bar_key="live:t0"))
    # Cross confirms with ADX=10 (below 22, no history) -- holds.
    signal = strat.generate_signal(_bar(fast=101.0, slow=100.0, adx=10.0, bar_key="live:t1"))
    assert signal == "HOLD"
    # Next bar: ADX rising 10 -> 14 (still < 22) -- now has 2 points of
    # history and is rising, so it fires without needing a fresh crossover.
    signal = strat.generate_signal(_bar(fast=101.0, slow=100.0, adx=14.0, bar_key="live:t2"))
    assert signal == "BUY"


def test_adx_not_rising_and_below_threshold_keeps_holding():
    strat = _ema(signal_confirm_bars=1)
    strat.generate_signal(_bar(fast=99.0, slow=100.0, adx=14.0, bar_key="live:t0"))
    strat.generate_signal(_bar(fast=101.0, slow=100.0, adx=14.0, bar_key="live:t1"))  # holds
    # ADX declines 14 -> 12 -- still below threshold, not rising -- keeps holding.
    signal = strat.generate_signal(_bar(fast=101.0, slow=100.0, adx=12.0, bar_key="live:t2"))
    assert signal == "HOLD"
    assert strat._pending_signal.get("RELIANCE") == "BUY"


def test_missing_adx_does_not_block_firing():
    """Guard against over-fixing -- adx14 absent from market_data must not
    silently block every crossover; the engine's separate adx_valid check
    is the real data-availability gate."""
    strat = _ema(signal_confirm_bars=1)
    bar0 = _bar(fast=99.0, slow=100.0, bar_key="live:t0")
    bar1 = _bar(fast=101.0, slow=100.0, bar_key="live:t1")
    del bar0["adx14"]
    del bar1["adx14"]
    strat.generate_signal(bar0)
    signal = strat.generate_signal(bar1)
    assert signal == "BUY"


def test_on_pause_clears_adx_history():
    strat = _ema()
    strat.generate_signal(_bar(adx=15.0, bar_key="live:t0"))
    assert strat._adx_history.get("RELIANCE")
    strat.on_pause()
    assert strat._adx_history == {}
    assert strat._history_bar_key == {}


# ── EMA-reversal exit generalized + underlying-based stop/target ────────────
# (VOLATILE-scoping removal itself is covered in test_strategies_extended.py;
# these focus on the new underlying-based stop/target addition.)

def test_underlying_based_stop_fires_for_a_call():
    # Fixed 2026-08-27 (trade review): underlying_stop_atr_mult raised from
    # 1.0 to 1.4 -- stop level is now 100 - 1.4xATR(2.0) = 97.2.
    strat = _ema()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0,
        "is_call": True, "current_ema_fast": 105.0, "current_ema_slow": 100.0,  # thesis still intact
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 97.0,  # 100 - 1.4xATR(2.0) = 97.2 -- breached
    }
    assert strat.manage_position(pos, 39.5) == "EXIT"


def test_underlying_based_target_fires_for_a_put():
    strat = _ema()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0,
        "is_call": False, "current_ema_fast": 95.0, "current_ema_slow": 100.0,  # thesis still intact
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 95.9,  # 100 - 2.0xATR(2.0) = 96 -- breached, favorable for a PE
    }
    assert strat.manage_position(pos, 40.5) == "EXIT"


def test_underlying_based_stop_target_skip_gracefully_when_absent():
    strat = _ema()
    pos = {"avg_price": 40.0, "peak_premium": 40.0, "is_call": True,
           "current_ema_fast": 105.0, "current_ema_slow": 100.0}
    assert strat.manage_position(pos, 39.9) == "HOLD"


def test_underlying_based_stop_checked_before_ema_reversal():
    """Fixed 2026-08-21 (deep review): momentum_v1's docstring calls the
    underlying-based stop/target the PRIMARY exit driver, checked first,
    ahead of structural/EMA-reversal invalidation -- but ema_crossover_v1
    had the opposite order (EMA reversal first). Reordered to match. Both
    conditions are true here (EMA has reversed for a CE AND the underlying
    stop is breached); pin that the underlying-based EXIT still fires
    (same outcome either way -- this test exists to guard the ordering via
    a future divergence, e.g. differing log messages or side effects, not
    to catch a different result today)."""
    strat = _ema()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0,
        "is_call": True, "current_ema_fast": 99.0, "current_ema_slow": 100.0,  # reversed for a CE
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 97.0,  # underlying stop breached (100 - 1.0xATR(2.0) = 98)
    }
    assert strat.manage_position(pos, 39.5) == "EXIT"


def test_underlying_based_stop_can_be_disabled():
    strat = _ema(underlying_stop_atr_mult=0)
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0,
        "is_call": True, "current_ema_fast": 105.0, "current_ema_slow": 100.0,
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 97.0,
    }
    assert strat.manage_position(pos, 39.9) == "HOLD"


# ── RVOL soft gate + MTF asymmetric gate (engine-level, static source) ──────

def test_engine_rvol_gate_is_skippable_via_rvol_hard_gate_flag():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    rvol_block = src[src.index("RVOL filter"):src.index("ADX filter")]
    assert "rvol_hard_gate" in rvol_block


def test_engine_mtf_gate_supports_asymmetric_mode():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    mtf_block = src[src.index("Multi-timeframe confirmation"):src.index("lot_size = await self._get_lot_size")]
    assert "mtf_strict" in mtf_block
    assert "mtf_strong_opposition_pct" in mtf_block


def test_engine_adx_gate_is_skippable_via_adx_checked_internally_flag():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert "adx_checked_internally" in src
    # The adx_valid (data-availability) check must remain unconditional --
    # only the flat numeric threshold is skippable.
    assert 'if not _adx_ema_valid' in src


def test_ema_crossover_defaults_to_soft_rvol_and_asymmetric_mtf():
    strat = _ema()
    assert strat.rvol_hard_gate is False
    assert strat.mtf_strict is False
    assert strat.mtf_strong_opposition_pct == 0.3


def test_momentum_v1_keeps_strict_defaults_unaffected():
    """Guard against over-fixing -- momentum_v1 never set rvol_hard_gate/
    mtf_strict, so getattr's own defaults (True) must still apply to it."""
    mom = MomentumStrategy("momentum_v1", {})
    mom.initialize()
    assert getattr(mom, "rvol_hard_gate", True) is True
    assert getattr(mom, "mtf_strict", True) is True
    assert mom.adx_checked_internally is True


# ── EMA candidate pool re-weighting ──────────────────────────────────────────

def test_ema_pool_score_weights_proximity_over_atr():
    from src.market_data.ltp_poller import LTPPoller
    # Two ticks with comparable (not extreme) ATR so proximity is actually
    # the deciding factor, not one stock's ATR simply swamping the other's
    # regardless of weighting: "far" has moderately higher ATR (1.5% vs
    # 1.0%) AND is farther from crossing (0.4% spread vs 0.05%). Under the
    # OLD 0.6xATR/0.4xproximity weighting, far's higher ATR wins (0.94 vs
    # 0.78); under the NEW 0.3xATR/0.7xproximity weighting, near's tighter
    # proximity to crossing must win instead (0.615 vs 0.52) -- confirming
    # the review's point ("it needs stocks actually approaching a
    # crossover," not just stocks with high ATR).
    far  = {"close": 100.0, "atr14": 1.5, "ema20": 100.4,  "ema50": 100.0, "adx14": 10.0}
    near = {"close": 100.0, "atr14": 1.0, "ema20": 100.05, "ema50": 100.0, "adx14": 10.0}
    ema_far, _, _, _ = LTPPoller._score_all(far)
    ema_near, _, _, _ = LTPPoller._score_all(near)
    assert ema_near > ema_far


# ── Signal audit (external review section 20) ────────────────────────────────

class _FakeEngineForAudit:
    _audit_gate = LiveTradingEngine._audit_gate
    get_signal_audit_report = LiveTradingEngine.get_signal_audit_report

    def __init__(self):
        self._signal_gate_stats = {}


def test_audit_gate_increments_per_strategy_per_gate():
    fake = _FakeEngineForAudit()
    fake._audit_gate("ema_crossover_v1", "signal_generated")
    fake._audit_gate("ema_crossover_v1", "signal_generated")
    fake._audit_gate("ema_crossover_v1", "rvol_passed")
    fake._audit_gate("momentum_v1", "signal_generated")

    report = fake.get_signal_audit_report()
    assert report["ema_crossover_v1"]["signal_generated"] == 2
    assert report["ema_crossover_v1"]["rvol_passed"] == 1
    assert report["momentum_v1"]["signal_generated"] == 1


def test_audit_report_is_a_snapshot_not_a_live_reference():
    fake = _FakeEngineForAudit()
    fake._audit_gate("ema_crossover_v1", "signal_generated")
    report = fake.get_signal_audit_report()
    fake._audit_gate("ema_crossover_v1", "signal_generated")
    assert report["ema_crossover_v1"]["signal_generated"] == 1  # snapshot, unaffected by the second call


def test_process_signal_calls_audit_gate_at_the_expected_checkpoints():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    for gate in [
        "signal_generated", "dte_passed", "rvol_passed", "adx_passed",
        "rs_passed", "mtf_passed", "lot_passed", "contract_resolved", "trade_placed",
    ]:
        assert f'"{gate}"' in src, f"missing audit checkpoint: {gate}"


# ── Production wiring must carry the new params ──────────────────────────────

def test_main_py_wires_up_ema_crossover_v1_round2_params():
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    idx = src.index('StrategyRegistry.load_strategy("EMA_CROSSOVER"')
    block = src[idx:idx + 2400]
    # Fixed 2026-08-27 (trade review): adx_entry_threshold raised 18->22,
    # underlying_stop_atr_mult raised 1.0->1.4 -- see ema_crossover.py's
    # matching comments for why.
    assert '"adx_entry_threshold": 22' in block
    assert '"rvol_hard_gate": False' in block
    assert '"mtf_strict": False' in block
    assert '"ema_reversal_exit": True' in block
    assert '"underlying_stop_atr_mult": 1.4' in block
