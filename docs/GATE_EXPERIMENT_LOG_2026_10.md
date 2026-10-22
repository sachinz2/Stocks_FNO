# Gate-Loosening Experiment Log (2026-10-08 → ongoing)

**Why this document exists:** starting 2026-10-08, every entry/signal gate
across all 4 strategies was deliberately loosened, on explicit user
instruction, to force trade volume across `ema_crossover_v1`, `momentum_v1`,
`credit_spread_v1`, and `iron_condor_v1` so there's enough real data to judge
each strategy on its own merits. Plan (user's own words, 2026-10-09): *"we
will do this for 1 more week, then we will start tightening the gates, based
on our observations and learnings from these trades."*

**⚠️ Update, same day:** the daily-loss circuit breaker has since been
**fully deactivated** (not just resized) through **2026-10-23** — see item
13. That's a bigger, separate risk step than the rest of this log; the
1-week plan above still stands for everything else.

This is the single source of truth for **every original value**, so
tightening back up later is a checklist, not an archaeology project. Each row
links to the commit that made the change — `git show <hash>` for the full
reasoning.

**Mode:** paper trading throughout (`TRADING_MODE=paper`). No real money at risk.

**Still untouched, on purpose** (see "What was deliberately NOT loosened" below)
— don't forget these exist when reviewing losses.

---

## Quick revert checklist

When it's time to tighten, each row below is independent — revert any subset,
in any order. Nothing here depends on anything else in this list. For each:
1. Edit the file back to the "Original" value.
2. Check whether a test was changed to match the loosened value (search the
   commit's diff for `tests/`) — revert that too, or the suite will fail for
   the opposite reason.
3. Run `python -m pytest tests/` and `python scripts/verify_invariants.py --repo .`
4. Deploy per the usual `git push` → server `git pull && docker compose restart api` flow.

The **expiry-snapping fix** (`cefdf9a`) is the one exception — that's a real
bug fix, not a loosened gate. Keep it regardless of what else gets reverted.

---

## 1. Regime gate (which strategies can run when)

| | |
|---|---|
| **File** | `src/market_data/regime_detector.py` — `REGIME_STRATEGY_MAP` |
| **Original** | `TRENDING: [EMA, SPREAD, MOMENTUM]`, `RANGE_BOUND: [CONDOR, SPREAD]`, `VOLATILE: [SPREAD, EMA]`, `LOW_VOL: []` |
| **Loosened to** | All 4 strategies listed under all 4 regimes |
| **Commit** | `be759a7` |
| **Revert note** | Original mapping is preserved verbatim as a commented-out `_DISABLED_ORIGINAL_MAP_FOR_REFERENCE_ONLY` dict right below the active one — copy it back in. |
| **Tests touched** | `test_regime_detector.py` — 3 design-decision tests `@pytest.mark.skip`'d (not deleted; un-skip after revert), 1 test (`test_regime_switching_still_pauses_a_regime_ineligible_strategy`) rewritten to monkeypatch its own reduced map so the pause *mechanism* stayed covered independent of this change — that rewrite is fine to leave permanently. |

## 2. VIX / IV-Rank thresholds (credit_spread_v1, iron_condor_v1 entry gate)

| | |
|---|---|
| **File** | `src/market_data/option_chain.py` — `vix_allows_selling()`, `iv_rank_allows_selling()` |
| **Original** | `vix >= 12.0`, `iv_rank >= 0.30` |
| **Loosened to** | `vix >= 0.0`, `iv_rank >= 0.0` |
| **Commit** | `be759a7` |
| **Also touched** | `src/risk/risk_manager.py` — defense-in-depth duplicate checks in `validate_trade()`, same 12.0→0.0 and 0.30→0.0 |
| **Tests touched** | `test_code_review_2026_08_28.py` (both threshold tests) |

## 3. Credit spread / iron condor ADX bands

| | | |
|---|---|---|
| **Strategy** | **Original** | **Final loosened value** |
| `credit_spread_v1` | 15–30 | 0–70 (two steps: `be759a7`→5-45, `8c3df8b`→0-70) |
| `iron_condor_v1` ceiling | 20 | 70 (two steps: `be759a7`→40, `8c3df8b`→70) |

- **File:** `src/live_trading/live_trading_engine.py`, `_process_credit_spread`/`_process_iron_condor`
- **Tests touched:** `test_gate_consolidation.py` (both ADX tests, updated each step)

## 4. Event calendar window (earnings/RBI blackout)

| | |
|---|---|
| **File** | `src/live_trading/live_trading_engine.py` — both `_process_credit_spread` and `_process_iron_condor` |
| **Original** | `days=5` |
| **Loosened to** | `days=0` (two steps: `be759a7`→1, `8c3df8b`→0) |
| **Note** | Even at `days=0`, a literal same-day event still blocks — this was never fully disabled, just shrunk. |

## 5. Credit spread / iron condor candidate-pool pre-filter (the "nothing ever enters the pool" bug... that turned out to be a gate, not a bug)

| | |
|---|---|
| **File** | `src/market_data/ltp_poller.py` — `_LOW_VOL_THRESHOLD`, `_FLAT_EMA_THRESHOLD` |
| **Original** | `1.2`, `0.1` |
| **Loosened to** | `5.0`, `1.0` |
| **Commit** | `9a97090` |
| **Also touched** | `src/api/main.py` — `credit_spread_v1`'s `low_vol_threshold` 1.2→5.0, `flat_threshold` **added explicitly** (was silently defaulting to 0.1) and set to **0.02** (lowered — credit_spread skips when *too flat*, opposite direction from iron_condor). `iron_condor_v1`'s `low_vol_threshold` 1.2→5.0, `flat_threshold` 0.1→**1.0** (raised — iron_condor skips when *too directional*). |
| **Important nuance** | `credit_spread_v1` and `iron_condor_v1` normally share one `flat_threshold` value as a deliberate market-partition boundary (see `credit_spread.py`'s own comment on this). That partition is **intentionally broken** right now — they now have different values (0.02 vs 1.0) so both can pull from overlapping candidates. When reverting, both should go back to the **same** shared value (0.1), not just back to "whatever each one says now." |
| **This was the single biggest unlock** — confirmed live: before this, `credit_spread_v1`/`iron_condor_v1`'s candidate pools were `EMPTY` every cycle, all day, both days — none of the other loosened gates downstream could matter if the pool never handed them a candidate at all. |

## 6. Credit spread / iron condor liquidity (bid-ask spread) gate

| | |
|---|---|
| **File** | `src/live_trading/live_trading_engine.py` — `_OPTION_MAX_SPREAD_PCT` (class constant, shared by ALL 4 strategies) |
| **Original** | `8.0` |
| **Loosened to** | `150.0` (three steps: `be759a7`→40.0, `8a4ebd8`→150.0) |
| **⚠️ This is the one flagged as highest-risk.** This is the exact mechanism behind the two confirmed catastrophic single-leg losses this session already root-caused and fixed once (KAYNES, IDEA — see commits `33fc3e8`/`e20059b` from before this experiment). Loosening it this far is a deliberate, explicit tradeoff the user made after being told this directly — **expect illiquid-fill losses to reproduce** while this is loosened. This is probably the single highest-priority item to revert first when tightening starts. |
| **Tests touched** | `test_multi_leg_liquidity_check_2026_10_01.py` (both tests), `test_trade_quality_layer_2026_09_16.py` (one test) — fixture `spread_pct` values bumped above whatever the current threshold was at each step. |

## 7. Credit spread / iron condor wing-credit risk/reward floor

| | |
|---|---|
| **File** | `src/live_trading/live_trading_engine.py` — `MIN_CREDIT_PCT_OF_WING` (credit_spread), `MIN_WING_CREDIT_PCT` (iron_condor, both wings) |
| **Original** | `0.20` (both) |
| **Loosened to** | `0.10` (both) — **deliberately a smaller cut** than everything else in this list; user explicitly asked to "loosen it a little bit," not aggressively. |
| **Commit** | `0def856` |

## 8. ema_crossover_v1 entry thresholds

| | | |
|---|---|---|
| **Parameter** | **Original** | **Loosened to** |
| `adx_entry_threshold` | 22 | 8 |
| `entry_min_gap_pct` | 0.001 | 0.0001 |

- **File:** `src/api/main.py`, `EMA_CROSSOVER` strategy config dict
- **Commit:** `be759a7`
- **Note:** exit-side thresholds (`ema_reversal_min_gap_pct`, `ema_reversal_confirm_bars`, `underlying_stop_atr_mult`, etc.) were deliberately **left alone** — only entry-side loosened, so exits still reflect realistic behavior for loss analysis.
- **Tests touched:** `test_ema_crossover_v1_redesign_2026_08_21.py`, `test_trade_review_fixes_2026_08_27.py` (2 tests)

## 9. momentum_v1 entry thresholds

| | | |
|---|---|---|
| **Parameter** | **Original** | **Loosened to** |
| `adx_entry_threshold` | 25 | 8 |
| `adx_exit_threshold` | 22 | 8 |
| `adx_rising_required` | `True` | `False` |
| `ema_slope_required` | `True` | `False` |
| `extension_atr_mult` | 2.5 | 6.0 |
| `vwap_extension_pct` | 2.5 | 6.0 |
| `rvol_entry_threshold` | 1.5 | 0.3 |
| `min_ema_spread_pct` | 0.30 | 0.05 |

- **File:** `src/api/main.py`, `MOMENTUM` strategy config dict
- **Commit:** `be759a7`
- **Tests touched:** `test_momentum_v1_redesign_2026_08_20.py`, `test_momentum_v1_redesign_round2_2026_08_21.py` (window-size only, no value change needed)

### 9b. Bonus bug found while loosening momentum_v1 (keep this fix regardless of the revert)

`momentum.py` never actually read `rvol_hard_gate`/`require_rs` from its
`parameters` dict — unlike `ema_crossover_v1`, which does. This meant
`momentum_v1` was silently running with **both as hard gates** (engine's
`getattr(..., True)` default) this entire time, a real asymmetry with
`ema_crossover_v1` that nobody had set on purpose.

- **File:** `src/strategies/momentum.py` — added `self.rvol_hard_gate = self.parameters.get("rvol_hard_gate", False)` and `self.require_rs = self.parameters.get("require_rs", False)` to `initialize()`.
- **Commit:** `be759a7`
- **Revert decision needed:** when tightening back up, decide explicitly whether `momentum_v1` *should* have these as hard gates (restore `True` defaults, or remove the lines entirely to go back to the old no-attribute behavior) or whether matching `ema_crossover_v1`'s convention (`False`, configurable) is actually the better permanent design. This is a real design question, not just an experiment value to snap back.
- **Tests touched:** `test_ema_crossover_v1_redesign_2026_08_21.py`, `test_third_review_2026_08_21.py` — both had a "guard against over-fixing" test asserting the OLD no-attribute behavior; both rewritten to assert the new explicit behavior instead of just bumping a number.

## 10. Shared intraday position concurrency cap

| | |
|---|---|
| **File** | `src/live_trading/live_trading_engine.py` — `_max_concurrent_intraday` default |
| **Original** | `2` |
| **Loosened to** | `6` |
| **Commit** | `028d018` |
| **Found live, not anticipated in advance:** this cap is **shared** across `ema_crossover_v1` and `momentum_v1` (both write into `_single_leg_journals`). `ema_crossover_v1` filled both slots almost immediately, which meant `momentum_v1` could never place a trade even after its own signal confirmed cleanly — not blocked by any signal-quality gate, just capacity. |

## 11. Per-strategy capital budget

| | |
|---|---|
| **File** | `src/risk/risk_manager.py` — `validate_trade()`, "5. Per-strategy capital allocation" |
| **Original** | `budget = self.initial_capital * STRATEGY_CAPITAL_ALLOCATION[strategy_name]` (a % of the capital-period-compounded base) |
| **Loosened to** | Flat `Rs 10,00,000` per strategy, regardless of `initial_capital` |
| **Commit** | `09bfd5f` |
| **Found live, not anticipated in advance:** `initial_capital` isn't the flat 3L constant — it's continuously updated by capital-period compounding. Cumulative paper losses had already compounded it down to ~Rs2,23,000, shrinking `momentum_v1`'s 20% share to just Rs44,681 and blocking every one of its confirmed signals that cycle — with **zero** `momentum_v1` positions even open at the time. The %-of-capital model couples "room to trade" to "how much was already lost," which is backwards for a data-gathering experiment. |
| **Revert note:** `STRATEGY_CAPITAL_ALLOCATION` itself (`src/core/constants.py` — 0.30/0.35/0.15/0.20 for the 4 strategies) was never touched; only the formula that *uses* it in `risk_manager.py` was swapped out. Revert by restoring the original formula line (kept as a comment in the diff). |
| **Tests touched:** `test_risk_manager.py` (2 tests) — fixture budgets changed from the percentage formula to the flat 10L figure. |
| **Reset (2026-10-15)** | Explicit user instruction: *"reset the available funds... there should be 1 lakh to trade each strategy."* Flat budget brought down from Rs10,00,000 to **Rs1,00,000** per strategy — still flat across all 4 strategies, not a revert to the original % formula. `_strategy_deployed` itself is pure in-memory state (cleared on `RiskManager.__init__`), so a normal `docker compose restart api` zeroes every strategy's deployed figure back to 0 against the new Rs1,00,000 ceiling — no DB/Redis state to touch. |
| **Tests touched (reset):** `test_risk_manager.py` (same 2 tests) — literal budget bumped from `1_000_000.0` to `100_000.0`. |

## 12. Daily-loss circuit breaker — threshold resize (SUPERSEDED, see item 13)

| | |
|---|---|
| **File** | `src/core/config.py` — `MAX_DAILY_LOSS_PCT`, **plus the server's `.env` file**, which had its own separate hardcoded `MAX_DAILY_LOSS_PCT=0.05` override that would have silently kept the old limit if left alone |
| **Original** | `0.05` (5% of capital) |
| **Loosened to, then reverted** | `0.20` (20%) in `a6be7e7`, same day — then **reverted back to `0.05`** in the same commit as item 13's full bypass, once that made the resize moot. Kept at the conservative default so the check resumes correctly once the item-13 bypass expires. |
| **Status: superseded by item 13 below, same day.** The kill switch trapped at least 2 trading days short with the 20% version still in place, and the user asked for something bigger. |

## 13. Daily-loss circuit breaker — fully deactivated for 2 weeks (supersedes item 12)

| | |
|---|---|
| **File** | `src/risk/risk_manager.py` — `validate_trade()`, "2. Daily loss limit" section |
| **Original behavior** | Checks `total_daily_pnl <= -(initial_capital * max_daily_loss_pct)` on every entry; trips the kill switch if breached. |
| **Changed to** | The entire check is skipped — no daily-loss auto-trip at all — **while `now_ist().date() < date(2026, 10, 23)`**. On or after that date it resumes automatically, using the value in item 12 (`0.05`, the original). |
| **Explicit user instruction:** *"i asked you to deactivate the kill switch for 2 weeks."* (Note: this is a bigger step than what was first asked for on 2026-10-09 — "change it... we will do this for 1 more week" — and item 12's 20% resize is what was actually built from that. When this discrepancy was flagged back, the user explicitly confirmed "fully deactivate for 2 weeks" over the smaller alternative offered.) |
| **⚠️ Flagged plainly before implementing:** unlike every other item in this log, there is now **no daily-loss circuit breaker of any kind** during this window. A bad day — or a bug — has nothing automatic stopping it short of 2026-10-23. This is a materially bigger risk step than anything else in this document. Still paper money throughout. |
| **What did NOT change — this is the one safety net still standing:** the **manual** kill switch (the `kill_switch_active`/`circuit_breaker_active` flags, checked as layer 1, *before* the now-bypassed layer 2) and its `/admin/kill-switch` activate/deactivate API are completely untouched. If something looks wrong during these 2 weeks, flipping the kill switch on manually still immediately blocks all new entries — it just won't happen *automatically* from daily P&L anymore. |
| **Self-expiring by design:** the bypass is a hard-coded date check (`_DAILY_LOSS_AUTO_TRIP_DISABLED_UNTIL = date(2026, 10, 23)`), not an indefinite flag — it cannot silently stay disabled past the 2 weeks even if nobody remembers to revert it manually. A `verify_invariants.py` check (`check_daily_loss_bypass_has_an_expiry_and_does_not_touch_the_manual_kill_switch`) guards both that this date exists and that the manual kill switch stays independent of it. |
| **Tests touched:** `test_risk_manager.py` (2 tests), `test_defense_in_depth_2026_08_20.py` (1 test) — each now monkeypatches `now_ist()` to a date after 2026-10-23 so the underlying trip logic is still verified as correct, rather than the tests silently passing (or failing) for the wrong reason. |
| **To revert early** (before 2026-10-23, if needed): change the date comparison to always take the "else" branch, or just set `_DAILY_LOSS_AUTO_TRIP_DISABLED_UNTIL` to today. **To revert permanently** at or after the 2-week mark: delete the bypass's `if` entirely (the original unconditional check is preserved in the diff), and decide fresh what `MAX_DAILY_LOSS_PCT` should be given everything learned in the interim — don't just assume 0.05 is still right. |

---

**⚠️ Priority shift, 2026-10-09 (end of day 2):** after seeing the *corrected*
P&L for the first 2 days (see "Historical pnl correction" section below —
the dashboard had been showing fake profits), the user clarified: *"we can
reduce the trades for ema_crossover, but we need trades in iron_condor,
iron_condor & credit_spread are my main strategies."* `ema_crossover_v1`'s
real performance was a 33% win rate and a large net loss over 29 trades
across the two days, and its high volume was claiming underlyings via the
cross-strategy collision guard (item 16), crowding out
`credit_spread_v1`/`iron_condor_v1` candidates. Items 14-16 below implement
this: one gate loosened further for `credit_spread_v1`/`iron_condor_v1`
specifically, two gates partially (not fully) tightened back for
`ema_crossover_v1` only (`momentum_v1` untouched — the user named
`ema_crossover_v1` specifically).

## 14. Resolved-strike delta-accuracy tolerance (credit_spread_v1 + iron_condor_v1)

| | |
|---|---|
| **File** | `src/live_trading/live_trading_engine.py` — `_resolved_strike_delta_ok()`'s `delta_tol` default |
| **Original** | `0.08` |
| **Loosened to** | `0.15` |
| **What it does**: after `_resolve_contract()` snaps a computed strike to the nearest actually-listed one, this re-verifies the FINAL strike's real Black-Scholes delta hasn't drifted too far from the original target (e.g. -0.20 for a put short). Shared by both `credit_spread_v1` (1 call site) and `iron_condor_v1` (2 call sites, one per short leg) — neither passes `delta_tol` explicitly, so both use this default. |
| **Found live, same day:** this was the very next gate `iron_condor_v1` hit (GAIL) immediately after clearing every other gate in its chain (pool, ADX, event calendar, liquidity, wing-credit, expiry resolution) for the first time. |
| **Tests touched:** none needed updating — the two existing delta-tolerance tests (`test_second_opinion_review_2026_08_21.py`) use strikes far enough outside tolerance that widening 0.08→0.15 doesn't flip either result. |

## 15. ema_crossover_v1 ADX entry threshold — partially re-tightened, twice

| | |
|---|---|
| **File** | `src/api/main.py`, `EMA_CROSSOVER` config dict |
| **Original** | `22` → loosened to `8` (`be759a7`, item 8) → re-tightened to `16` (2026-10-09, "reduce ema_crossover_v1 trades") → **nudged to `20`** (2026-10-15, data-driven) |
| **Why not back to 22:** the 2026-10-15 trade review (104 trades) found ADX≥22 wasn't clearly better than the 16-22 band (36.8% vs 39.3% win rate) -- a full revert isn't supported by the data. The <16 trades (already excluded by the 10-09 change) were clearly the worst (21.1% win rate), so nudging closer to 22 trims more of that tail without the unsupported full jump. |
| **Tests touched:** `test_ema_crossover_v1_redesign_2026_08_21.py`, `test_trade_review_fixes_2026_08_27.py` (2 tests) — literal value bumps, window-size increases. |

## 16. ema_crossover_v1 entry min-gap — partially re-tightened

| | |
|---|---|
| **File** | `src/api/main.py`, `EMA_CROSSOVER` config dict |
| **Original** | `0.001` → loosened to `0.0001` (`be759a7`, item 8) → **partially re-tightened to `0.0005`** |
| **Scope reminder:** entry side only — `ema_reversal_min_gap_pct` (exit side) was never touched by item 8 or this change, so exits still reflect realistic behavior. |
| **Tests touched:** `test_trade_review_fixes_2026_08_27.py` (1 test) — literal value and window-size bump. |

## 17. ema_crossover_v1 — new hour=13 (1-2pm IST) entry exclusion

| | |
|---|---|
| **File** | `src/live_trading/live_trading_engine.py`, `_process_signal()` -- new check, right after the VOLATILE-regime BUY-skip, before the daily-order-limit check |
| **What it does** | Blocks new `ema_crossover_v1` entries specifically during IST hour 13 (1:00-2:00pm). `momentum_v1` and all other strategies are unaffected -- scoped by `strategy.name == "ema_crossover_v1"`. |
| **Evidence (2026-10-15, 104-trade review, Oct 8-15)** | Hour=13 entries accounted for ₹47,208 of the total ₹112,140 loss (42%) at a 15% win rate vs 39-42% every other hour -- survives removing the single biggest outlier trade (IDEA, -₹20,013). **Caveat on record**: concentrated in 2 of 5 sampled days (Oct 9, Oct 14 = 88% of the hour-13 damage; Oct 13 was fine) rather than uniformly bad every day, and the entire 104-trade sample is RANGE_BOUND-regime-only -- re-validate if/when a TRENDING/VOLATILE stretch provides fresh data. |
| **This is a new rule, not a loosened-then-retightened experiment value** -- there's no "original" to revert to. If it turns out not to hold up, remove the `if` block entirely (kept as a single, clearly-delimited block for easy removal). |
| **Tests touched** | 6 existing tests that drive the real `_process_signal()` with `strategy.name="ema_crossover_v1"` started failing/would-have-failed depending on the real wall-clock hour at test-run time (confirmed live: the full suite ran at 13:xx IST and exactly these 6 failed). Fixed by pinning `now_ist()` to a safe hour via `monkeypatch` in each (`test_live_trading_engine.py` x2, `test_rich_gate_rejection_detail_2026_09_16.py` x5 via a new autouse fixture). **This is a real, pre-existing class of risk** -- any test that drives `_process_signal()` with an unmocked clock and `strategy.name="ema_crossover_v1"` is now time-dependent; 15 test files reference that name but only these were confirmed to actually reach this code path. |
| **`verify_invariants.py`** | `check_ema_crossover_hour13_exclusion_is_scoped_correctly` added -- confirms the exclusion exists, actually blocks (not just logs), and main.py's `adx_entry_threshold` matches the 2026-10-15 value. |

## 19. ema_crossover_v1 — ADX entry gate gets a ceiling (floor -> band)

| | |
|---|---|
| **File** | `src/strategies/ema_crossover.py` (`initialize()` + `generate_signal()`), `src/api/main.py` (`EMA_CROSSOVER` config dict: `"adx_entry_ceiling": 25`) |
| **What it does** | `adx_entry_ceiling=25`, checked BEFORE the existing floor/rising logic: `adx >= 25` always blocks (`adx_ok = False`), unconditionally -- even if ADX is rising. Only `adx < 25` reaches the existing `adx >= adx_entry_threshold (20) OR ADX rising` check. Net effect: entries now require `20 <= ADX < 25`, or a still-rising ADX below 25. |
| **Evidence (2026-10-19, 148-trade review, Oct 8-19)** | ADX/entry relationship is non-monotonic, not a simple "higher is better": `<16`=30.8% win, `16-20`=39.1%, **`20-25`=43.8% (best)**, `25-30`=28.9%, `30-35`=21.1% (worst), `35+`=40.0% (n=10, too thin to act on). The 25-35 "valley" has ~2x the avg MAE of the 20-25 band (-14.49%/-11.52% vs -9.26%) and less upside (avg MFE 6.07%/3.98% vs 8.18%) -- buying into an already-extended move prone to a sharp reversal, not catching a fresh one. Cross-checked against a separate dig into "fresh opposite signal" reversal-exit losses (9 trades since the Oct-15 MFE/MAE fix, 0% win, -₹4,374 avg -- the single worst bucket found this session): **8 of 9 already had entry ADX>=25** -- this ceiling would have blocked them before the reversal-exit mechanism ever got involved. |
| **Deliberately NOT done:** no attempt to also exempt the 35+ recovery -- only 10 trades, too thin to build a non-contiguous band around yet. Revisit once more data accumulates. |
| **Expected effect:** cuts `ema_crossover_v1` volume hard -- only ~32 of the last 148 trades would have cleared `20<=ADX<25`. Consistent with the user's standing instruction that this strategy's volume can shrink (`"we can reduce the trades for ema_crossover... iron_condor & credit_spread are my main strategies"`, item 14). |
| **Tests touched:** `tests/test_trade_review_fixes_2026_08_27.py` (2 existing tests bumped `adx14` fixture 25.0->22.0 to stay below the new ceiling since they test gap logic, not ADX; 5 new tests added for the ceiling itself + its main.py wiring), `tests/test_third_review_2026_08_21.py` (1 window-size bump, 3000->3500). |

## 20. momentum_v1 — hour=13 entry exclusion extended from ema_crossover_v1

| | |
|---|---|
| **File** | `src/live_trading/live_trading_engine.py`, `_process_signal()` -- the existing 2026-10-15 ema_crossover_v1 hour=13 check's condition widened from `strategy.name == "ema_crossover_v1"` to `strategy.name in ("ema_crossover_v1", "momentum_v1")`. |
| **Evidence (2026-10-19, 34-trade review, Oct 8-19)** | Independent analysis found the identical pattern already fixed for ema_crossover_v1: 11/34 momentum_v1 trades (a third of all volume) fall in hour=13, at a 9.1% win rate, accounting for ₹24,078 of the strategy's total ₹46,353 loss (52%). Spans 5 different days (Oct 8, 9, 15, 16, 19) and 8 different symbols; removing the single worst trade still leaves -₹15,870 at 10% win rate -- not an outlier artifact. Same RANGE_BOUND-only caveat as the ema_crossover_v1 version applies (100% of the sample is RANGE_BOUND). |
| **Also observed, not acted on:** 33 of 34 momentum_v1 trades have RVOL<0.5 at entry, and that bucket is clearly the worst-performing; the single trade with RVOL 0.5-1.0 was the best trade in the sample (+₹1,648, 41% MFE). Suggestive but n=1 for the "good" bucket -- nowhere near enough to set a threshold on. Revisit once more data accumulates. |
| **Tests touched:** `verify_invariants.py`'s `check_ema_crossover_hour13_exclusion_is_scoped_correctly` updated to check for both strategy names. `test_rich_gate_rejection_detail_2026_09_16.py`: 3 new tests (hour=13 blocks each strategy independently; hour=10 blocks neither). |

## 21. momentum_v1 — new "stall exit" (not a gate, a new exit mechanism)

| | |
|---|---|
| **File** | `src/strategies/momentum.py` (`initialize()` + `manage_position()`, new `stall_exit_minutes` param, default 15), `src/live_trading/live_trading_engine.py` (`_process_signal()` captures `entry_timestamp` at journal creation; `_check_open_option_exits()` computes `minutes_since_entry` and threads it + the already-tracked `mfe_option_pct` into `manage_position()`). |
| **What it does** | Exits a position early if its premium has never traded above entry (`mfe_option_pct <= 0`) for at least `stall_exit_minutes`. Checked ahead of every other exit condition, including the underlying-based stop -- the point is to cut a position showing zero favorable movement before it reaches a worse, later stop level. |
| **Evidence (2026-10-21, 43-trade review, Oct 8-21)** | Split by whether `option_mfe_pct` was exactly 0.0 at exit: zero-MFE trades were **0% win rate, -₹2,810 avg (20 trades, -₹56,207 total)**; nonzero-MFE trades (showed *any* favorable move, however brief) were roughly breakeven (**30.4% win, +₹62 avg, 23 trades, +₹1,417 total**). Removing the zero-MFE group alone would make the strategy net positive over the window. Confirmed this isn't an instant/same-cycle-exit artifact -- zero-MFE trades held 15-80 minutes, plenty of monitoring cycles to register a favorable tick if one ever came. Checked ADX/RVOL/VIX/hour-of-day as potential entry-time predictors of which group a trade falls into -- all nearly identical between the two groups (RVOL was actually *higher*, backwards from expectation), so **this cannot be fixed with a better entry gate**; it's genuinely only visible after entry. |
| **This is a different kind of change than everything else in this log** -- not a threshold tightened/loosened on an existing gate, but a brand-new, timed exit condition. `stall_exit_minutes=15` is a reasonable starting point (shorter than nearly every zero-MFE loss's actual hold time), **not derived from precise time-to-first-favorable-move data** -- `trade_journal` only has the final MFE/MAE at exit, not a time series, so there was no way to find the "optimal" cutoff precisely. Revisit based on observed results. |
| **Scope:** `momentum_v1` only -- `ema_crossover_v1`'s zero-MFE pattern was never analyzed; do not assume this generalizes without checking separately. |
| **Tests touched:** `test_momentum_v1_redesign_round2_2026_08_21.py` (8 new tests: fires at/after threshold, not before, not if ever favorable, skips gracefully when fields missing, checked before the underlying-based stop, configurable, disableable, default value), `test_trade_review_fixes_2026_08_27.py` (1 window-size bump, 4200->4600). |

---

## 18. iron_condor_v1 push — three further changes, 2026-10-16

User: *"we want trades in iron_condor... what more can we do?"* Diagnosed
live from ~1hr of rejection logs (09:54-10:53 IST, 2026-10-16): every
candidate was failing at one of four points. Three were acted on; one was
investigated and found NOT to be a bug.

| | |
|---|---|
| **a) PaperBroker capital top-up** | **File:** `src/core/config.py`, `INITIAL_CAPITAL` `300,000 -> 600,000`. **Why:** the dominant rejection reason this morning was "insufficient margin" (COALINDIA ~Rs40,500, PIDILITIND ~Rs40-60K, SBIN ~Rs45-60K, TITAN/LT ~Rs52,500, INDUSINDBK ~Rs56,000) — this is `_check_available_margin()`'s paper-mode check against `broker.balance` (a SHARED pool across all 4 strategies), not the per-strategy soft budget touched in item 11/the 2026-10-15 reset. `broker.balance` had shrunk to ~Rs1,07,515 from cumulative realized paper losses. `_reconcile_paper_broker_balance()` recomputes it as `INITIAL_CAPITAL + total_realized - deployed_cash` on every restart, so this takes effect on next restart without wiping any trade history. **Deploy gotcha hit live:** the server's `.env` had `INITIAL_CAPITAL=300000.0` pinned, which pydantic Settings reads ahead of the class default — the code change alone did nothing until `.env` was updated to match. Then `docker compose restart api` *still* showed the stale value, because `restart` only restarts the existing container's process without re-reading `.env`; needed `docker compose up -d api` (which reports "Recreate"/"Recreated") to actually pick up the new env var. Confirmed fixed: `broker.balance` reconciled to ₹315,045.71 after the recreate, vs ₹15,045.71 before. |
| **a-2) Second top-up, same day: `_check_available_margin()` wasn't the real bottleneck after all** | Within ~2hrs, iron_condor_v1 candidates (LT, ADANIPORTS, ONGC) started clearing every signal-quality gate AND the entry-level margin gate above, then failing at actual order placement: `PaperBroker.place_order()` has its OWN, independent, much stricter margin model for SELL-to-open legs — `SHORT_MARGIN_MULTIPLE_OF_PREMIUM = 10.0` (10x the premium per short lot, not wing-width-based; see `paper_broker.py`). The 3 existing credit_spread_v1 short legs (HDFCBANK, POWERGRID, WIPRO) alone locked ~Rs2,98,930 under this model, leaving only ~Rs16-28K free against candidates needing Rs55,800 (ONGC) to Rs2,33,130 (ADANIPORTS). Presented the choice (top up further vs. loosen `SHORT_MARGIN_MULTIPLE_OF_PREMIUM` itself, which would reduce modeled short-selling risk for every short leg, not just unblock capital) — user chose to top up again. `INITIAL_CAPITAL` `600,000 -> 1,500,000`. Same `.env` + `docker compose up -d` deploy steps required again. |
| **b) iron_condor_v1 wing-credit floor** | **File:** `src/live_trading/live_trading_engine.py`, `_process_iron_condor()`. `MIN_WING_CREDIT_PCT` `0.10 -> 0.07`, **iron_condor_v1 only** — `credit_spread_v1`'s matching `MIN_CREDIT_PCT_OF_WING` left at `0.10` (31.9 rolling PF, already working, no case to loosen its own risk/reward floor). ONGC/TITAN/DRREDDY were repeatedly missing the 0.10 floor by small amounts as the market ticked (e.g. Rs0.21-0.30 vs Rs0.50 needed on ONGC). |
| **c) Option bid-ask spread cap** | **File:** `src/live_trading/live_trading_engine.py`, `_OPTION_MAX_SPREAD_PCT` `150.0 -> 200.0`. Clears LICI (observed 193.22%) and PIDILITIND (152-159%), both repeat-rejected. **Side effect flagged:** this constant is shared across ALL 4 strategies (single-leg entries too) — `ema_crossover_v1`/`momentum_v1`'s own liquidity gate loosens by the same amount, even though neither was asked for and both are currently the worse-performing strategies. |
| **d) Delta-tolerance-after-cache-resolution — investigated, not changed** | IDFCFIRSTB/BEL were failing `_resolved_strike_delta_ok()` (0.15 tolerance). Dug into `_resolve_contract()`/`_resolved_strike_delta_ok()`: this is NOT a stale-cache bug — `bs_delta()` is recomputed fresh from the current `underlying_price`/`dte`/`sigma` each call. The "after contract-cache resolution" wording refers to `get_real_contract()` snapping the candidate strike to a different, actually-listed strike (by design); what's being checked is whether *that* real strike's real delta is still within tolerance of the original target. Working as designed, already widened once (0.08->0.15 on 2026-10-09). Left untouched — widening further changes the condor's actual risk/reward profile, not just signal volume; flagged back to user rather than auto-changed. |
| **Tests touched:** `test_multi_leg_liquidity_check_2026_10_01.py` (2 tests), `test_trade_quality_layer_2026_09_16.py` (1 test) — literal `spread_pct` fixtures bumped above 200.0. |

