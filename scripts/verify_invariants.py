#!/usr/bin/env python3
"""
Post-deploy invariant checker for Falcon Trader.

Unlike health-check.py (which only checks that API/DB/Redis are UP), this
script re-checks the SPECIFIC bugs found and fixed during the full-system
audit — so a future edit can't silently reintroduce one of them without a
deploy failing loudly instead of being discovered days later from logs.

Two kinds of checks:
  STATIC  — scans the checked-out source on this server for the fixed code
            pattern (or the absence of the broken one). Runs instantly,
            no running services required.
  RUNTIME — queries the live API/Redis of the just-started stack. Some are
            market-hours-aware and SKIP (not FAIL) outside 09:15-15:30 IST,
            since the underlying data legitimately doesn't exist yet.

Usage:
    python3 scripts/verify_invariants.py                  # run on the server
    python3 scripts/verify_invariants.py --repo /home/falcon/trading
    python3 scripts/verify_invariants.py --api http://localhost:8000/api/v1

Exit code 0 = all checks PASS or SKIP. Exit code 1 = at least one FAIL.
Intended to run as the final step of deploy.sh.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Tuple

# Force UTF-8 stdout regardless of the host's default locale (e.g. Windows
# consoles default to cp1252, which can't encode the status icons below).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import requests
except ImportError:
    requests = None

IST = timezone(timedelta(hours=5, minutes=30))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
if sys.stdout.isatty():
    _ICON = {PASS: "\033[92m✓\033[0m", FAIL: "\033[91m✗\033[0m", WARN: "\033[93m!\033[0m", SKIP: "\033[90m-\033[0m"}
else:
    _ICON = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}

Result = Tuple[str, str, str]  # (status, name, detail)


def _read(repo: Path, rel_path: str) -> str:
    return (repo / rel_path).read_text(encoding="utf-8")


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return t >= datetime.strptime("09:15", "%H:%M").time() and t <= datetime.strptime("15:30", "%H:%M").time()


# ── STATIC checks — scan the deployed source tree ───────────────────────────

def check_exit_classification_by_pnl(repo: Path) -> Result:
    name = "Exit profit/adverse classified by net_pnl, not exit_reason keywords"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "_adverse_kw" in src:
        return FAIL, name, "Found reintroduced keyword-based classification (_adverse_kw) — a DTE timeout loss will be miscounted as a profit again."
    hits = len(re.findall(r"if net_pnl < 0:\s*\n\s*(adverse_closes|to_close_adverse_c)\.append", src))
    if hits < 2:
        return FAIL, name, f"Expected net_pnl-based classification in both _check_spread_exits and _check_condor_exits, found {hits}/2."
    return PASS, name, f"net_pnl-based classification present in both exit paths ({hits}/2)."


def check_capital_allocation_keys(repo: Path) -> Result:
    name = "STRATEGY_CAPITAL_ALLOCATION keys match runtime instance ids"
    src = _read(repo, "src/core/constants.py")
    m = re.search(r"STRATEGY_CAPITAL_ALLOCATION\s*=\s*\{([^}]*)\}", src, re.S)
    if not m:
        return FAIL, name, "STRATEGY_CAPITAL_ALLOCATION dict not found."
    body = m.group(1)
    expected = ["ema_crossover_v1", "credit_spread_v1", "iron_condor_v1"]
    missing = [k for k in expected if f'"{k}"' not in body]
    if missing:
        return FAIL, name, f"Missing/renamed keys: {missing}. If these don't match the instance_id StrategyRegistry.load_strategy() uses, the budget check silently never fires."
    return PASS, name, "All three lowercase _v1 instance-id keys present."


def check_ema_state_is_per_symbol(repo: Path) -> Result:
    name = "EMA Crossover state is keyed per-symbol, not shared across its pool"
    src = _read(repo, "src/strategies/ema_crossover.py")
    if re.search(r"self\.prev_fast_ema:\s*Optional\[float\]\s*=\s*None", src):
        return FAIL, name, "Found flat scalar prev_fast_ema again — state will leak between the 5 pool symbols within a cycle."
    if not re.search(r"self\.prev_fast_ema:\s*Dict\[str,\s*float\]\s*=\s*\{\}", src):
        return WARN, name, "Could not confirm the per-symbol Dict[str, float] declaration — check manually."
    if "data.get(\"symbol\"" not in src:
        return FAIL, name, "generate_signal() no longer reads symbol from the tick data — per-symbol keying can't work without it."
    return PASS, name, "prev_fast_ema/prev_slow_ema/_pending_* are per-symbol dicts, generate_signal reads symbol."


def check_eod_notification_guard(repo: Path) -> Result:
    name = "EOD/expiry square-off notification only fires once per day"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "_eod_notified_today" not in src:
        return FAIL, name, "_eod_notified_today flag not found — is_square_off_time() is true for the whole 15:20-15:30 window and _square_off_all() runs every cycle in it, so without this the notification spams every minute."
    guards = len(re.findall(r"and not self\._eod_notified_today", src))
    if guards < 2:
        return FAIL, name, f"Expected the guard on both the expiry-day and normal-day notify calls, found {guards}/2."
    if "self._eod_notified_today = False" not in src:
        return FAIL, name, "Flag is never reset — would stay stuck 'already notified' forever after the first trading day."
    return PASS, name, "Guard present on both notify paths and reset at market open."


def check_dte_rollover_exists(repo: Path) -> Result:
    name = "Fresh credit-spread/iron-condor entries roll to next expiry instead of going dark"
    utils_src = _read(repo, "src/core/utils.py")
    if "def get_entry_expiry" not in utils_src:
        return FAIL, name, "get_entry_expiry() missing from core/utils.py."
    engine_src = _read(repo, "src/live_trading/live_trading_engine.py")
    hits = len(re.findall(r"expiry\s*=\s*get_entry_expiry\(_ENTRY_MIN_DTE\)", engine_src))
    if hits < 2:
        return FAIL, name, f"Expected the rollover call in both _process_credit_spread and _process_iron_condor, found {hits}/2."
    return PASS, name, f"get_entry_expiry() defined and wired into both entry paths ({hits}/2)."


def check_regime_uses_real_market_data(repo: Path) -> Result:
    name = "Regime detector reads live market-wide stats, not the dead NIFTY50 tick"
    src = _read(repo, "src/market_data/regime_detector.py")
    if "tick:NIFTY50" in src or "REDIS_NIFTY_TICK" in src:
        return FAIL, name, "Still references the NIFTY50 tick key that nothing ever writes — regime will silently default to flat/quiet again."
    if "REDIS_TREND_STATS_KEY" not in src or "market:trend_stats" not in src:
        return FAIL, name, "market:trend_stats key not referenced — regime detector has no live data source."
    poller_src = _read(repo, "src/market_data/ltp_poller.py")
    if "market:trend_stats" not in poller_src or "FIVE_MIN_ATR_DAILY_SCALE" not in poller_src:
        return FAIL, name, "LTPPoller no longer publishes market:trend_stats with the daily-ATR scale applied."
    return PASS, name, "regime_detector reads market:trend_stats; LTPPoller publishes it with daily-ATR scaling."


def check_kill_switch_has_reset_path(repo: Path) -> Result:
    name = "Kill switch has a reset path outside of restarting the whole server"
    rm_src = _read(repo, "src/risk/risk_manager.py")
    if "def deactivate_kill_switch" not in rm_src:
        return FAIL, name, "RiskManager.deactivate_kill_switch() missing."
    admin_src = _read(repo, "src/api/routers/admin_router.py")
    if "kill-switch/reset" not in admin_src and "kill_switch/reset" not in admin_src:
        return FAIL, name, "No admin API endpoint to reset the kill switch."
    dash_src = _read(repo, "src/dashboard/app.py")
    if "kill-switch" not in dash_src:
        return WARN, name, "Backend reset endpoint exists but no dashboard control found — only reachable via raw API calls."
    return PASS, name, "Backend reset endpoint + dashboard control both present."


def check_capital_released_on_ema_exit(repo: Path) -> Result:
    name = "EMA Crossover exits release their deployed capital"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    hits = len(re.findall(r"release_deployed_capital\(\s*\n?\s*(info|jrnl|_jrnl_info)\.get\(\"strategy_name\"", src))
    if hits < 3:
        return FAIL, name, f"Expected release_deployed_capital() at all 3 single-leg exit points (normal exit, reversal exit, EOD/expiry square-off), found {hits}/3 — budget would only ever grow and eventually block entries."
    return PASS, name, f"release_deployed_capital() present at {hits}/3 single-leg exit points."


def check_expiry_day_journal_logged(repo: Path) -> Result:
    name = "Expiry-day force-close logs trade_journal exit for spreads/condors"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "_exit_prices" not in src:
        return FAIL, name, "_exit_prices capture missing from _square_off_all — expiry-day closes for spreads/condors would never get an exit_time/pnl in trade_journal."
    hits = len(re.findall(r'exit_reason=f"Expiry day force-close', src))
    if hits < 2:
        return FAIL, name, f"Expected the expiry-day journal-close call for both spreads and condors, found {hits}/2."
    return PASS, name, "Expiry-day journal close logged for both structure types."


STATIC_CHECKS: List[Callable[[Path], Result]] = [
    check_exit_classification_by_pnl,
    check_capital_allocation_keys,
    check_ema_state_is_per_symbol,
    check_eod_notification_guard,
    check_dte_rollover_exists,
    check_regime_uses_real_market_data,
    check_kill_switch_has_reset_path,
    check_capital_released_on_ema_exit,
    check_expiry_day_journal_logged,
]


# ── RUNTIME checks — query the live, just-started stack ─────────────────────

def _get(api_base: str, path: str, timeout: float = 5.0):
    r = requests.get(f"{api_base}/{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def check_api_stack_up(api_base: str) -> Result:
    name = "API / DB / Redis are up"
    try:
        data = _get(api_base, "health")
    except Exception as e:
        return FAIL, name, f"Health endpoint unreachable: {e}"
    bad = [k for k in ("status", "database", "redis") if not str(data.get(k, "")).startswith(("UP", "ok", "OK"))]
    if bad:
        return FAIL, name, f"Not healthy: {data}"
    return PASS, name, f"status={data.get('status')} database={data.get('database')} redis={data.get('redis')}"


def check_three_strategies_registered(api_base: str) -> Result:
    name = "All 3 strategies registered (ema_crossover_v1, credit_spread_v1, iron_condor_v1)"
    try:
        data = _get(api_base, "strategies")
    except Exception as e:
        return FAIL, name, f"/strategies unreachable: {e}"
    ids = {s.get("id") for s in data}
    expected = {"ema_crossover_v1", "credit_spread_v1", "iron_condor_v1"}
    missing = expected - ids
    if missing:
        return FAIL, name, f"Missing: {missing}. Engine may not have finished starting, or a strategy failed to load."
    return PASS, name, f"Registered: {sorted(ids)}"


def check_kill_switch_endpoint(api_base: str) -> Result:
    name = "Kill-switch status endpoint reachable and well-formed"
    try:
        data = _get(api_base, "admin/kill-switch")
    except Exception as e:
        return FAIL, name, f"/admin/kill-switch unreachable: {e}"
    if "active" not in data:
        return FAIL, name, f"Response missing 'active' field: {data}"
    if data["active"]:
        return WARN, name, f"Kill switch is currently ACTIVE — reason: {data.get('reason')}. Not a deploy failure, but new entries are blocked until reset."
    return PASS, name, "Reachable, inactive."


def check_regime_data_is_live(api_base: str) -> Result:
    name = "Regime classification is using live market data, not frozen defaults"
    if not _is_market_hours():
        return SKIP, name, "Outside market hours (09:15-15:30 IST Mon-Fri) — no live tick data expected."
    try:
        data = _get(api_base, "analytics/market-regime")
    except Exception as e:
        return FAIL, name, f"/analytics/market-regime unreachable: {e}"
    atr = data.get("market_atr_pct")
    ema_spread = data.get("market_ema_spread")
    if atr is None:
        return WARN, name, f"No regime data yet this run: {data}"
    # The old bug's exact frozen fallback values were 1.0 / 0.15 — flag an exact
    # match as suspicious (real data essentially never lands on these precisely).
    if atr == 1.0 and ema_spread == 0.15:
        return WARN, name, "ATR%=1.00 EMA_spread%=0.15 exactly — matches the old hardcoded fallback. Possibly coincidence, but worth a manual look if it persists past the next cycle."
    return PASS, name, f"regime={data.get('regime')} atr%={atr} ema_spread%={ema_spread}"


def check_no_duplicate_eod_notifications(repo: Path) -> Result:
    """
    Flags rapid-fire repeats (the actual bug pattern: same notification re-sent
    every cycle for the whole 15:20-15:30 window), not just ">1 per calendar
    day" — a fix deployed mid-window on the day the bug was caught will always
    show pre-fix sends earlier that same day, which is history, not a live
    regression. Two sends under 15 minutes apart is the real signal.
    """
    name = "No rapid-fire duplicate EOD notifications"
    log_path = repo / "logs" / "falcon.log"
    if not log_path.exists():
        return SKIP, name, "falcon.log not found at expected path."
    timestamps: List[datetime] = []
    try:
        with log_path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "Email sent: EOD POSITION UPDATE" not in line:
                    continue
                m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if m:
                    timestamps.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        return WARN, name, f"Could not read log: {e}"
    timestamps.sort()
    for a, b in zip(timestamps, timestamps[1:]):
        if (b - a) < timedelta(minutes=15):
            return FAIL, name, f"Two sends only {int((b - a).total_seconds())}s apart ({a} -> {b}) — the once-per-day guard may not be working."
    return PASS, name, f"{len(timestamps)} EOD notification(s) in the log, none within 15 min of each other."


RUNTIME_CHECKS: List[Callable[[str], Result]] = [
    check_api_stack_up,
    check_three_strategies_registered,
    check_kill_switch_endpoint,
    check_regime_data_is_live,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="Path to the checked-out repo (default: current directory)")
    parser.add_argument("--api", default="http://localhost:8000/api/v1", help="API base URL")
    parser.add_argument("--skip-runtime", action="store_true", help="Only run static source checks")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    results: List[Result] = []

    print(f"Falcon Trader — invariant check\nRepo: {repo}\nTime: {datetime.now(IST).isoformat()}\n")

    print("── Static (source) checks ──────────────────────────────")
    for check in STATIC_CHECKS:
        try:
            status, name, detail = check(repo)
        except FileNotFoundError as e:
            status, name, detail = FAIL, check.__name__, f"File not found: {e}"
        except Exception as e:
            status, name, detail = FAIL, check.__name__, f"Check crashed: {e}"
        results.append((status, name, detail))
        print(f"  {_ICON[status]} [{status}] {name}\n        {detail}")

    status, name, detail = check_no_duplicate_eod_notifications(repo)
    results.append((status, name, detail))
    print(f"  {_ICON[status]} [{status}] {name}\n        {detail}")

    if not args.skip_runtime:
        print("\n── Runtime (live stack) checks ──────────────────────────")
        if requests is None:
            print("  ! 'requests' not installed — skipping runtime checks. pip install requests")
        else:
            for check in RUNTIME_CHECKS:
                try:
                    status, name, detail = check(args.api)
                except Exception as e:
                    status, name, detail = FAIL, check.__name__, f"Check crashed: {e}"
                results.append((status, name, detail))
                print(f"  {_ICON[status]} [{status}] {name}\n        {detail}")

    n_pass = sum(1 for s, _, _ in results if s == PASS)
    n_warn = sum(1 for s, _, _ in results if s == WARN)
    n_skip = sum(1 for s, _, _ in results if s == SKIP)
    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{n_pass} passed, {n_warn} warnings, {n_skip} skipped, {n_fail} failed.")

    if n_fail:
        print("\nFAILED — a previously-fixed bug pattern may have been reintroduced, or the deploy didn't start cleanly. See details above.")
        return 1
    if n_warn:
        print("\nPassed with warnings — review above, no action required unless something looks wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
