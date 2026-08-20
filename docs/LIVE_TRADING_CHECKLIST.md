# Live Trading Go-Live Checklist

Target: real-money trading starting ~2026-08-26. This is a living checklist —
check items off, add new ones as they're found, and keep the "why" notes so
we don't re-litigate a decision later.

Structure: **Part A** is what actually needs to happen before the live-mode
flip. **Part B** is everything real and worth doing, but deliberately
deferred — either because it's not blocking, or because it's risky/large
enough that doing it under launch-week pressure is more dangerous than
leaving it for after you have real live-trading experience.

---

# Part A — Before going live

## A1. Paper-trading track record

`.env` says: *"Switch to live only after 45+ days profitable paper trading."*
Paper trading started 2026-06-15 (45-day mark technically passed), but a
large fraction of that history was measured on code with bugs fixed this
session (fill-vs-quote P&L errors in 6+ places, stale option prices, dead
position tracking, missed auth recovery, cross-strategy contract
collisions). Recomputed on corrected data: net profitable, **₹72,120.75
across 22 closed trades** — but momentum_v1 is a confirmed net loser
(−₹2,134.75/8 trades, 1 win) on the corrected numbers, not a fill-price
artifact.

- [ ] Run at least 2-3 more weeks of paper trading **on the fully-fixed
      code** (2026-08-13 onward) before counting it toward the 45-day gate.
      First trade was 2026-07-03, so only 41 days of real trading activity
      exist even though paper infra started 2026-06-15 — the old track
      record was earned on a materially different, buggier system.
- [ ] Give momentum_v1's poor showing a hard look — pause it, retune it, or
      make a deliberate decision to launch without it.
- [ ] Compare paper fills vs a spot-check of real quotes for a few trades —
      confirm PaperBroker's slippage model is realistic, not systematically
      generous or harsh vs what Zerodha would actually fill.
- [ ] Re-baseline the "2-3 more weeks" clock from 2026-08-20, not 2026-08-13
      — found live 2026-08-20: `ema_crossover_v1`/`momentum_v1` placed ZERO
      orders 2026-08-17..08-20 (and, traced back, 2026-07-25..08-05) due to a
      `max_dte` bug fixed this same day (see "Already done"). The existing
      22-closed-trade paper track record under-samples these two strategies
      by roughly one dead week per month; give it a few full cycles on the
      fixed code before trusting the win rate.
- [ ] Same re-baseline note applies even more strongly now: `FNO_SYMBOLS`
      expanded 41 → 132 the same day (also "Already done") — the existing
      22-trade track record reflects a completely different, much smaller
      candidate universe. Don't compare pre-2026-08-20 win rate/P&L against
      post-expansion results as if they're the same system; treat everything
      before this date as informative but not directly comparable.

## A2. Risk & capital configuration — sized for the REAL account

Current `.env`: `INITIAL_CAPITAL=300000.0`, `MAX_OPEN_POSITIONS=5`,
`MAX_DAILY_LOSS_PCT=0.05`, `MAX_EXPOSURE_PCT=0.30`.

- [ ] **Blocked on you.** Confirm `INITIAL_CAPITAL` matches the actual funded
      Zerodha account balance at go-live time, not the ₹300,000 placeholder.
      This also drives paper trading's position sizing right now
      (risk_manager + PaperBroker both read it, and it seeds the
      compounding capital-period system) — changing it will visibly shift
      paper trading behavior, so do it deliberately, not as a drive-by edit.
- [ ] `MAX_OPEN_POSITIONS` (.env says 5) still doesn't match the actually
      enforced limit (hardcoded 25, counts individual legs not structures).
      Deliberately left unwired since the two numbers aren't the same unit
      — needs an actual decision on what this should mean (e.g. `MAX_
      STRUCTURES=5` / `MAX_LEGS=20` as two separate, explicit limits), not
      just a wiring fix.
- [ ] `MAX_DAILY_LOSS_PCT` (5%) not yet explicitly re-confirmed as the right
      number for real capital — still just inherited from the paper-mode
      default.
- [ ] Confirm `_check_available_margin`'s live-mode path (`kite.margins()`)
      has actually been exercised against the real account at least once
      before go-live, not just unit-tested against a mock. Code-level gap
      already fixed (2026-08-14, external review): this used to fail OPEN
      ("Allowing trade") on a `kite.margins()` API error — both call sites
      are entry paths only, never an exit, so it now fails CLOSED and blocks
      the entry instead. What's left here is the real-account exercise
      itself, not a code change.