---

## Historical pnl correction (2026-10-09) — unrelated bug, not a loosened gate

Separately from this experiment: the user noticed several `ema_crossover_v1`
"EOD square-off" trades showed large profits despite exit price being
*lower* than entry price. Root cause: `PaperBroker.get_positions()` returns
LIVE references into its internal position dicts, and the closing order
zeroes out a fully-closed position's `avg_price`/`quantity` the instant it
fills. `_square_off_all()`/`_exit_all_options_for()` were both re-reading
those fields from the position dict *after* placing the closing order,
silently picking up the post-close zero instead of the real pre-close
value. Fixed in `a33431e` (see that commit for the full writeup) — this is
a correctness bug, unrelated to any gate, and stays fixed regardless of
what else in this log gets reverted.

6 historical `trade_journal` rows (ids 204, 224, 225, 229, 231, 235) had
already been written with the wrong `pnl` before the fix landed — all 6
were corrected via direct `UPDATE` to their real `(exit-entry)*qty` value
on 2026-10-09, on the user's explicit request. The real 2-day P&L is **far
worse** than what was shown live: Oct 8 -₹11,524.50, Oct 9 -₹51,590.00
(both strategies, both days net negative) — not the roughly-breakeven
picture the corrupted data suggested at the time.

## Reversal-exit MFE/MAE logging bug (2026-10-15) — unrelated bug, not a loosened gate

