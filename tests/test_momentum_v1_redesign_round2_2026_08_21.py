"""
momentum_v1 entry-quality redesign, round 2 (2026-08-21) -- the user asked
for a precise accounting of what from the external PDF review was still
unimplemented after round 1 (2026-08-20, see
test_momentum_v1_redesign_2026_08_20.py), and to implement it. Covers:
  - Pullback+breakout EVENT-based confirmation model (sections 8/9/13),
    now the default (use_pullback_continuation_model=True).
  - Two-tier RVOL breakout confirmation (section 10).
  - Underlying-based stop/target as the PRIMARY exit driver (sections 20-21).
  - momentum_v1 removed from VOLATILE (section 15) -- see
    test_regime_detector.py::test_volatile_regime_includes_ema_crash_catching_excludes_momentum
    for that specific coverage.
See momentum.py's class docstring for the full round-2 change list and what
remains deliberately unimplemented (DTE A/B testing, fixed-window MFE/MAE
snapshots).
"""
from src.strategies.momentum import MomentumStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine


def _mom(**overrides):
    strat = MomentumStrategy("momentum_v1", overrides)
    strat.initialize()
    return strat


def _bar(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx=30.0, close=110.0,
         atr=2.0, vwap=110.0, rvol=1.5, rvol_valid=True, bar_key="live:t0"):
    # Fixed 2026-09-04: the pullback+breakout state machine reads
    # rvol_closed_bar/rvol_closed_bar_valid (the last FINALIZED bar's RVOL),
    # not the plain rvol/rvol_valid fields (the still-forming current bar's
    # volume-so-far) -- see momentum.py's matching fix note. Both are set
    # here from the same rvol/rvol_valid kwargs so these tests still express
    # "this bar's RVOL reading" without every call site needing a rename.
    return {
        "symbol": symbol, "ema20": ema20, "ema50": ema50, "adx14": adx,
        "close": close, "atr14": atr, "vwap": vwap, "rvol": rvol,
        "rvol_valid": rvol_valid,
        "rvol_closed_bar": rvol, "rvol_closed_bar_valid": rvol_valid,
        "ohlc_bar_key": bar_key,
    }


# ── Pullback + breakout event model (default on) ────────────────────────────

def test_pullback_continuation_model_is_the_new_default():
    strat = _mom()
    assert strat.use_pullback_continuation_model is True