- [ ] Decide what happens when the kill switch fires for real: who gets
      paged, what's the manual re-enable process, has it ever been triggered
      end-to-end (not just unit-tested)?

## A3. Broker/account readiness

- [ ] Confirm Zerodha account is approved for F&O segment trading with
      sufficient margin for at least a few concurrent structures.
- [ ] Daily auth: confirm `run_daily_auth()` / the 08:30 scheduled job / the
      `_kite_self_heal()` retry-window fix have been observed working
      unattended for several real trading days — this is the single point
      of failure that already caused one full-day outage this session
      (missed daily-auth job with no active recovery).
- [ ] Confirm real Zerodha API rate limits are respected under live order
      volume (paper mode has no real rate limit to violate).
- [ ] GTT backstops (`_place_gtt_backstop`) — confirm at least one has been
      placed and verified visible in the real Zerodha GTT order book, not
      just logged as "placed" by the app, AND that the actual trigger/order
      parameters (`order_type=MARKET`, `product=NRML`, `price=0`) behave as
      expected when a real GTT fires — `kite.place_gtt()` returning a
      trigger ID is not sufficient verification on its own. Source comment
      corrected 2026-08-14 (external review): was overclaiming "server-
      independent emergency stop" — now documented as an exchange-side
      backstop layered on top of the normal exit rules, not a substitute
      guarantee of the exact exit price. The verification itself still
      requires a real Zerodha session and hasn't been done.

## A4. Operational readiness

- [ ] Decide who is "on call" during the first weeks of live trading and
      what the response SLA is for a `CRITICAL` / `MANUAL INTERVENTION
      REQUIRED` alert (several exist in the codebase already, e.g. failed
      spread/condor unwinds, failed expiry-day journal writes).
- [ ] Log retention: currently rotates out at ~1 month. Confirm that's
      enough for compliance/audit needs once real trades are involved.

## A5. Testing & CI safety net

- [ ] Full test suite green (currently 329 passing) immediately before flip.
- [ ] `deploy.sh`'s invariant-check step (`verify_invariants.py`, currently
      18 static + 4 runtime checks) wired into the actual deploy path used
      for the live-mode cutover, not run ad hoc.
- [ ] Dry run: flip `TRADING_MODE=live` on a throwaway/sandboxed Zerodha
      account or with `MAX_OPEN_POSITIONS=1` and tiny lot size first, to
      watch one real order go through the full pipeline before trusting it
      at full size.
- [ ] Re-run `verify_invariants.py` right before the live-mode flip itself,
      not just after routine deploys.
- [ ] Full pass over `_check_spread_exits`/`_check_condor_exits`/
      `_process_credit_spread`/`_process_iron_condor` one more time — these
      four functions alone accounted for most of this session's bugs; worth
      a fresh, dedicated read rather than trusting "we already fixed it"
      from memory.

## A6. Go-live mechanics

- [ ] Confirm all currently-open paper positions are cleanly closed (or
      explicitly excluded) before the mode flip — don't want paper and live
      state coexisting in `_active_spreads`/`_active_condors`.
- [ ] Pick the actual go-live date/time deliberately (e.g. not on an expiry
      day, not right before a holiday) rather than defaulting to "whenever
      it's ready."
- [ ] Post-flip: watch the first live session in real time rather than
      trusting it to run unattended on day 1.

---

# Part B — Later stage (real, but deliberately deferred)

Bigger or riskier changes where introducing a new bug under launch-week
pressure is worse than leaving a known, bounded gap for now.

## B1. Position/order state machine & crash recovery

- [ ] **Multi-leg execution atomicity beyond what exists.** Today's
      unwind-on-failure logic handles "leg 2 fails to place," but not
      "process crashes between leg 1 filling and leg 2 being attempted" —
      a real gap (naked short at the broker that nothing detects on
      restart). The Zerodha daily sync built this session reconciles
      *orders*, not *positions*, so it wouldn't catch this specific case.
      A scoped fix (not the full state machine below): on startup, compare
      broker's real positions against internal state, and if a leg exists
      at the broker with no matching internal record, alert loudly and
      block new entries until resolved.
- [ ] **Full Position/Structure state machine**, as proposed by the
      external review (`DETECTED → PLANNING → ENTRY_SENT → PARTIAL_FILL →
      ACTIVE → EXIT_PENDING → CLOSED`, plus a separate `UNKNOWN →
      RECONCILING → RESOLVED` path for broker/internal state mismatches).
      This is the "right" long-term architecture but a multi-day rewrite of
      the core position-tracking model — do this with real live-trading
      experience behind you, not as a pre-launch scramble.