Found during the same multi-pass `ema_crossover_v1` trade review that produced
items 15 and 17 above: all 15 reversal-exit trades in the Oct 8-15 sample
showed exactly `0.0` for `underlying_mfe_pct`/`mae_pct`/`option_mfe_pct`/
`mae_pct`, regardless of real hold time (9-305 minutes) or symbol --
impossible for real market data, confirming a logging bug rather than a real
pattern. Root cause: `_close_option_positions()` (the "Reversal exit" closer)
never read these 4 fields off the single-leg journal dict and passed them to
`_log_trade_close()`, unlike every *other* exit path
(`_execute_single_leg_exit`, EOD square-off, EXIT-signal close), which all do.
`_log_trade_close()` leaves its entry-time-seeded `0.0` default untouched
when the caller passes `None` -- so this silently produced fake-zero data for
that entire 15-trade/0%-win bucket. Fixed (same commit as items 15/17's
deployment) by reading `jrnl.get("mfe_underlying_pct")` etc., identical to
the other 3 exit paths. 1 new regression test, verified to fail without the
fix and pass with it. This is a correctness/observability bug, unrelated to
any gate, and stays fixed regardless of what else in this log gets reverted.
No historical row correction was done for this one (unlike the pnl bug
above) -- the 15 affected rows just keep their 0.0 MFE/MAE values as a
known gap in that historical data; only trades closed by this path *after*
the fix will have real values.