def test_never_pulling_back_never_fires_even_though_the_gate_qualifies_every_bar():
    """Guard against the old debounce semantics leaking back in -- a trend
    that just keeps extending (never dips) never produces a genuine
    pullback+breakout EVENT, so it must never fire under the new model,
    unlike the old 'same condition for N bars' debounce."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    signals = []
    close = 110.0
    for i in range(6):
        close += 1.0  # strictly extending every bar -- never a pullback
        signals.append(strat.generate_signal(_bar(adx=30.0, close=close, bar_key=f"live:t{i}")))
    assert all(s == "HOLD" for s in signals)


def test_pullback_then_breakout_fires_with_rvol_confirmation():
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    # Bar 0: qualifies -- ESTABLISHED, ref=110.
    strat.generate_signal(_bar(close=110.0, rvol=1.6, bar_key="live:t0"))
    # Bar 1: still extends -- ref raised to 112.
    strat.generate_signal(_bar(close=112.0, rvol=1.6, bar_key="live:t1"))
    # Bar 2: pulls back (close < ref=112) -- PULLBACK begins, ref locked at 112.
    signal = strat.generate_signal(_bar(close=111.0, rvol=1.0, bar_key="live:t2"))
    assert signal == "HOLD"
    assert strat._trend_state["RELIANCE"] == "PULLBACK"
    # Bar 3: breaks back above ref=112, flat RVOL (>= rvol_entry_threshold=1.5) -- fires.
    signal = strat.generate_signal(_bar(close=113.0, rvol=1.6, bar_key="live:t3"))
    assert signal == "BUY"
    # State fully reset after firing.
    assert "RELIANCE" not in strat._trend_state


def test_breakout_without_rvol_confirmation_stays_in_pullback_not_discarded():
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, rvol=1.6, bar_key="live:t0"))
    strat.generate_signal(_bar(close=111.0, rvol=1.0, bar_key="live:t1"))  # pullback begins, ref=110->?
    # ref was raised to 110 at bar0 only (bar0 doesn't extend past itself) --
    # use an explicit extend bar first for clarity:
    strat2 = _mom(adx_rising_required=False, ema_slope_required=False,
                  extension_atr_mult=0, vwap_extension_pct=0)
    strat2.generate_signal(_bar(close=110.0, rvol=1.6, bar_key="live:t0"))
    strat2.generate_signal(_bar(close=112.0, rvol=1.6, bar_key="live:t1"))  # extends, ref=112
    strat2.generate_signal(_bar(close=111.0, rvol=1.0, bar_key="live:t2"))  # pullback, ref=112
    # Breaks ref=112 but RVOL=0.5 is below both breakout_rvol_min (1.3, no
    # contraction seen yet since 1.0 > pullback_rvol_low=0.8) and
    # rvol_entry_threshold (1.5) -- rejected, but setup is NOT discarded.
    signal = strat2.generate_signal(_bar(close=113.0, rvol=0.5, bar_key="live:t3"))
    assert signal == "HOLD"
    assert strat2._trend_state.get("RELIANCE") == "PULLBACK"
    # A later bar with strong RVOL now breaks through and fires.
    signal = strat2.generate_signal(_bar(close=114.0, rvol=1.6, bar_key="live:t4"))
    assert signal == "BUY"


def test_two_tier_rvol_fires_at_breakout_rvol_min_after_a_genuine_contraction():
    """Section 10: a genuine volume contraction during the pullback
    (RVOL < pullback_rvol_low) followed by expansion on the breakout bar
    should fire at breakout_rvol_min (1.3), even though that's BELOW the
    flat rvol_entry_threshold (1.5) that would otherwise be required."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, rvol=1.6, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, rvol=1.6, bar_key="live:t1"))  # extends, ref=112
    # Pullback bar with a genuine contraction (RVOL=0.5 < pullback_rvol_low=0.8).
    strat.generate_signal(_bar(close=111.0, rvol=0.5, bar_key="live:t2"))
    # Breakout at RVOL=1.4 -- below rvol_entry_threshold(1.5) but above
    # breakout_rvol_min(1.3), and a contraction was observed -- fires.
    signal = strat.generate_signal(_bar(close=113.0, rvol=1.4, bar_key="live:t3"))
    assert signal == "BUY"