- [ ] Failure-injection tests for the above: broker-timeout-with-duplicate-
      order idempotency, and engine-restart-with-orphaned-position recovery
      (the two most valuable of the external review's 8 proposed chaos
      tests; the others are already covered by this session's work).

## B2. Backtester realism

- [ ] `qty=1`, no brokerage/slippage/spread modeled, and doesn't appear to
      share execution logic with paper/live (single-leg OHLC signal → price
      → exit, not real option-chain/multi-leg simulation) — **not yet
      independently verified**, flagged by the external review, haven't
      opened `src/backtesting/` this session to confirm firsthand.
      Paper trading (which uses the *real* execution path) is what's
      actually gating this go-live, not backtest metrics — fix this before
      leaning on backtest numbers to pick or tune strategies, not before
      this launch.
- [ ] Walk-forward/Monte Carlo/robustness as a mandatory gate before calling
      any strategy "profitable" — the tooling already exists
      (`walk_forward.py`, `monte_carlo.py`, `robustness.py`); this is a
      process discipline to adopt, not a code change.

## B3. Risk manager refinements

- [ ] Reserved-capital / TOCTOU race: two strategies' risk checks could both
      pass against the same available capital before either order fills.
      Asyncio's single-threaded event loop narrows (doesn't eliminate) this
      vs. a truly parallel system — worth understanding exactly which
      `await` points make this reachable before deciding it needs a formal
      reservation mechanism.
- [ ] Daily loss limit on start-of-day equity (rather than the compounding-
      per-expiry-period basis it uses now), plus strategy-level loss limits
      and a consecutive-loss circuit breaker.
- [ ] Full continuous broker-reconciliation mode (`if broker/DB/Redis
      disagree → stop new entries`) — the narrow startup check in B1 gets
      most of the safety for a fraction of the risk; this is the fuller
      version.

## B4. Explicitly NOT doing right now (and why)

- **Moving historical bug commentary out of source into docs.** Those
  "Fixed 2026-08-13..." comments were genuinely load-bearing this session —
  they're how repeat mistakes were avoided. A mechanical cleanup pass this
  close to go-live risks losing that context for zero functional benefit.
- **UTC-internal / convert-at-display time refactor.** The specific
  timezone bugs that mattered have already been found and fixed at their
  actual usage sites. A full internal representation change now is exactly
  the "improve architecture, introduce a new bug" risk to avoid pre-launch.
- **Market-chain-driven strike selection instead of delta/Black-Scholes.**
  Changes actual trading behavior, which would invalidate the paper-trading
  track record being used as the go/no-go signal. Post-launch strategy
  improvement, not a pre-launch fix.
- **Unifying EMA/Momentum into a shared SignalConfirmationStateMachine.**
  Pure refactor; both already work correctly and are tested.
- ~~**Dynamic FNO_SYMBOLS universe via `kite.instruments()`.**~~ Done
  2026-08-20 — see "Already done" below. (Originally deferred as "a
  maintenance improvement, not a correctness fix right now"; the user asked
  to actually measure the effect with real data instead of leaving it
  deferred, which changed the calculus once the numbers were in hand.)

---

# ✅ Already done

Kept short — see git log / individual commit messages for full detail.

- **P&L recompute** on corrected fill-price data (2026-08-13)
- **`verify_invariants.py`** extended to 18 static + 4 runtime checks
  covering every fix made this session (2026-08-13/14)
- **Expiry-to-expiry compounding capital periods** driving live risk limits,
  restart-safe (2026-08-13, bug found and fixed 2026-08-14)
- **`MAX_EXPOSURE_PCT`** fixed as dead config and raised to 30% (2026-08-13)
- **Daily Zerodha↔DB order reconciliation**, live-mode gated, blocking calls
  fixed, per-order error isolation added (2026-08-13/14)
- **MySQL backups**: daily cron, 90-day retention, restore-tested
  end-to-end (2026-08-13)
- **Notification pipeline** tested end-to-end, email confirmed delivered
  (2026-08-13)
- **Server hardening**: SSH password-auth actually disabled this time
  (verified with a fresh connection, no lockout), fail2ban installed,
  `.env` permissions tightened (2026-08-13)
- **Rollback runbook** written: [ROLLBACK_RUNBOOK.md](ROLLBACK_RUNBOOK.md)
  (2026-08-13)
- **10 code-review findings fixed**: capital-period restart bug, expiry-day
  data-loss-on-failure bug, duplicate RiskManager instance in
  `orders_router.py`, blocking Zerodha calls, missing per-order error
  isolation, a broken stray test file, dead/stale config references,
  duplicated collision-guard/fill-price-helper/position-fetch logic
  consolidated (2026-08-14)
