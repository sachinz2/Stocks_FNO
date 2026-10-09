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

## 15. ema_crossover_v1 ADX entry threshold — partially re-tightened

| | |
|---|---|
| **File** | `src/api/main.py`, `EMA_CROSSOVER` config dict |
| **Original** | `22` → loosened to `8` (`be759a7`, item 8) → **partially re-tightened to `16`** |
| **Why not back to 22:** the user asked to *reduce*, not eliminate, `ema_crossover_v1` activity — still deliberately looser than the pre-experiment baseline. |
| **Tests touched:** `test_ema_crossover_v1_redesign_2026_08_21.py`, `test_trade_review_fixes_2026_08_27.py` (2 tests) — literal value and one window-size bump. |

## 16. ema_crossover_v1 entry min-gap — partially re-tightened

| | |
|---|---|
| **File** | `src/api/main.py`, `EMA_CROSSOVER` config dict |
| **Original** | `0.001` → loosened to `0.0001` (`be759a7`, item 8) → **partially re-tightened to `0.0005`** |
| **Scope reminder:** entry side only — `ema_reversal_min_gap_pct` (exit side) was never touched by item 8 or this change, so exits still reflect realistic behavior. |
| **Tests touched:** `test_trade_review_fixes_2026_08_27.py` (1 test) — literal value and window-size bump. |

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