def test_invalid_rvol_sentinel_does_not_register_as_a_contraction():
    """Fixed 2026-08-21 (deep review): ltp_poller emits rvol=0.0 paired with
    rvol_valid=False as an 'insufficient volume history' sentinel for
    roughly the first 100 minutes of every session -- this must NOT be
    treated as a genuine volume contraction (had_contraction=True). A
    pullback that only ever saw rvol=0.0/rvol_valid=False bars must still
    require the flat rvol_entry_threshold (not the lower post-contraction
    breakout_rvol_min) to fire."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, rvol=0.0, rvol_valid=False, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, rvol=0.0, rvol_valid=False, bar_key="live:t1"))  # extends, ref=112
    # Pullback bar with the sentinel -- rvol=0.0 would look like a genuine
    # contraction (< pullback_rvol_low=0.8) if rvol_valid weren't checked.
    strat.generate_signal(_bar(close=111.0, rvol=0.0, rvol_valid=False, bar_key="live:t2"))
    assert strat._rvol_history.get("RELIANCE") == []
    # Breakout at RVOL=1.4 -- above breakout_rvol_min(1.3) but below the flat
    # rvol_entry_threshold(1.5). Must be REJECTED since no genuine
    # contraction was ever observed (the sentinel doesn't count).
    signal = strat.generate_signal(_bar(close=113.0, rvol=1.4, bar_key="live:t3"))
    assert signal == "HOLD"
    assert strat._trend_state.get("RELIANCE") == "PULLBACK"
    # A breakout clearing the full flat threshold still fires normally.
    signal = strat.generate_signal(_bar(close=114.0, rvol=1.6, bar_key="live:t4"))
    assert signal == "BUY"


def test_invalid_rvol_on_the_breakout_bar_itself_does_not_satisfy_confirmation():
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, rvol=1.6, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, rvol=1.6, bar_key="live:t1"))  # extends, ref=112
    strat.generate_signal(_bar(close=111.0, rvol=0.5, bar_key="live:t2"))  # genuine contraction
    # Breakout bar itself has an invalid RVOL reading (sentinel) -- even
    # though a real contraction preceded it, this bar's own RVOL is unknown
    # and must not satisfy breakout_rvol_min.
    signal = strat.generate_signal(_bar(close=113.0, rvol=0.0, rvol_valid=False, bar_key="live:t3"))
    assert signal == "HOLD"
    assert strat._trend_state.get("RELIANCE") == "PULLBACK"


def test_breakout_confirmation_uses_closed_bar_rvol_not_the_forming_bar():
    """Fixed 2026-09-04 (live incident): the breakout check only evaluates on
    is_new_bar -- the instant a new 5-min bucket starts and ltp_poller's plain
    `rvol` (the still-forming current bar) resets to a few-seconds-old,
    structurally deflated number. It must read `rvol_closed_bar` (the last
    FINALIZED bar's real, full-bar RVOL) instead. Confirmed live: with plain
    `rvol`, every breakout attempt in 2+ weeks read 0.0-0.96 and momentum_v1's
    pullback model fired zero live trades in that entire window."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, rvol=1.6, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, rvol=1.6, bar_key="live:t1"))  # extends, ref=112
    strat.generate_signal(_bar(close=111.0, rvol=1.0, bar_key="live:t2"))  # pullback
    # Breakout bar: the still-forming bar's plain rvol is near-zero (exactly
    # the live symptom), but the last CLOSED bar's rvol_closed_bar clears the
    # flat threshold -- must fire on the closed-bar reading, not the forming one.
    bar = _bar(close=113.0, bar_key="live:t3")
    bar["rvol"], bar["rvol_valid"] = 0.02, True
    bar["rvol_closed_bar"], bar["rvol_closed_bar_valid"] = 1.6, True
    signal = strat.generate_signal(bar)
    assert signal == "BUY"


def test_breakout_confirmation_rejects_when_only_the_forming_bar_rvol_is_strong():
    """Mirror of the above: a strong plain `rvol` (forming bar) must NOT
    substitute for a weak/invalid rvol_closed_bar -- proves the closed-bar
    field is authoritative, not just an additional passing path."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, rvol=1.6, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, rvol=1.6, bar_key="live:t1"))  # extends, ref=112
    strat.generate_signal(_bar(close=111.0, rvol=1.0, bar_key="live:t2"))  # pullback
    bar = _bar(close=113.0, bar_key="live:t3")
    bar["rvol"], bar["rvol_valid"] = 5.0, True
    bar["rvol_closed_bar"], bar["rvol_closed_bar_valid"] = 0.02, True
    signal = strat.generate_signal(bar)
    assert signal == "HOLD"
    assert strat._trend_state.get("RELIANCE") == "PULLBACK"


def test_same_bar_key_direction_flip_does_not_reset_pullback_progress():
    """Fixed 2026-08-21 (deep review): the fresh-qualification/direction-
    flip reset branch ran unconditionally, before the is_new_bar check
    further down -- so calling generate_signal twice with the SAME bar_key
    but a flipped raw direction could wipe accumulated pullback progress
    and reseed a new state mid-bar. It must now wait for a genuinely new
    bar_key, same as every other transition in this state machine."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, ema20=105.0, ema50=100.0, rvol=1.6, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, ema20=105.0, ema50=100.0, rvol=1.6, bar_key="live:t1"))
    strat.generate_signal(_bar(close=111.0, ema20=105.0, ema50=100.0, rvol=1.0, bar_key="live:t2"))
    assert strat._trend_state.get("RELIANCE") == "PULLBACK"
    ref_before = strat._pullback_ref.get("RELIANCE")
    # Same bar_key as the last processed cycle ("live:t2"), but a flipped
    # direction (EMA20 now below EMA50 -- raw would be SELL). Must be a
    # no-op: state, direction, and ref must all survive untouched.
    signal = strat.generate_signal(_bar(close=111.0, ema20=95.0, ema50=100.0, rvol=1.0, bar_key="live:t2"))
    assert signal == "HOLD"
    assert strat._trend_state.get("RELIANCE") == "PULLBACK"
    assert strat._trend_direction.get("RELIANCE") == "BUY"
    assert strat._pullback_ref.get("RELIANCE") == ref_before
    # A genuinely new bar_key with the flipped direction is still free to
    # reset and start a fresh setup in the other direction.
    signal = strat.generate_signal(_bar(close=111.0, ema20=95.0, ema50=100.0, rvol=1.0, bar_key="live:t3"))
    assert signal == "HOLD"
    assert strat._trend_state.get("RELIANCE") == "ESTABLISHED"
    assert strat._trend_direction.get("RELIANCE") == "SELL"


def test_pullback_setup_expires_after_exactly_max_pullback_bars_without_breakout():
    """Fixed 2026-08-21 (deep review): _pullback_bars is seeded at 1 on
    entering PULLBACK, so `> max_pullback_bars` previously let a setup
    survive max_pullback_bars + 1 bars instead of the documented
    max_pullback_bars. With max_pullback_bars=2, the setup must expire on
    exactly the 2nd pullback bar, not the 3rd."""
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0, max_pullback_bars=2)
    strat.generate_signal(_bar(close=110.0, rvol=1.6, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, rvol=1.6, bar_key="live:t1"))  # extends, ref=112
    strat.generate_signal(_bar(close=111.0, rvol=1.0, bar_key="live:t2"))  # pullback bar 1 -- survives
    assert strat._trend_state.get("RELIANCE") == "PULLBACK"
    signal = strat.generate_signal(_bar(close=110.5, rvol=1.0, bar_key="live:t3"))  # pullback bar 2 -- expires
    assert signal == "HOLD"
    assert "RELIANCE" not in strat._trend_state


