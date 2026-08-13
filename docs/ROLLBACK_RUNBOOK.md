# Live Trading Rollback Runbook

What to actually do if something goes wrong after go-live. Grounded in the
real controls that exist today (checked 2026-08-13) — not generic advice.

Server: `root@46.62.198.238`, app at `/home/falcon/trading` (as `falcon` user).
Dashboard: `http://<server-ip>:8501`. API: `http://<server-ip>:8000/api/v1`.

**Golden rule: Zerodha's own app/website is always the fastest, most
certain way to close a real position or check real account state.** Every
option below is a layer on top of that, not a replacement for it. If in
doubt or the system seems unresponsive, close positions directly in
Zerodha first, investigate this system second.

---

## Decision tree

**"Something looks off, I'm not sure yet, but don't want new risk added"**
→ Step 1 (pause strategies). Existing positions keep being watched by
exit logic — this is the safest, least destructive first move.

**"I need a specific position closed right now"**
→ Step 2 (close a position), or Zerodha app directly if faster.

**"I need ALL real trading stopped immediately, not just new entries"**
→ Step 3 (flip to paper mode). Existing positions still aren't
auto-closed — decide separately whether to close them too.

**"The system itself seems broken/unresponsive/doing something wrong"**
→ Close everything in Zerodha directly FIRST, then Step 4 (stop the
app). Do not stop the app while real positions are open and unwatched
unless you've already secured them at the broker.

---

## Step 1 — Pause strategies (stops new entries, keeps exit monitoring running)

Safest, most reversible first move. Existing open positions are
untouched and still actively monitored (stop-loss/profit-target/DTE exit
checks keep running) — only NEW entries are blocked.

**Dashboard:** Strategy Management page → toggle off the strategy(ies).

**API** (per strategy — `ema_crossover_v1`, `momentum_v1`,
`credit_spread_v1`, `iron_condor_v1`):
```bash
curl -X POST http://localhost:8000/api/v1/strategies/deactivate \
  -H "Content-Type: application/json" \
  -d '{"strategy_id": "credit_spread_v1"}'
```
To pause everything, repeat for all 4 strategy_ids.

**Reverse:** `POST /strategies/activate` with the same body.

## Step 2 — Manually close a specific position

```bash
curl -X POST http://localhost:8000/api/v1/admin/positions/{contract}/close
```
`{contract}` is the exact tradingsymbol (e.g. `TITAN26SEP4650PE`). Use
the Positions page on the dashboard to find the exact symbol first.

If this doesn't respond quickly or you're not confident it worked,
**close it directly in the Zerodha app instead** — don't wait on this
system to confirm.

## Step 3 — Flip out of live mode (stops ALL new real order placement)

This stops the engine from placing any new real orders, but does **not**
auto-close existing open positions at the broker — GTT backstops (if any
were placed on short legs) remain active at the exchange independently,
which is the point of them, but decide separately whether to manually
close everything too (Step 2, or Zerodha directly).

```bash
ssh root@46.62.198.238
sed -i 's/^TRADING_MODE=live/TRADING_MODE=paper/' /home/falcon/trading/.env
su - falcon -c 'cd /home/falcon/trading && docker compose up -d api'
```
Verify: `curl http://localhost:8000/api/v1/health` and check the
dashboard's mode indicator. Confirm no new orders are being placed by
watching `docker exec falcon_api tail -f /app/logs/falcon.log`.

**To resume live trading later:** reverse the sed (paper→live), restart,
and — importantly — re-run the invariant check first:
```bash
python3 scripts/verify_invariants.py --repo /home/falcon/trading --api http://localhost:8000/api/v1
```

## Step 4 — Full stop (last resort)

Stops every scheduled job, including exit monitoring. **Only do this
after real positions are already secured (closed, or you've accepted
they're only protected by exchange-level GTT backstops with nothing else
watching them.)**

```bash
ssh root@46.62.198.238
su - falcon -c 'cd /home/falcon/trading && docker compose stop api dashboard'
```
This leaves MySQL/Redis running (state preserved) but nothing acting on
it. `docker compose up -d api dashboard` to resume.

---

## After any rollback action

1. Send yourself a note of what happened and why (the EOD report/
   notification pipeline won't capture "I manually paused things at
   14:32" — write it down somewhere, even just a text file on the
   server or a note to yourself).
2. Check `docker exec falcon_mysql mysql -uroot -ppassword123 falcon_db
   -e "SELECT * FROM trade_journal WHERE exit_time IS NULL"` to see
   exactly what's still open.
3. Before resuming normal operation, re-run
   `scripts/verify_invariants.py` against the live stack.
4. If the rollback was due to a real bug (not a false alarm), fix it,
   write a test for it (matching this project's established pattern:
   root-cause the fix, add a regression test, deploy, verify), before
   resuming.

---

*Written 2026-08-13 as part of go-live preparation. Update this if any
of the referenced endpoints/commands change.*
