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

- [ ] Full test suite green (currently 375 passing) immediately before flip.
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
- **F&O active universe made self-correcting instead of a static snapshot**
  (2026-08-20). User asked the obvious follow-up: liquidity isn't static —
  what happens when a thin symbol excluded today gets thick in a month, or
  vice versa? Chose full automation (system self-adjusts on a schedule, no
  human review step) over a notify-and-approve or manual-rerun alternative.
  - New `src/market_data/fno_universe.py`: shared pure-function logic
    (universe discovery, liquidity ranking) deduped from both diagnostic
    scripts and the new weekly job — `MIN_ADTV_CR = 150.0` is a **fixed**
    Rs Crore/day threshold, deliberately not "worst of whatever's currently
    active" (a self-referencing floor would drift: dropping thin symbols
    raises the floor of "the worst remaining" one, silently shrinking the
    universe on every successive run).
  - `scripts/zerodha_auto_auth.py`'s daily lot-size/real-contract cache
    population widened from "only the active FNO_SYMBOLS list" to the full
    real universe (excluding index options) — otherwise a symbol the weekly
    job newly promotes would fail closed for a day waiting for the *next*
    day's cache refresh to happen to cover it too.
  - New `recompute_active_universe()`: fetches the full real universe,
    computes 20-30 day average daily turnover, writes the qualifying set to
    Redis (`REDIS_ACTIVE_FNO_SYMBOLS`, read via `get_active_fno_symbols()`,
    falls back to the static `FNO_SYMBOLS` list on any cache miss/error).
    Wired into the scheduler as `_weekly_universe_refresh`, Sunday 07:00 IST
    (outside market hours, before Monday's 08:30 auth job) — pushes freshly
    resolved tokens straight into `LTPPoller`/`RSRanker` and notifies via
    email with an added/removed summary.
  - **The one hard safety requirement**: an open position must never lose
    market-data coverage just because its symbol's liquidity later falls
    below the floor. `LTPPoller.register_underlying()`/
    `unregister_underlying()` force-track any symbol with a currently open
    position regardless of active-universe membership.
    `LiveTradingEngine._sync_must_track_underlyings()` reconciles that
    force-tracked set against whatever's actually open
    (`_active_spreads`/`_active_condors`/`_single_leg_journals`) once per
    signal cycle — a single source of truth instead of an incremental
    register/unregister call at each of the ~15 scattered entry/exit sites
    in `live_trading_engine.py`, and immediately synced on restart via
    `attach_symbol_poller()` (mirrors `attach_ltp_poller()`'s existing
    restore-on-restart pattern for option contracts).
  - `_provision_kite()` widened the same way as the daily cache population —
    used to resolve NSE tokens only for the static `FNO_SYMBOLS` list, which
    would have lost the token for anything the weekly job dynamically added
    on the next restart between weekly runs.
  - `RSRanker` got the same dynamic-symbol-list treatment as `LTPPoller`
    (no force-tracking needed there — it's a soft entry-signal filter, not
    the sole source of market-data coverage, so the position-safety
    requirement above doesn't apply to it).
  - 33 new tests across 5 new test files
    (`tests/test_fno_universe.py`, `tests/test_active_fno_symbols.py`,
    `tests/test_ltp_poller_dynamic_universe.py`,
    `tests/test_engine_symbol_poller_sync.py`,
    `tests/test_recompute_active_universe.py`), 1 new `verify_invariants.py`
    static check, 1 existing test in
    `tests/test_zerodha_auto_auth_contracts.py` replaced with 2 reflecting
    the widened (not FNO_SYMBOLS-filtered) daily cache population.
- **Full-system code review of the day's work (8-angle, high effort),
  4 confirmed bugs fixed** (2026-08-20). User asked for a bug/logic-error
  review of the whole system with emphasis on that day's changes. Ran the
  established review process (line-by-line, removed-behavior, cross-file
  tracer, reuse, simplification, efficiency, altitude -- conventions angle
  skipped, no CLAUDE.md exists) against the full day's diff, verified each
  surviving candidate directly against the code before fixing.
  - **Most severe**: `LTPPoller` polled `self.symbols` (active-liquid list
    UNION force-tracked open-position underlyings) and scored/published
    ALL of them into the top-N entry-candidate pools `_process_signal`
    reads for new entries -- meaning a symbol force-tracked *only* to keep
    an existing position's exit data fresh could still rank into a pool
    and trigger a brand-new reversal entry on itself, defeating the whole
    point of demoting it. Confirmed reachable and completely unprotected
    on the SELL/PE side (the RS-rank gate that partially covers BUY
    doesn't apply to SELL at all). Fixed: new `LTPPoller._active_set`
    tracks the liquidity-eligible subset separately from what's merely
    polled; only `_active_set` members compete for entry-candidate pools,
    while `self.symbols` (the union) still keeps market data fresh for
    everything, including force-tracked positions.
  - `recompute_active_universe()` wrote its computed active list to Redis
    unconditionally, with no check for a partial data-coverage failure
    (`compute_liquidity_turnover()` catches each symbol's fetch failure
    individually and continues) -- a Zerodha rate-limit/timeout mid-run
    could silently publish a tiny list as if it were a genuine liquidity
    result, collapsing the universe for a week. Independently flagged by
    3 of 8 finder angles. Fixed: refuses to publish (keeps the previous
    list live, notifies) when turnover data covers under 70% of the
    symbols attempted.
  - The weekly job updated `LTPPoller`/`RSRanker` but never
    `ZerodhaTicker` (the actual primary real-time tick source --
    `LTPPoller`'s own docstring says today's bars are built ONLY from live
    ticks, never `historical_data()`) or `ZerodhaLTPPoller` (its REST
    fallback) -- both stayed frozen at their startup-time symbol list.
    Fixed: new `ZerodhaLTPPoller.set_symbols()`, both components updated
    in the same weekly-job run.
  - Self-heal's token-recovery log line divided by the static
    `len(FNO_SYMBOLS)` (132) even though `_provision_kite()` now resolves
    against the full ~208-symbol universe -- could log an impossible
    "over 100%" ratio. Cosmetic, fixed.
  - Two findings deliberately left as-is after consideration: `RSRanker`'s
    dynamic refresh doesn't union force-tracked underlyings the way
    `LTPPoller` now does -- lower priority since the `LTPPoller` fix above
    already prevents a demoted symbol from reaching any pool `RSRanker`'s
    gate would matter for. `get_active_fno_symbols()` fails open to the
    static `FNO_SYMBOLS` list on a Redis error -- judged acceptable on
    reflection, since that fallback is itself a deliberately-vetted,
    already-liquidity-verified baseline (this same day's own 132-symbol
    expansion), not a computed guess the way the lot-size/contract
    fail-open bug from the prior day was.
  - 12 new tests, 2 new `verify_invariants.py` static checks. 368 tests
    passing.
- **Second review round: fixes to the first round's fixes** (2026-08-20).
  User asked for another review; scoped it to just the fix commit above
  rather than re-covering the whole day's diff again, since that had
  already been thoroughly reviewed. Same 8-angle process. 3 confirmed bugs,
  all in the newly-fixed code:
  - **Most severe, independently confirmed by 5 of 6 finder angles**: the
    ZerodhaTicker fix from the first round mutated `zt._instrument_tokens`/
    `_token_symbol` directly, but `subscribe()`/`set_mode()` are ONLY ever
    called from `_on_connect()` -- an already-open WebSocket connection was
    never actually told about a newly-promoted symbol, so it silently got
    zero real-time ticks until an unrelated reconnect (which, on a healthy
    connection, could be the rest of the trading week). This exact class of
    "looks fixed, isn't wired to the live connection" bug undercut the
    first round's own stated goal. Fixed: new
    `ZerodhaTicker.set_instrument_tokens()` calls `subscribe()`/`set_mode()`/
    `unsubscribe()` directly on the live connection instead of only
    updating bookkeeping dicts.
  - `_weekly_universe_refresh()` discarded a fully-valid, already-fetched
    tokens dict whenever the run was skipped (partial-coverage failure) --
    token resolution succeeds/fails independently of the turnover-fetch
    failure that triggers a skip. Fixed: tokens are now pushed to all live
    components BEFORE the skip-path check, not after.
  - The `MIN_COVERAGE_FRACTION` guard only compared turnover-vs-tokens,
    blind to a truncated `kite.instruments()` response that shrinks
    `tokens` itself before turnover ever runs (100% coverage of an
    already-collapsed set passes cleanly). Independently confirmed by 3
    finder angles. Fixed: added a second guard comparing tokens resolved
    against the full known universe, at the pipeline stage where a
    truncation would actually first appear.
  - Two lower-severity findings deliberately deferred: `compute_liquidity_
    turnover()` can't distinguish an API failure from a symbol legitimately
    having sparse trading data (judged very low probability for NSE F&O-
    eligible stocks, which must already meet minimum liquidity criteria to
    be listed); `ZerodhaLTPPoller.set_symbols()` has a narrow single-cycle
    race window with an in-flight `refresh_ltp()` call (self-heals next
    cycle, once a week).
  - 15 new tests, 1 existing `verify_invariants.py` check strengthened.
    375 tests passing.
- **Full-system deep review (2026-08-20)** — non-diff-scoped, unlike every
  round above. User remained skeptical after two diff-scoped review rounds
  ("I am still skeptical... deep review") and explicitly wanted the whole
  system read end-to-end, not just recently-changed lines. 7 parallel agents
  each read a full subsystem (live_trading_engine.py in three passes covering
  single-leg exit/persistence, spread/condor exit, and risk/capital helpers;
  order_manager/risk_manager/paper_broker/zerodha broker; the market data
  pipeline; all five strategies; api/main.py's lifespan+scheduling). Every
  finding verified against the actual code before fixing, not trusted from
  the agent report alone. 18 findings reported, all fixed (user chose "fix
  everything").
  - **Critical — 3 exit paths never checked order_status before treating a
    SELL as closed**: `_close_option_positions` (reversal exit),
    `_square_off_all` (mandatory EOD close), and `_exit_all_options_for`
    (EXIT-signal close) all placed a broker order and then unconditionally
    journaled it as closed and released capital — unlike the sibling
    `_execute_single_leg_exit`, which already checked. A rejected/failed
    broker order would fabricate a closed position while the real position
    stayed open, untracked, for the rest of the day (or overnight, for
    square-off). Fixed: all three now check `order_status` and leave the
    position tracked for retry on rejection, matching the established
    pattern.
  - **Critical — daily-loss kill switch never actually saw realized P&L**:
    `_refresh_risk_state` summed `positions[i]['realized_pnl']`/
    `['unrealized_pnl']` — keys neither broker ever populates (Zerodha's real
    fields are `realised`/`unrealised`/`pnl`; PaperBroker's positions only
    carry symbol/quantity/avg_price) — so `daily_realized_pnl` was silently
    always 0.0 in both modes, regardless of real losses. The one fallback
    that gave unrealized P&L any value only summed `_active_spreads`/
    `_active_condors`, so open single-leg positions contributed nothing
    either. Fixed: realized P&L now comes from `trade_journal` (correct in
    both modes, same pattern `send_daily_report()` already used); unrealized
    P&L is computed from the real broker position list (covers every open
    leg, single- and multi-leg alike).
  - **Critical — `expire_stale_orders()` could place a genuine duplicate
    order**: discarded `cancel_order()`'s return value entirely. If an order
    had actually already filled at the broker in the window since the last
    periodic sync (up to ~1 minute), `cancel_order()` correctly returned
    `False` — but that was never checked, so the order got marked `EXPIRED`
    and retried anyway. Fixed: syncs against the broker's real status first,
    and on a failed cancel, re-syncs and trusts the real terminal status
    instead of assuming "stale, safe to retry."
  - **Critical — multi-leg exit retry resent orders for already-closed
    legs**: on a partial rejection (one leg's closing order fills, the
    sibling is rejected), the code correctly kept the position tracked for
    retry — but the *next* retry cycle resent orders for *all* legs,
    including the one that already closed and is now flat, opening an
    accidental new position on it (up to 3 accidental positions for a
    4-leg iron condor). Fixed: `_close_leg()` remembers each leg that
    already closed (this cycle or a previous partial attempt) and never
    resubmits an order for it; only re-fetches broker positions to detect
    "already flat" when the fetch is known-good (an empty/failed fetch
    must not be misread as "everything's already closed").
  - **Critical — NaN/None `atr14` bypassed the low-volatility gate**:
    `credit_spread.py`/`iron_condor.py`'s `generate_signal()` excluded
    `atr14` from the missing-data guard. `NaN >= threshold` is `False` in
    Python, so a NaN ATR (possible with insufficient bar history in the
    upstream EWM calc) fell through into the directional/condor branch with
    genuinely unknown volatility instead of returning `HOLD`. Fixed: both
    now treat `None`/NaN `atr14` as missing data.
  - **Critical — `StrategyMonitor`'s recovery log could crash and silently
    block all new entries**: `_profit_factor()` legitimately returns `None`
    whenever the rolling window has zero losing trades — but the "now
    healthy" log line formatted it with `:.3f` unconditionally, raising an
    uncaught `TypeError`. `evaluate_all()` had no per-strategy isolation and
    is called directly from `run_signal_cycle()` with no try/except, so this
    would abort the rest of that entire cycle (regime detection + all
    entry-signal generation) and repeat every cycle for as long as the
    zero-losers condition persisted. Fixed: both the format crash and the
    missing isolation.
  - High/medium, also fixed: `ZerodhaBroker.place_order()`'s bare `@retry`
    had no idempotency check (a lost response after Zerodha already accepted
    the order would place a genuine duplicate) — replaced with a manual
    retry loop that tags each order with the internal DB order id and checks
    for an existing order by tag before resubmitting; paper-mode margin
    check used the static `INITIAL_CAPITAL` instead of the broker's live
    balance; spread/condor exit DTE was computed from the global near-month
    expiry instead of each position's own stored (possibly next-month)
    expiry, risking a weeks-early force-close; new spread/condor entries
    mutated `_active_spreads`/`_active_condors` without the lock the 10s
    exit job iterates them under, risking a "dict changed size during
    iteration" abort; `RSRanker`'s per-symbol scoring loop had no exception
    isolation (unlike `LTPPoller`'s) and its published keys had no TTL;
    `TRADING_MODE=LIVE` silently falling back to `PaperBroker` on a missing
    token only logged, never alerted; `_peak_premiums` (the trailing-stop/
    breakeven-stop high-water mark) was never persisted across restarts;
    orphaned single-leg positions restored from a previous day were silently
    dropped instead of force-closed (with a best-effort `trade_journal`
    match for genuinely-orphaned live-mode positions that predate this fix).
  - 15 new tests, 1 existing `verify_invariants.py` check updated for the
    `_close_leg()` refactor. 390 tests passing. Deployed and verified live
    (30/30 static+runtime invariant checks pass; API/DB/Redis healthy,
    RSRanker's first post-deploy cycle ran clean).

---

*Last updated: 2026-08-20, after the full-system deep review round's fixes.*