- **`.env` untracked from git** — was committed since the repo's first
  commit on a public GitHub repo; verified only placeholders ever leaked,
  server's real secrets file confirmed byte-identical throughout (2026-08-14)
- **36 `__pycache__` files untracked** from git (2026-08-14)
- **Lot-size/contract resolution now fails closed** instead of trading a
  computed guess when Redis cache data is unavailable — `_get_lot_size()`
  and `_resolve_contract()` both return `None`/`Optional` on a cache miss,
  every entry path (single-leg, credit-spread ×4 call sites, iron-condor
  ×4 legs) explicitly checks and skips rather than trading unverified data.
  Verified safe to deploy (both caches were 41/41 populated on the live
  server) before shipping (2026-08-14)
- **ML confirmed correctly separated** from live signal generation — no
  action needed (2026-08-14)
- **5 code-level gaps from an external pre-live review fixed** (2026-08-14,
  verified against actual code before fixing, not taken on faith):
  - `_safe_get_positions()` used to conflate "broker call failed" with
    "broker confirmed zero positions" (both returned `[]`). Now tracks a
    separate `_broker_position_state_known` flag; `run_signal_cycle()`
    blocks all new entries for the cycle when it's `False`, without
    changing exit behavior.
  - `_check_available_margin()`'s live-mode path used to fail OPEN
    ("Allowing trade") on a `kite.margins()` API error. Both call sites are
    entry paths only (never an exit), so it now fails CLOSED instead.
  - Found while fixing the above: the paper-mode branch of the same
    function read `getattr(settings, "initial_capital", 300_000)` —
    lowercase, which never matched the real case-sensitive
    `INITIAL_CAPITAL` — so it silently used the hardcoded 300,000 fallback
    regardless of the actually configured value. Now reads the real setting.
  - `_get_market_data()`'s timestamp validation let a malformed/unparseable
    timestamp through unchanged instead of rejecting it, skipped the
    staleness check entirely on a missing timestamp, and never rejected a
    future timestamp. All four cases (missing/malformed/future/stale) now
    return `None`.
  - The condor regime-shift exit check's silent no-op on a Redis/JSON error
    (the one confirmed fail-open gap called out in the external review, was
    tracked in A4) now logs a warning instead.
  - GTT backstop docstring corrected from the overclaiming "server-
    independent emergency stop" to "exchange-side emergency backstop" — the
    real-account trigger/order-parameter verification itself (A3) is still
    open, this was a documentation-accuracy fix, not a substitute for it.
  - 13 new tests added (`tests/test_prelive_external_review_2026_08_14.py`).
- **Single-leg (`ema_crossover_v1`/`momentum_v1`) monthly DTE dead zone
  fixed** (2026-08-20). User flagged zero trades for 3 days; traced to
  `_process_signal`'s expiry resolution (`get_near_month_expiry()`, only
  rolls at DTE<7 — unlike credit_spread/iron_condor's `get_entry_expiry()`,
  which was fixed for exactly this reason on 2026-08-13 but never wired into
  the single-leg path). Right after a monthly roll the fresh contract's DTE
  can be as high as 41, above the old `max_dte=25` — blocking every entry
  for ~1-2 weeks each month. Confirmed via order history this happened twice
  already (2026-07-25..08-05, 2026-08-17..08-20), not a regression from any
  change made this session — the code path dates to 2026-06-18. Separately
  confirmed `credit_spread_v1`/`iron_condor_v1` were independently idle over
  the same window purely because VIX sat below the 12.0 premium-selling
  floor — that part is working as designed, not a bug. Fix: raised
  `max_dte` from 25 to 42 (covers the real worst case of 41, verified by
  walking every month's expiry-to-expiry gap on the actual NSE calendar
  function). 5 new tests, 1 new `verify_invariants.py` static check.
- **Strike interval now derived from real listed contracts, not just the
  static table** (2026-08-20). `FNO_STRIKE_INTERVALS` was already found
  wrong for 27/39 symbols once this project — `get_real_contract()` protects
  against ordering a strike that isn't listed (snaps to nearest real one),
  but a wrong interval still corrupts the *candidate* fed into it, especially
  `find_delta_strike()`'s scan grid (candidates spaced by `strike_interval`
  across up to 30 strikes from ATM — too large overshoots the intended delta
  range, too small never reaches it). New `get_real_strike_interval()`
  (`option_chain.py`) derives the interval from the minimum gap between
  consecutive real strikes in the same daily-refreshed real-contract cache
  `get_real_contract()` already reads — self-correcting, can't drift out of
  sync with NSE the way a hand-maintained table can. Wired into all 3 entry
  paths (`_process_signal`/`_process_credit_spread`/`_process_iron_condor`)
  via new `LiveTradingEngine._get_strike_interval()`, which falls back to the
  static table on a cache miss (intentionally fail-open here — this feeds a
  candidate that still gets validated/snapped downstream, not a final trade
  decision). Also de-risks a future `FNO_SYMBOLS` universe expansion (see
  Part B note above) — a new symbol needs zero manual strike-interval
  verification. 11 new tests, 1 new `verify_invariants.py` static check.
