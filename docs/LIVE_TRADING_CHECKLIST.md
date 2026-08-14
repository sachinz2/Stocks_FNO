# Live Trading Go-Live Checklist

Target: real-money trading starting ~2026-08-26. This is a living checklist —
check items off, add new ones as they're found, and keep the "why" notes so
we don't re-litigate a decision later.

---

## 1. Re-establish a clean paper-trading track record

`.env` says: *"Switch to live only after 45+ days profitable paper trading."*
Paper trading started 2026-06-15, so the 45-day mark has technically passed —
but a large fraction of that history was measured on code with the bugs
fixed in this session (fill-vs-quote P&L errors in 6+ places, stale option
prices, dead position tracking, missed auth recovery, cross-strategy
contract collisions). The historical *win/loss pattern* is still valid, but
the exact P&L numbers it was judged on were often wrong — one trade even
flipped from a recorded profit to a real loss (POWERGRID, +₹380 → −₹2,052).

- [x] **Done 2026-08-13.** Recomputed total realized P&L on corrected data:
      net profitable, **₹72,120.75 across 22 closed trades**
      (credit_spread_v1 ₹73,033.00/9 trades, iron_condor_v1 ₹1,163.25/4,
      ema_crossover_v1 ₹59.25/1, **momentum_v1 −₹2,134.75/8 — 1 win, 7
      losses**). momentum_v1's poor showing is confirmed on correct
      numbers, not a fill-price artifact — worth a hard look before go-live.
- [ ] Run at least 2-3 more weeks of paper trading **on the fully-fixed
      code** (2026-08-13 onward — today's fixes) before counting it toward
      the 45-day gate — the old track record was earned on a different,
      buggier system. First trade was 2026-07-03, so only 41 days of real
      trading activity exist even though paper infra started 2026-06-15.
- [ ] Compare paper fills vs a spot-check of real quotes for a few trades —
      confirm PaperBroker's slippage model is realistic, not systematically
      generous or harsh vs what Zerodha would actually fill.

## 2. Regression-guard everything fixed this session

A lot of severe bugs were found by hand this session. Before trusting real
money to it, close the loop so the next change can't silently reintroduce
one of them.

- [x] **Done 2026-08-13.** Added 8 invariant checks (`scripts/verify_invariants.py`)
      covering entry/exit fill-vs-quote pricing, the cross-strategy
      contract-collision guard, exposure/daily-loss cap wiring, capital-period
      compounding driving live limits, the Zerodha-sync live-mode gate, stale
      option-price resolution, and auth self-heal. 21/21 pass against the live
      server (static + runtime).
- [ ] Re-run `verify_invariants.py` right before the live-mode flip, not
      just after routine deploys.
- [ ] Full pass over `_check_spread_exits`/`_check_condor_exits`/
      `_process_credit_spread`/`_process_iron_condor` one more time —
      these four functions alone accounted for most of this session's bugs;
      worth a fresh, dedicated read rather than trusting "we already fixed
      it" from memory.

## 3. Risk & capital configuration — sized for the REAL account, not the default

Current `.env`: `INITIAL_CAPITAL=300000.0`, `MAX_OPEN_POSITIONS=5`,
`MAX_DAILY_LOSS_PCT=0.05`, `MAX_EXPOSURE_PCT=0.30` (raised from 0.20,
2026-08-13 — also fixed as dead config, see §2 history: it wasn't wired
to anything at all before today).

- [ ] **Blocked on you.** Confirm `INITIAL_CAPITAL` matches the actual funded
      Zerodha account balance at go-live time, not the ₹300,000 placeholder.
      Also drives paper trading's position sizing right now (risk_manager +
      PaperBroker both read it) — changing it will visibly shift paper
      trading behavior, so do it deliberately, not as a drive-by edit.
