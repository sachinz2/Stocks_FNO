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

- [ ] Recompute total realized P&L across every historical trade using real
      fill prices (same methodology as the credit_spread/iron_condor
      backfill just done) — confirm the strategy is genuinely net profitable,
      not just reported as such.
- [ ] Run at least 2-3 more weeks of paper trading **on the fully-fixed
      code** (today's fixes onward) before counting it toward the 45-day
      gate — the old track record was earned on a different, buggier system.
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
`MAX_DAILY_LOSS_PCT=0.05`, `MAX_EXPOSURE_PCT=0.20`.

- [ ] Confirm `INITIAL_CAPITAL` matches the actual funded Zerodha account
      balance at go-live time, not a stale placeholder.
- [ ] Explicitly decide (don't inherit by default) whether `MAX_DAILY_LOSS_PCT`
      (5%) and `MAX_EXPOSURE_PCT` (20%) are still the right numbers for real
      capital — paper-mode risk tolerance and real-money risk tolerance are
      not obligated to match.
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
- [ ] Server hardening check: root SSH still reachable directly (used
      throughout this session for deploys) — confirm this is acceptable for
      a box now handling live trading, or lock it down further.
- [ ] Log retention: currently rotates out at ~1 month. Confirm that's
      enough for compliance/audit needs once real trades are involved.

## 6. Testing & CI safety net

- [ ] Full test suite green (currently 209 passing) immediately before flip.
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

---

*Last updated: 2026-08-13, after the credit_spread/iron_condor fill-price
backfill and the entry-side fix + cross-strategy collision guard.*