- **F&O universe-expansion prep: real universe sizing, sector mapping,
  concurrent OHLC prefetch** (2026-08-20). User asked to actually measure
  the effect of a ~208-symbol universe instead of reasoning about it
  abstractly. Read-only diagnostic (`scripts/diagnostic_universe_timing.py`,
  no orders/writes, live `kite.instruments("NFO")` + `kite.historical_data()`
  calls) found: real universe is 208 stocks (not the ~190 estimated), and a
  sequential cold-start OHLC fetch across all 208 takes 48.4s of the 60s
  cycle budget with zero rate-limit errors — safe in steady state (~10s/
  cycle, since only ~1/5 of symbols refresh per minute) but risks a skipped
  cycle on every restart. Also found `FNO_SECTORS` (sector-concentration
  risk check) was *also* a hardcoded 41-entry table, same shape as the
  strike-interval gap, silently no-op'ing for any symbol not in it — unlike
  strike interval, sector classification isn't in Zerodha's instrument data,
  so this needed external verification (web search against live sources,
  not memory) rather than a derivation. Two fixes:
  - `FNO_SECTORS` extended to all 208 real symbols (currently only 41 are
    active via `FNO_SYMBOLS`; the rest sit ready, unused, for a future
    expansion) — every entry verified against a live source, not guessed,
    including recent listings (`TMPV`, `PREMIERENE`, `VMM`, `LTM`, `GVT&D`,
    `WAAREEENER`, `PGEL`).
  - New `LTPPoller._prefetch_stale_histories()`: fetches every symbol's
    stale OHLC concurrently (bounded to 5 at a time via a semaphore, not
    full-parallel, to stay well under Zerodha's rate limits) before the main
    per-symbol loop, for both the 5-min and 15-min caches. Removes the cold-
    start cycle-skip risk regardless of universe size.
  - 6 new tests, 2 new `verify_invariants.py` static checks.
- **FNO_SYMBOLS expanded 41 → 132, liquidity-verified** (2026-08-20). With
  the prep above done, ran a second read-only diagnostic
  (`scripts/diagnostic_universe_liquidity.py`) computing real 20-day average
  daily turnover (`volume × close`) for all 208 symbols via
  `kite.historical_data(interval="day")` — 36.4s, zero failures. Found the
  current 41 was never actually "the 41 most liquid" — it's a legacy
  curated list with real gaps (e.g. `BSE` ranks #6 by turnover, more liquid
  than `TCS`, but wasn't traded; `KALYANKJIL`, `ETERNAL`, `PAYTM`, `MCX`,
  `LICI`, `SWIGGY` are all comparably liquid newer/renamed listings that
  arrived after the list was built). Used "at least as liquid as
  TATACONSUM" (the least-liquid symbol already traded, #132 by rank) as the
  floor: 132 of 208 symbols clear it. Expanded `FNO_SYMBOLS` to exactly that
  132-symbol set — purely additive, nothing previously traded was removed,
  every addition liquidity-verified against real turnover data rather than
  "NSE says it's F&O eligible." The remaining 76 (e.g. `DALBHARAT` ₹38
  Cr/day, `PETRONET`/`BAJAJHLDNG` ₹55 Cr/day) stayed excluded as genuinely
  too thin. Also fixed a regex fragility this surfaced in
  `verify_invariants.py`: a source comment containing a quoted string
  (`kite.instruments("NFO")`) inside the `FNO_SYMBOLS` list literal was
  getting swept up by a naive `re.findall` as if it were a real list entry
  — added `_strip_comments()` so future comments can't trip the same check.
  5 new tests (`tests/test_fno_universe_expansion_2026_08_20.py`), 1 new
  `verify_invariants.py` static check, 1 existing test relaxed
  (`FNO_LOT_SIZES` no longer needs 1:1 `FNO_SYMBOLS` coverage now that it's
  fallback-only — the daily Redis cache is authoritative).

---

*Last updated: 2026-08-20, after the FNO_SYMBOLS 41→132 expansion.*