- [x] **Partially done 2026-08-13.** `MAX_EXPOSURE_PCT` explicitly decided
      → 30% (was silently unenforced 20% before today's fix).
      `MAX_DAILY_LOSS_PCT` (5%) not yet explicitly re-confirmed for real
      capital — still just inherited from the paper-mode default.
- [ ] `MAX_OPEN_POSITIONS` (.env says 5) still doesn't match the actually
      enforced limit (hardcoded 25, counts individual legs not
      structures) — found 2026-08-13, deliberately left unwired since the
      two numbers aren't the same unit. Needs an actual decision on what
      this should mean, not just a wiring fix.
- [ ] Confirm `_check_available_margin`'s live-mode path (`kite.margins()`)
      has actually been exercised against the real account at least once
      before go-live, not just unit-tested against a mock.
- [ ] Decide what happens when the kill switch fires for real: who gets
      paged, what's the manual re-enable process, has it ever been triggered
      end-to-end (not just unit-tested)?

## 4. Broker/account readiness

- [ ] Confirm Zerodha account is approved for F&O segment trading with
      sufficient margin for at least a few concurrent structures.
- [ ] Daily auth: confirm `run_daily_auth()` / the 08:30 scheduled job / the
      `_kite_self_heal()` retry-window fix (added this session) have been
      observed working unattended for several real trading days — this is
      the single point of failure that already caused one full-day outage
      this session (missed daily-auth job with no active recovery).
- [ ] Confirm real Zerodha API rate limits are respected under live order
      volume (paper mode has no real rate limit to violate).
- [ ] GTT backstops (`_place_gtt_backstop`) — confirm at least one has been
      placed and verified visible in the real Zerodha GTT order book, not
      just logged as "placed" by the app.

## 5. Operational readiness

- [x] **Done 2026-08-13.** Daily MySQL backups: `/home/falcon/backup_mysql.sh`
      via cron at 02:00 UTC (~07:30 IST), 90-day retention, gzip-compressed.
      Restore-tested end-to-end (dumped, restored into a scratch DB, row
      counts matched exactly, scratch DB dropped).
- [x] **Done 2026-08-13.** Notification pipeline tested end-to-end — a real
      TEST message sent via `ComboNotifier`, confirmed delivered to email
      (the only channel currently configured; Telegram has no
      `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` set, so it's a documented no-op,
      not a silent failure). Decided email-only is sufficient for go-live.
- [ ] Decide who is "on call" during the first weeks of live trading and
      what the response SLA is for a `CRITICAL` / `MANUAL INTERVENTION
      REQUIRED` alert (several exist in the codebase already, e.g. failed
      spread/condor unwinds).
- [x] **Done 2026-08-13.** Server hardening: found the original SSH-hardening
      commands (from initial server setup) had silently no-op'd — sed patterns
      didn't match the actual config lines, so PasswordAuthentication was
      still enabled server-wide despite bash history showing an attempt to
      disable it. Fixed properly this time (verified with `sshd -T` +
      `sshd -t` before applying, reloaded, then confirmed with a **fresh**
      connection before considering it done — no lockout). Also: installed
      + enabled fail2ban (wasn't running at all), tightened `.env` from
      world-readable (664) to owner-only (600). Root login was already
      key-only. Note: unattended-upgrades has a pending kernel update
      needing a reboot to fully apply — not done (would restart all
      containers/trading), your call on timing.
- [ ] Log retention: currently rotates out at ~1 month. Confirm that's
      enough for compliance/audit needs once real trades are involved.

## 6. Testing & CI safety net

- [ ] Full test suite green (currently 244 passing) immediately before flip.
- [ ] `deploy.sh`'s invariant-check step (`verify_invariants.py`) wired into
      the actual deploy path used for the live-mode cutover, not run ad hoc.
- [ ] Dry run: flip `TRADING_MODE=live` on a throwaway/sandboxed Zerodha
      account or with `MAX_OPEN_POSITIONS=1` and tiny lot size first, to
      watch one real order go through the full pipeline before trusting it
      at full size.

## 7. Go-live mechanics

- [x] **Done 2026-08-13.** Written rollback runbook: see
      [ROLLBACK_RUNBOOK.md](ROLLBACK_RUNBOOK.md) — concrete decision tree and
      exact commands (pause strategies, close a position, flip out of live
      mode, full stop), grounded in the real admin endpoints that exist today.
- [ ] Confirm all currently-open paper positions are cleanly closed (or
      explicitly excluded) before the mode flip — don't want paper and live
      state coexisting in `_active_spreads`/`_active_condors`.
- [ ] Pick the actual go-live date/time deliberately (e.g. not on an expiry
      day, not right before a holiday) rather than defaulting to "whenever
      it's ready."
- [ ] Post-flip: watch the first live session in real time rather than
      trusting it to run unattended on day 1.

## 8. From the external architecture review (2026-08-14)

An outside review flagged 27 points. Most were either already fixed earlier
in this project's history, or duplicate items already above. This section
covers only what's genuinely NEW and was independently verified against the
actual current code (not taken on the reviewer's word) before being added.

- [x] **Done 2026-08-14.** `.env` was git-tracked since the repo's first
      commit, on a now-confirmed-**public** GitHub repo. Verified every
      historical version — only ever held placeholder values, so no real
      credential rotation needed. Untracked via `git rm --cached` on both
      local and server; verified the server's real `.env` (genuine Zerodha/
      email/DB secrets) was byte-for-byte unchanged throughout (checksum
      compared before/after). No longer one `git add -A` away from leaking.
- [x] **Done 2026-08-14.** 36 compiled `__pycache__` files were also
      git-tracked despite `.gitignore` already listing them (same root
      cause as `.env` — added after the fact, doesn't retroactively
      untrack). Untracked; local files unaffected.
- [ ] **Verified real, not yet fixed.** Contract/lot-size resolution is
      fail-open, not fail-closed: `FNO_LOT_SIZES.get(symbol, 1)` silently
      returns lot size **1** for an unrecognized symbol instead of blocking
      the trade, and `_resolve_contract()`'s own docstring confirms it
      "falls back to build_option_symbol()'s formula... whenever the cache
      is unavailable" rather than refusing to trade. For real capital, "no
      valid contract metadata → reject the trade" is safer than "use a
      computed guess." Worth an explicit decision on which failure modes
      should become fail-closed (option quote unavailable, contract
      metadata unavailable, broker reconciliation unavailable) vs. which
      are genuinely fine to fail-open (dashboard/notifications/optional
      ranking) — the reviewer's fail-open/fail-closed framework is sound,
      but auditing every call site is a real project, not a quick patch.
- [ ] **Partially verified.** Sampled ~7 bare `except Exception: pass`
      blocks in `live_trading_engine.py` — most are correctly scoped to
      informational-only signals (market breadth, matching the "logged for
      visibility, not gated" comments already there). Found one real gap:
      a condor's regime-shift-triggered early exit check silently no-ops
      on a Redis/parse error, meaning that one protective check can go
      dark without any log — bounded impact since SL/profit-target/DTE
      checks still run independently for the same position, but worth a
      log line at minimum.
- [ ] **Not yet investigated.** Backtester realism: `qty=1`, no brokerage/
      slippage/spread modeled, and doesn't appear to share execution logic
      with paper/live (single-leg OHLC signal → price → exit, not real
      option-chain/multi-leg simulation). If true, backtest P&L numbers
      aren't representative of what paper/live would actually produce.
      Haven't opened `src/backtesting/` this session to confirm firsthand.
- [ ] **Not yet investigated.** Multi-leg execution atomicity beyond what
      exists: today's unwind-on-failure logic handles "leg 2 fails to
      place," but not "process crashes between leg 1 filling and leg 2
      being attempted" — a real gap per the reviewer's Test 2 (restart with
      a naked short at the broker that nothing detects). The Zerodha daily
      sync built today (`zerodha_sync.py`) reconciles orders, not
      positions, so it wouldn't catch this specific case.
- [ ] **Not yet investigated.** Reserved-capital / TOCTOU risk manager
      race: two strategies' risk checks could both pass against the same
      available capital before either order fills. Asyncio's single-
      threaded event loop narrows (doesn't eliminate) this vs. a truly
      parallel system — worth understanding exactly which `await` points
      make this reachable before deciding it needs a reservation mechanism.
- [x] **Confirmed correctly separated, no action needed.** ML prediction
      paths are not wired into live signal generation anywhere in
      `src/live_trading/` or `src/strategies/` — matches the reviewer's own
      "keep it that way" recommendation, not a flagged problem.

---

*Last updated: 2026-08-14, after the credit-spread/iron-condor code
review fixes, the external architecture review follow-up, and the
.env/__pycache__ git-tracking fixes.*