def test_quality_gate_dropping_out_resets_the_pullback_setup():
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, adx=30.0, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, adx=30.0, bar_key="live:t1"))  # ESTABLISHED
    assert "RELIANCE" in strat._trend_state
    # ADX drops below adx_entry_threshold (25) -- raw becomes None, resets.
    strat.generate_signal(_bar(close=111.0, adx=20.0, bar_key="live:t2"))
    assert "RELIANCE" not in strat._trend_state


def test_use_pullback_continuation_model_false_falls_back_to_legacy_debounce():
    """Rollback path -- must reproduce the exact pre-2026-08-21 behavior."""
    strat = _mom(use_pullback_continuation_model=False, signal_confirm_bars=1,
                 adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(bar_key="live:t0"))
    signal = strat.generate_signal(_bar(bar_key="live:t1"))
    assert signal == "BUY"


def test_on_pause_clears_pullback_state():
    strat = _mom(adx_rising_required=False, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(close=110.0, bar_key="live:t0"))
    strat.generate_signal(_bar(close=112.0, bar_key="live:t1"))
    strat.generate_signal(_bar(close=111.0, rvol=1.0, bar_key="live:t2"))  # PULLBACK
    assert strat._trend_state.get("RELIANCE") == "PULLBACK"
    strat.on_pause()
    assert strat._trend_state == {}
    assert strat._pullback_ref == {}
    assert strat._rvol_history == {}


# ── Engine: RVOL gate must not double-check when the strategy already did ───

def test_engine_skips_flat_rvol_gate_when_strategy_checked_internally():
    import inspect
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert "rvol_checked_internally" in src


def test_pullback_model_sets_rvol_checked_internally():
    strat = _mom()
    assert strat.rvol_checked_internally is True


def test_legacy_model_does_not_set_rvol_checked_internally():
    strat = _mom(use_pullback_continuation_model=False)
    assert strat.rvol_checked_internally is False