---

## What was deliberately NOT loosened (and why — don't touch these without a fresh decision)

| Gate | File | Why it's different from everything above |
|---|---|---|
| Real-quote requirement before any LIMIT entry (fail closed on a missing quote, no ATR-estimate fallback) | `live_trading_engine.py`, `option_chain.py` | **Data integrity, not risk tolerance.** Loosening this would fabricate prices instead of just tolerating worse real ones — it would make the resulting losses *uninterpretable* (you can't learn "is this strategy good in this regime" from a trade based on a guessed price). |
| Margin sufficiency check | `_check_available_margin()` | Affordability constraint, not a tunable threshold — this is what keeps the paper broker's own accounting from going negative/corrupting every downstream P&L figure. |
| Duplicate-resting-order guard | `place_order()` | Correctness (prevents double-fills corrupting position tracking), not a risk-tolerance gate. |
| Kill switch *mechanism* itself, daily reset, sector concentration cap | `risk_manager.py` | Portfolio-level circuit breakers / diversification rules — only the daily-loss *threshold* (item 12) was resized, nothing else in this category was touched. |

---

## Real bug fixed during this experiment (not a loosened gate — keep regardless)

**`get_real_contract()` now snaps an unlisted expiry to the nearest real listed one, within a 5-day tolerance** (`src/market_data/option_chain.py`), commit `cefdf9a`.

Root cause: `_last_expiry_weekday()`'s hand-rolled "last Tuesday of the month"
formula computed `2026-11-24` for November's roll-forward expiry, but NSE's
actual listed expiry that month is `2026-11-23` — one day earlier, almost
certainly a holiday missing from the hardcoded fallback list (the live
server actually has `exchange_calendars` installed and loads a dynamic
calendar via XBSE, so this was likely a local-dev-only gap, but the fix
protects against **any** future drift between our date arithmetic and NSE's
real calendar, not just this specific date). Every `credit_spread_v1`/
`iron_condor_v1` roll-forward entry was failing closed on "no verified real
contract" for literally every candidate until this was fixed — this is what
unblocked contract resolution after item 5 got the pool loosened in the
first place.

This mirrors the exact fail-closed-but-smart pattern `get_real_contract()`
already used for an unlisted *strike* (snap to nearest real one) — now also
applied to the *expiry*. 3 new tests + a `verify_invariants.py` check added.

---

## Timeline of what each change actually unblocked (for post-mortem context)

1. **`be759a7`** — loosened everything at once (regime, VIX/IV-rank, ADX, event calendar, liquidity, ema_crossover/momentum entry params). Result: `ema_crossover_v1` started trading immediately. `credit_spread_v1`/`iron_condor_v1` pools stayed `EMPTY` — none of this mattered yet.
2. **`028d018`** — found `momentum_v1` was signal-confirming but blocked by the shared concurrency cap (item 10). Fixed. `momentum_v1` started trading.
3. **`09bfd5f`** — found `momentum_v1` separately blocked by its own compounding-shrunk capital budget (item 11). Fixed.
4. **`9a97090`** — found the REAL reason `credit_spread_v1`/`iron_condor_v1` had empty pools: the pool pre-filter (item 5), upstream of everything else. Fixed — pools went from `EMPTY` every cycle to fully populated.
5. **`8c3df8b`** — pools populated, but every top candidate still hit the ADX ceiling or 1-day event window. Loosened both further (items 3, 4).
6. **`cefdf9a`** — candidates now clearing ADX/event-calendar, but every single one failed contract resolution on a 1-day-wrong expiry (the real bug, see above). Fixed.
7. **`8a4ebd8`** — contract resolution fixed, but every candidate was a now-correctly-resolved far-month contract with genuinely thin liquidity (44%-113% spreads). Raised the liquidity ceiling (item 6) with explicit risk flagged to the user before doing it.
8. **`0def856`** — liquidity cleared, next candidates failed the wing-credit risk/reward floor. Loosened a little (item 7), per explicit "just a little" instruction.
9. **First `credit_spread_v1` trade placed** (HDFCBANK credit spread) shortly after `0def856`.
10. **`a6be7e7`** — by this point the daily-loss kill switch had already tripped twice across the two days, each time cutting the day short before `iron_condor_v1` got a real chance. Raised for a bounded 1-week window (item 12).

As of this writing: `ema_crossover_v1`, `momentum_v1`, and `credit_spread_v1`
have all placed at least one trade since the experiment began. `iron_condor_v1`
has not yet — not blocked by any specific gate at last check, just hasn't had
its own candidate come up yet in the pool rotation.

---

## 22. iron_condor_v1 — observations only, 2026-10-22 (NO code changed, explicit user instruction)

User: *"which gate(s) we can tighten in iron_condor? please analyze."* Full
history is only **5 trades ever** (ITC, COALINDIA, TORNTPHARM, SUNPHARMA,
ASIANPAINT) — far too thin for the 148/43-trade statistical approach used
for `ema_crossover_v1`/`momentum_v1`. Logged as observations, deliberately
**not acted on** per explicit instruction ("don't change anything yet").

### What it's NOT — checked every loosened entry gate against all 5 trades, none explain the losses

| Trade | ADX | VIX | IV Rank | Credit/max-loss | P&L |
|---|---|---|---|---|---|
| ITC | 16.4 | 14.64 | 0.513 | 26.3% | -₹690 |
| COALINDIA | 11.4 | 14.48 | 0.715 | 36.1% | -₹1,647 |
| TORNTPHARM | 22.7 | 14.70 | 0.683 | 29.5% | +₹3,953 |
| SUNPHARMA | 39.3 | 14.99 | 0.703 | 25.3% | -₹1,078 |
| ASIANPAINT | 17.7 | 15.17 | 0.915 | 24.2% | -₹1,530 |

VIX (14.5-15.2) and IV rank (0.51-0.92) comfortably clear even the
**original, pre-experiment** floors (12.0 and 0.30) on every single trade —
tightening either back up would not have blocked any of these 5. Same for
the wing-credit floor (credit/max-loss 24-36%, nowhere near even the
original 20%, let alone the current 7%). ADX is the one soft exception —
only SUNPHARMA (39.3) is notably high relative to a tighter ceiling, and
it's a single data point.

### What it IS (1): the REGIME_STRATEGY_MAP loosening directly caused one loss

**ITC entered while `regime_label` was already `TRENDING`** — confirmed
directly in its trade record. `regime_detector.py`'s current
`REGIME_STRATEGY_MAP["TRENDING"]` includes `STRATEGY_CONDOR` (loosened
2026-10-08, part of the original all-at-once loosening, item 1). The
preserved original map (`_DISABLED_ORIGINAL_MAP_FOR_REFERENCE_ONLY`) lists
`iron_condor_v1` under `RANGE_BOUND` *only* — a market-neutral,
range-betting structure was never supposed to be eligible to enter during a
classified TRENDING regime in the first place. This is the single most
concrete, directly-verifiable finding of this entire dig: not a statistical
pattern inferred from a small sample, but one specific trade whose entry
regime is on record as contradicting the strategy's fundamental thesis,
traceable to one specific, already-identified loosened setting.

### What it IS (2): the "close whole condor on first wing's 75% profit" exit, and next-day regime-shift exits, both point the same direction

3 of 5 trades (ITC, COALINDIA, TORNTPHARM) closed via: *"if one wing's
short premium decays to 25% of entry (75%+ profit), close the entire
condor — holding exposes the now-more-threatened OTHER wing to breach"*
(`_check_condor_exits()`, around line 5867). Mechanically sound risk logic
(the stock moved toward the triggering wing's strike means it moved *away*
from the other wing, so the untriggered wing's own exposure is credibly
larger now) -- but 2 of 3 times (ITC, COALINDIA) it still closed at a net
loss, meaning the threatened wing's cost outpaced the safe wing's gain.
Couldn't compute a reliable counterfactual ("would holding longer have been
worse") without intraday tick data this analysis didn't pull — flagged, not
concluded to be wrong.

The other 2 trades (SUNPHARMA, ASIANPAINT) entered during `RANGE_BOUND` but
were force-exited the very next session by `_check_condor_exits()`'s
regime-shift check (`regime_detector` had already reclassified to
`TRENDING` by the next day). 2 of 5 (40%) hitting this in such a short
window, on top of ITC's direct TRENDING-entry case above, is a consistent
story: **across this entire 5-trade sample, the underlying market has
behaved more directionally/trending than range-bound**, and a pure
range-betting strategy has been fighting that the whole stretch. This may
be as much "the current market character is wrong for this strategy right
now" as "a specific gate is wrong" — can't distinguish the two with this
little data.

### Candidate for reconsideration (not applied)

Restoring `iron_condor_v1` to `RANGE_BOUND`-only in `REGIME_STRATEGY_MAP`
(reverting just this one strategy's entry in the loosened map, independent
of `credit_spread_v1`/`ema_crossover_v1`/`momentum_v1`'s own entries there)
is the most concretely-justified single change surfaced by this dig — it
would have blocked exactly the one trade with directly-verifiable evidence
against it (ITC), without touching VIX/IV-rank/wing-credit/ADX, none of
which were shown to be the actual constraint. **Not applied** — logged for
the next tightening pass.

## 23. ema_crossover_v1 / momentum_v1 — same-day re-entry block wired up

| | |
|---|---|
| **File** | `src/live_trading/live_trading_engine.py`: `_process_signal()` checks `_exited_today` right after the existing cross-strategy collision guard (entry side); `_execute_single_leg_exit()` adds the underlying to `_exited_today` on an adverse close, `pnl < 0` (exit side). |
| **What it does** | `_exited_today` is not new -- it already existed ("adverse exits today -- blocks same-day re-entry") and was already checked by `_process_credit_spread()`/`_process_iron_condor()` before a new entry, but had never been wired up for the single-leg strategies (`ema_crossover_v1`/`momentum_v1`) that share `_process_signal()`. This connects the two existing halves of a mechanism that already worked for 2 of 4 strategies. |
| **Evidence (2026-10-22, user: "i believe we can look into momentum strategy...")** | Quick same-symbol re-entries (<=2hrs after that symbol's own prior exit) perform meaningfully worse than each strategy's own baseline: `momentum_v1` **0% win rate on 6 instances** (-₹12,050 combined; strategy's overall win rate 14.8%), `ema_crossover_v1` **25.0% win rate on 20 instances** (-₹26,843 combined; overall 34.3%). `KALYANKJIL` alone was entered and stopped out 3 times in one hour on 2026-10-22 (10:55, 11:20, 11:55), each a fresh loss right after the last. Not a threshold tuned from a statistical pattern -- a documented, working guard that was simply never connected to half the strategies that could use it. |
| **Routing convention** | By realized `pnl` sign, matching `_check_spread_exits()`/`_check_condor_exits()`'s own "route to profit or adverse bucket... by realized P&L, not by scanning exit_reason text" comment -- not a string match on `exit_reason`. |
| **Tests touched:** `test_rich_gate_rejection_detail_2026_09_16.py`'s `_FakeEngine` (2 new tests: blocked when the symbol is in `_exited_today`, not blocked for a different symbol), `test_live_trading_engine.py`'s `_FakeEngine` (added missing `_exited_today` attribute; 2 new tests: adverse exit adds the underlying, profitable exit does not). |