# ── Underlying-based stop/target (primary exit driver) ──────────────────────

def test_underlying_based_stop_fires_for_a_call_before_premium_moves_much():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "is_call": True,
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 97.9,  # 100 - 1.0x ATR(2.0) = 98 -- breached
    }
    # Premium P&L alone (39.5 vs 40, -1.25%) wouldn't trip any premium stop.
    assert strat.manage_position(pos, 39.5) == "EXIT"


def test_underlying_based_target_fires_for_a_put_before_premium_moves_much():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "is_call": False,
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 95.9,  # 100 - 2.0x ATR(2.0) = 96 -- breached (favorable for a PE)
    }
    assert strat.manage_position(pos, 40.5) == "EXIT"


def test_underlying_based_stop_target_do_not_fire_within_range():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "is_call": True,
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 100.5,  # within [98, 104]
    }
    assert strat.manage_position(pos, 40.0) == "HOLD"


def test_underlying_based_stop_target_skip_gracefully_when_entry_context_missing():
    strat = _mom()
    pos = {"avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0}
    assert strat.manage_position(pos, 39.9) == "HOLD"


def test_underlying_based_stop_can_be_disabled_independently_of_target():
    strat = _mom(underlying_stop_atr_mult=0)
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "is_call": True,
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 97.0,  # would have breached the stop
    }
    assert strat.manage_position(pos, 39.9) == "HOLD"


def test_underlying_based_stop_checked_before_premium_hard_stop():
    """Both would fire here -- confirm the underlying-based check is truly
    primary (same EXIT outcome either way, but this pins the priority via
    the log path / short-circuit rather than leaving it untested)."""
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "is_call": True,
        "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 97.0,  # underlying stop breached
    }
    # Premium down 60% -- would also hit the hard stop_loss_pct (50%).
    assert strat.manage_position(pos, 16.0) == "EXIT"


# ── Engine: entry-context capture + threading ────────────────────────────────

def test_engine_captures_entry_underlying_price_and_atr_at_entry():
    import inspect
    src = inspect.getsource(LiveTradingEngine._process_signal)
    idx = src.index('"entry_regime":  regime,')
    block = src[idx:idx + 2000]
    assert '"entry_underlying_price": underlying_price' in block
    # Fixed 2026-09-04 (live incident): entry_atr must be stored ALREADY
    # scaled by _5MIN_ATR_SCALE -- the raw 5-min-bar ATR was being fed
    # directly into momentum_v1's/ema_crossover_v1's underlying-based
    # stop/target with no scaling, landing the stop/target ~0.15-0.3% from
    # entry (ordinary tick noise), firing almost immediately on nearly every
    # entry. See momentum.py/ema_crossover.py's manage_position() -- neither
    # applies the scale factor itself, so it must be applied once here.
    assert '"entry_atr":              atr * _5MIN_ATR_SCALE,' in block


def test_engine_threads_entry_context_into_manage_position_call():
    import inspect
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    assert '"entry_underlying_price": owner_info.get("entry_underlying_price")' in src
    assert '"entry_atr":              owner_info.get("entry_atr")' in src


# ── Production wiring must carry the round-2 params too ─────────────────────
# Same class of bug as round 1's test_main_py_does_not_override_
# adx_entry_threshold_back_to_the_old_value -- main.py's config dict is a
# separate, independent source of truth from momentum.py's class defaults,
# and was caught drifting once already. Assert the round-2 params explicitly
# rather than relying on "no override = class default applies" being
# rediscovered the hard way again.

def test_main_py_wires_up_the_round2_pullback_and_underlying_stop_params():
    import inspect
    from src.api import main as main_module
    src = inspect.getsource(main_module)
    idx = src.index('StrategyRegistry.load_strategy("MOMENTUM"')
    block = src[idx:idx + 2400]
    assert '"use_pullback_continuation_model": True' in block
    assert '"underlying_stop_atr_mult": 1.0' in block
    assert '"underlying_target_atr_mult": 2.0' in block
