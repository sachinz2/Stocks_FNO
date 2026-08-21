# Falcon Trader — Strategy Documentation

**Version:** 2.3  
**Last Updated:** 2026-07-03  
**Platform Version:** 3.2  

---

## Table of Contents

1. [Overview — Three-Strategy Architecture](#1-overview)
2. [Market Data & Signal Infrastructure](#2-market-data--signal-infrastructure)
3. [Risk Filters (Common to All Strategies)](#3-risk-filters)
4. [Strategy 1: EMA Crossover (Momentum Long Options)](#4-strategy-1-ema-crossover)
5. [Strategy 2: Credit Spread (Theta Collection, Directional)](#5-strategy-2-credit-spread)
6. [Strategy 3: Iron Condor (Theta Collection, Range-Bound)](#6-strategy-3-iron-condor)
7. [Daily Cycle — How Everything Fits Together](#7-daily-cycle)
8. [Multi-Day Holding — Loss Mitigation Controls](#7a-multi-day-holding--loss-mitigation-controls)
9. [Position Sizing & Margin Rules](#8-position-sizing--margin-rules)
10. [Execution & Slippage Model](#9-execution--slippage-model)
11. [Trade Journal Schema](#10-trade-journal-schema)
12. [Email Alerts Reference](#11-email-alerts-reference)
13. [Parameters Quick Reference](#12-parameters-quick-reference)
14. [Strategy Performance Metrics](#13-strategy-performance-metrics)
15. [Assumptions & Limitations](#14-assumptions--limitations)

---

## 1. Overview

The platform runs three strategies simultaneously on the same set of F&O symbols. They are designed to **never conflict** — each fires in a different market regime:

| Regime | ATR% | EMA spread% | Strategy Used |
|--------|------|-------------|---------------|
| Explosive move / trending | ≥ 1.2% | Any direction | **EMA Crossover** — BUY options directionally |
| Mild trend | < 1.2% | ≥ 0.1% (trending) | **Credit Spread** — collect premium from one side |
| Flat / sideways | < 1.2% | < 0.1% (flat) | **Iron Condor** — collect premium from both sides |

Only one strategy will fire signals for a given stock at any given time. If market conditions don't match any regime cleanly, all three return HOLD.

**Symbols traded:** 41 NSE F&O equity symbols. Top 5 per strategy are ranked dynamically each cycle from the full pool.  
**Instruments:** NSE F&O equity options (near-month expiry, lot-size basis).  
**Execution:** Signal cycle runs every 60 seconds, 9:15 AM – 3:30 PM IST. Exit checks run every 10 seconds in parallel.  
**Hold period:**
- EMA Crossover — intraday only (closed by 3:20 PM)
- Credit Spread / Iron Condor — **multi-day** — held overnight until exit conditions trigger (DTE < 7, profit target, stop loss, or delta breach). EOD square-off does NOT apply to these structures.

---

## 2. Market Data & Signal Infrastructure

### 2.1 Data Sources (3-Tier)

| Tier | Source | Latency | Used For |
|------|--------|---------|---------|
| 1 | Zerodha WebSocket (ZerodhaTicker) | Real-time tick | LTP pushed to Redis; option contract prices |
| 2 | Zerodha REST LTP poll (ZerodhaLTPPoller) | Every 5 seconds | Fallback when WebSocket unavailable |
| 3 | Zerodha 5-min OHLC + indicators (LTPPoller) | Every 60 seconds | EMA20, EMA50, ATR14, ADX14, RVOL, VWAP computed from 5-min candles |

### 2.2 Indicator Timeframe — 5-Minute Candles

The LTPPoller fetches **10 days of 5-minute OHLC candles** from Zerodha (`kite.historical_data`, interval `"5minute"`). All indicators are computed on these bars and recomputed every 60 seconds.

| Indicator | Coverage |
|-----------|----------|
| EMA20 | 20 bars × 5 min = ~100 intraday minutes |
| EMA50 | 50 bars × 5 min = ~250 min ≈ 4.2 hours (~2/3 of a trading day) |
| ATR14 | 14 bars × 5 min = ~70 minutes |

Signals **can and do change intraday** as new 5-minute bars close.

### 2.3 Indicators Computed and Used

| Indicator | Computation | Used By |
|-----------|-------------|---------|
| **EMA20** | 20-bar EWM (span=20, adjust=False) on 5-min close | All 3 strategies |
| **EMA50** | 50-bar EWM (span=50, adjust=False) on 5-min close | All 3 strategies |
| **ATR14** | 14-bar Wilder's EWM (α=1/14, adjust=False) on True Range of 5-min bars | All 3 strategies |
| **ATR%** | ATR14 ÷ close × 100 — volatility as % of price | Regime filter |
| **EMA spread%** | abs(EMA20 − EMA50) ÷ EMA50 × 100 — trend strength | Regime filter, iron condor entry |
| **ADX14** | Wilder's Average Directional Index (α=1/14) on 5-min bars | Credit spread and iron condor entry filters |
| **RVOL** | Current bar volume ÷ 20-bar simple average volume | EMA Crossover entry filter |
| **VWAP** | Volume-weighted average price from 10 days of 5-min candles (~750 bars) | Credit spread VWAP alignment filter |
| **Market Breadth** | Advancing symbols ÷ (advancing + declining) across all 41 symbols | Credit spread and iron condor entry filters |
| **PCR** | Put-Call Ratio from NSE OI data (per-symbol) | Credit spread direction filter; iron condor neutrality filter |
| **IV Rank** | Current implied vol vs 52-week IV range [0–1] | Risk layer 3 (all premium-selling entries) |
| **VIX** | India VIX from Zerodha | Risk layer 3; VIX spike exit trigger |
| **EMA20 (15-min)** | 20-bar EWM on 15-min close — fetched separately | MTF confirmation for EMA Crossover |
| **EMA50 (15-min)** | 50-bar EWM on 15-min close | MTF confirmation for EMA Crossover |

**ATR14** uses **Wilder's exponential smoothing** (α = 1/14, adjust=False) — the same smoothing convention as ADX14. This is the standard Wilder's ATR, not a simple rolling mean.

**Market Breadth** is computed once per 60-second LTP poll cycle. It counts all 41 symbols where `close > prev_close` (advancing) and `close < prev_close` (declining), then publishes `advancing / (advancing + declining)` to Redis key `market:breadth` with a 2-minute TTL.
- Breadth > 0.65 → broad market advancing (bullish)
- Breadth < 0.35 → broad market declining (bearish)
- 0.35–0.65 → neutral

**15-min OHLC** is fetched from Zerodha (`kite.historical_data`, interval `"15minute"`, 30 days of history) and cached in Redis key `tick15:{SYMBOL}` with a 30-minute TTL. EMA20 and EMA50 are computed on these candles for MTF confirmation.

### 2.4 Symbol Scoring (Three Separate Pools)

Every 60 seconds, the LTPPoller ranks all 41 symbols into three pools and publishes the top 5 from each to Redis:

| Pool | Redis Key | Scoring Formula |
|------|-----------|----------------|
| EMA Crossover | `nfo:top5` | ATR% × 0.6 + EMA\_spread% × 0.4 — rewards volatile, trending stocks |
| Credit Spread | `nfo:top5:spread` | (1.2 − ATR%) × 0.4 + EMA\_spread% × 0.6 — rewards gentle trend, low vol |
| Iron Condor | `nfo:top5:condor` | (1.2 − ATR%) × 0.6 + (0.1 − EMA\_spread%) × 0.4 — rewards flat, stable stocks |

Each strategy reads only from its own pool. EMA Crossover always operates on the most volatile stocks, Iron Condor on the most range-bound. The EMA\_spread% denominator in all three formulas is **EMA50** (not close price).

### 2.5 Event / Earnings Calendar Filter

**Files:** `src/market_data/event_calendar.py`, `src/market_data/calendar_refresh.py`

Entry signals for Credit Spreads and Iron Condors are blocked within **5 calendar days** of a scheduled corporate event or macro event. EMA Crossover is not blocked by the calendar.

**Data sources (priority order):**
1. Redis key `event:calendar` — JSON dict `{SYMBOL: ["YYYY-MM-DD", ...], "*": [...]}`. Updated every Monday at market open.
2. `config/event_calendar.json` — static fallback / manual override file.

**Auto-refresh:** Every Monday at 9:15 AM the engine fires `_refresh_event_calendar()` as a background task. It fetches upcoming results dates from two NSE API endpoints (`/api/event-calendar` and `/api/corporates-corporateActions`) and merges with hardcoded RBI MPC dates under the `"*"` key. Both Redis and the JSON file are updated on success. If NSE is unreachable, the existing calendar is left intact.

**Hardcoded market-wide dates (FY 2026-27):**
- RBI MPC: Aug 7, Oct 8, Dec 5 2026; Feb 5 2027
- Union Budget: Feb 1 2027

**Blocking logic:** `has_event_within_days(symbol, redis, days=5)` returns True if any date in the symbol's list (or the `"*"` global list) falls within 5 days of today. Entry is silently skipped. Returns False on Redis error (fail-open — data issues don't halt trading).

### 2.6 Stale Data Circuit Breaker

Market data older than **90 seconds** is rejected. `_get_market_data()` checks the `timestamp` field in the Redis tick. If the tick is stale (or missing), it returns `None` and no signal is generated for that symbol.

The timestamp is stored as a naive local-time (IST) string by LTPPoller. The stale check compares `datetime.now() - timestamp` (both naive IST), avoiding timezone arithmetic errors.

This protects against LTP poller lag, Redis outages, and stale previous-session data in the 9:15–9:30 warm-up window.

### 2.7 Option Premium Estimation

In **paper mode**, option premiums are estimated using an ATR-based model:

```
estimated_premium = ATR14_daily × sqrt(DTE / 252) × OTM_discount
```

where `ATR14_daily = ATR14_5min × sqrt(75)` (converts 5-min ATR to daily equivalent: `sqrt(375 min/day ÷ 5 min/bar) = sqrt(75) ≈ 8.66`).

OTM discount by interval from ATM:
- 0 intervals (ATM): 100%
- 1 interval OTM: ~65%
- 2 intervals OTM: ~42%
- 3 intervals OTM: ~27%

In **live mode**, actual LTP is fetched from Zerodha `kite.ltp()` for all open option contracts every cycle. ATR estimates are not used for live fills.

### 2.8 Strike Selection (Delta-Based)

Strikes are selected using the Black-Scholes delta model:
- `find_delta_strike()` — finds the strike where the option has a target delta
- Short legs target **δ ≈ 0.20** (≈80% probability of expiring worthless)
- Long legs (hedges) target **δ ≈ 0.10** (further OTM, cheaper hedge)

ATM strike is rounded to the symbol's standard interval (e.g. RELIANCE: ₹50, HDFCBANK: ₹20, TCS: ₹100).

**Volatility input — two-step process:**

**Step 1 (baseline):** Convert 5-min ATR14 to annualised realized vol:
```
_atr_sigma = atr_to_annualised_vol(ATR14_5min × sqrt(75), underlying_price)
             # Typical result: 25–30% for F&O stocks
```

**Step 2 (live mode upgrade):** `_get_live_sigma()` fetches ATM CE and PE quotes from Zerodha, solves their implied vols, and returns the average. This ensures delta targets are met against the actual market pricing surface:
```
sigma = _get_live_sigma(symbol, price, dte, interval, expiry, fallback=_atr_sigma)
        # Falls back to _atr_sigma in paper mode or if Zerodha unavailable
```

`_atr_sigma` is retained as the **realized vol baseline** for the HV/IV ratio filter (§5.2 condition 12). Using live IV in that denominator would measure volatility skew instead of the vol risk premium.

### 2.9 Market Open Warm-Up

A 15-minute warm-up window blocks all **entry** signals until 9:30 AM. The previous session's 5-min bars can produce misleading EMA/ATR readings at the open. Exit checks and position management run throughout warm-up.

```
9:15 AM  — Market opens. Exit checks active. Entries blocked.
9:16–9:29 — Each new 5-min bar dilutes the previous-session data.
9:30 AM  — Warm-up complete. Entry signals enabled.
```

---

## 3. Risk Filters

Every order passes through a multi-layer risk manager before placement. Exit orders bypass most layers.

### 3.1 Risk Layers

| Layer | Check | Applies To |
|-------|-------|-----------|
| 1 | **Kill switch** — if activated, all orders rejected | All orders |
| 2 | **Daily PnL limit** — combined realized + unrealized loss ≥ 5% of capital: stop trading | All orders |
| 3 | **IV Rank / VIX gate** — spread/condor entries blocked when options too cheap | Entry (spread/condor) only |
| 4 | **Sector concentration** — max 2 positions per sector | New entry only |
| 5 | **Per-strategy capital budget** — each strategy has a fixed allocation | Entry only |
| 6 | **Max open positions** — hard cap at 25 total legs | Entry only |
| 7 | **BUY exposure limit** — long option premium capped at 20% of capital per trade | Entry (BUY) only |

Exit orders (`is_exit_order=True`) skip layers 3–7. Spread/condor hedge legs (`is_spread_leg=True`) also skip 3–7 to prevent the hedge from being independently rejected.

### 3.2 Portfolio-Level PnL Cap (Layer 2)

```python
total_daily_pnl = daily_realized_pnl + daily_unrealized_pnl
max_allowed_loss = -(initial_capital × 5%)
if total_daily_pnl ≤ max_allowed_loss → kill switch activated
```

With ₹3,00,000 capital, cap = ₹15,000/day. Unrealized losses count — a single large open loss can trigger the cap before any position closes.

**In paper mode**, `broker.get_positions()` returns zero unrealized PnL. The engine instead computes unrealized PnL directly from its in-memory active spreads and condors by reading current option prices from the Redis cache (`optltp:{CONTRACT}` or `optq:{CONTRACT}`). This ensures the daily loss circuit breaker fires correctly in paper trading.

When the daily loss limit triggers, the kill switch activates automatically. **Exit orders are always allowed through regardless of kill switch state.**

### 3.3 IV Rank / VIX Gate (Layer 3)

Applies to Credit Spreads and Iron Condors only (premium-selling strategies).

| Check | Threshold | Action |
|-------|-----------|--------|
| India VIX | < **12.0** | Skip entry — market-wide IV too low, premiums too cheap |
| IV Rank (per-symbol) | < **0.30** | Skip entry — symbol-specific IV too low |

EMA Crossover has no IV gate — it buys options and benefits from low-IV environments before a move.

If VIX is unavailable (None), the check passes (fail-open). If IV Rank is unavailable, it also passes.

### 3.4 Per-Strategy Capital Budget (Layer 5)

Each strategy has a fixed budget as a percentage of initial capital (defined in `STRATEGY_CAPITAL_ALLOCATION`). The risk manager tracks BUY-side capital deployed per strategy in `_strategy_deployed`.

On each market open (`on_market_open`), after `reset_daily_state()` clears this tracker, the engine **rebuilds `_strategy_deployed`** from all overnight active spreads and condors:
- Spread: add `long_premium × lot_size` for the strategy
- Condor: add `(put_long_premium + call_long_premium) × lot_size` for the strategy

This ensures that the per-strategy budget check accurately reflects overnight multi-day positions and prevents over-allocation on day 2+ of a multi-day position.

### 3.5 End-of-Day Square-Off

At **3:20 PM IST**, the engine closes **EMA Crossover single-leg positions only**. All open single-leg options are sold at the current estimated premium.

**Credit spreads and iron condors are NOT closed at 3:20 PM.** They are multi-day theta strategies designed to hold overnight. They close only when their own exit conditions trigger:
- DTE falls below 7 (gamma risk)
- Underlying breaches a short strike (emergency stop)
- Short leg doubles (stop loss)
- Short leg decays to tiered profit target
- Short leg delta exceeds |0.40| (delta breach)

Closing spreads/condors intraday would forfeit most of the expected theta profit. A spread opened with 25 DTE may not reach its profit target for 10–15 days.

---

## 4. Strategy 1: EMA Crossover

**File:** [src/strategies/ema_crossover.py](../src/strategies/ema_crossover.py)  
**Registered as:** `EMA_CROSSOVER`  
**Strategy ID:** `ema_crossover_v1`

### 4.1 Concept

Buys a **single-leg option** in the direction of a confirmed EMA crossover on 5-minute bars. When EMA20 crosses above EMA50 → BUY a Call option. When it crosses below → BUY a Put option (reversal).

This strategy **buys** premium. It benefits from a strong directional move after entry and is used in high-volatility regimes (ATR% ≥ 1.2%) where the move is expected to overcome theta decay.

### 4.2 Entry Conditions

**Step 1 — Crossover detection (on 5-min EMA values):**
- **BUY signal:** EMA20 was ≤ EMA50 last cycle AND EMA20 > EMA50 this cycle
- **SELL signal:** EMA20 was ≥ EMA50 last cycle AND EMA20 < EMA50 this cycle

**Step 2 — 2-bar confirmation:**

The crossover must persist across **2 distinct completed 5-minute bars**. The engine tracks `ohlc_bar_key` (timestamp of the last completed 5-min candle). The pending count only increments when the bar key changes — multiple engine cycles within the same 5-min candle do not count as multiple bars. If the crossover reverses before 2 confirmations, the count resets.

```
Bar 1 closes with EMA20 above EMA50 → pending BUY (1/2)
Bar 2 closes with EMA20 still above EMA50 → CONFIRMED → BUY order placed
```

**Step 3 — Duplicate prevention:**  
If a CE (or PE) option for the symbol is already open, a new BUY of the same type is skipped. Any opposite-type option is closed first (reversal exit), then the new position checked.

**Step 4 — RVOL filter (Relative Volume ≥ 1.3):**  
RVOL = current bar volume ÷ 20-bar average volume. A crossover on below-average volume is more likely a false breakout. A RVOL of 0 (indicator not yet available) bypasses this check.

**Step 5 — ADX trend strength filter (ADX ≥ 25):**  
ADX14 < 25 indicates a ranging market where the crossover is likely noise. A value of 0 bypasses this check.

**Step 6 — Multi-Timeframe (MTF) confirmation:**  
The 15-minute EMA direction must agree with the 5-minute signal:
- BUY (5-min EMA20 > EMA50): requires 15-min EMA20 > 15-min EMA50
- SELL (5-min EMA20 < EMA50): requires 15-min EMA20 < 15-min EMA50

If 15-min data is unavailable, the MTF check is skipped (no block). If available and contradicts the signal, entry is skipped.

**Step 7 — DTE range:**  
Entry only when 10 ≤ DTE ≤ 25.

**Step 8 — Market open warm-up:**  
No entries before 9:30 AM.

**LTP Poller registration:**  
After a successful BUY order, the option contract is immediately registered with the LTPPoller for 5-second real-time price tracking. On platform restart, all contracts in `_single_leg_journals` (restored from Redis) are re-registered with the LTPPoller during `attach_ltp_poller()`.

### 4.3 What Gets Bought

| Parameter | Value |
|-----------|-------|
| Option type | CE if BUY signal, PE if SELL signal |
| Strike | ATM — rounded to symbol's standard interval |
| Expiry | Near-month |
| Quantity | 1 lot (symbol-specific lot size) |
| Entry price | ATM premium estimate (ATR model) or live LTP |

### 4.4 Entry Example

```
RELIANCE @ ₹2,850  |  ATR14 (5-min) = ₹45  →  ATR% = 1.58%  ≥ 1.2%  ✓
EMA20 (5-min) = 2,855 crossed above EMA50 = 2,840 (confirmed 2 bars)
RVOL = 1.6  ✓  |  ADX14 = 28  ✓  |  15-min EMA20 > 15-min EMA50  ✓

→ BUY  RELIANCE25JUN2850CE  @ ₹62  (1 lot × 250 shares)
  Total outlay: ₹15,500
```

### 4.5 Exit Conditions (Priority Order)

#### Exit 1: DTE < 4 days
Forced close to avoid gamma explosion. Overrides all other checks.

#### Exit 2: Hard Stop Loss
Premium has fallen ≥ 50% from entry.
```
Entry: ₹62 → Stop at: ₹31 (≤ ₹31: EXIT)
Max loss: ₹31 × 250 = ₹7,750
```

#### Exit 3: Profit Target
Premium has risen ≥ 100% from entry (doubled).
```
Entry: ₹62 → Target at: ₹124 (≥ ₹124: EXIT)
Profit: ₹62 × 250 = ₹15,500
```

#### Exit 4: Trailing Stop
Activates only after the position has been profitable. Tracks the peak premium seen since entry. Exits if premium falls ≥ 25% from the peak.
```
Peak premium: ₹95
Trailing stop: ₹95 × 75% = ₹71.25
If current < ₹71.25: EXIT
```

The actual fill price (`order.avg_price`) is used to compute PnL and `total_slippage_pts` in the trade journal, rather than the estimated price.

### 4.6 Risk/Reward Profile

| Scenario | P&L per lot |
|----------|------------|
| Stop loss hit (50% premium loss) | −₹7,750 |
| Profit target hit (100% premium gain) | +₹15,500 |
| Trailing stop after 53% gain | +₹8,060 |

---

## 5. Strategy 2: Credit Spread

**File:** [src/strategies/credit_spread.py](../src/strategies/credit_spread.py)  
**Registered as:** `CREDIT_SPREAD`  
**Strategy ID:** `credit_spread_v1`

### 5.1 Concept

Sells premium upfront, collects it as theta decays. Two-leg defined-risk structure:
- **Bull Put Spread** — mild uptrend: SELL OTM put + BUY further-OTM put. Profit if stock stays above short strike.
- **Bear Call Spread** — mild downtrend: SELL OTM call + BUY further-OTM call. Profit if stock stays below short strike.

### 5.2 Entry Conditions (All Must Be Met)

| # | Condition | Threshold / Logic |
|---|-----------|-------------------|
| 1 | ATR% below volatility threshold | < 1.2% |
| 2 | EMA is directional (not flat) | EMA spread% ≥ 0.1% |
| 3 | No existing spread for this symbol | Must be flat |
| 3a | No existing condor for this symbol | Stacking guard |
| 4 | **Minimum DTE (fresh entry)** | **≥ 21 days** |
| 4a | **Minimum DTE (re-entry after same-day profit close)** | **≥ 14 days** |
| 4b | Not in `_exited_today` | Adverse exits (breach/SL) block same-day re-entry entirely |
| 5 | **PCR alignment** | BULL_PUT: PCR ≥ 0.8. BEAR_CALL: PCR ≤ 1.2. Fails-open if PCR unavailable. |
| 6 | Short strike not at crowded OI | Moves 1 interval further OTM if crowded |
| 7 | Absolute net credit minimum | ≥ ₹350 total per spread |
| 8 | Net credit ≥ 20% of wing width | Guards risk/reward ratio |
| 9 | Margin available | ≈ (short − long strike) × lot_size |
| 10 | **VIX ≥ 12.0 AND IV Rank ≥ 0.30** | Premium must be worth selling |
| 11 | **VWAP alignment** | BULL_PUT: price ≥ VWAP × 0.995. BEAR_CALL: price ≤ VWAP × 1.005 |
| 12 | **HV/IV ratio ≥ 1.10** | Market IV (from live short-leg price) ÷ realized HV (ATR sigma) ≥ 1.10 |
| 13 | **Market Breadth alignment** | BEAR_CALL blocked when breadth > 0.65 (advancing market). BULL_PUT blocked when breadth < 0.35 (declining market). Neutral 0.35–0.65 allows both. |
| 14 | **ADX range filter** | 15 ≤ ADX14 ≤ 30. Below 15: no trend (condor territory). Above 30: too strong (blowthrough risk). A value of 0 bypasses this check. |
| 15 | **Event calendar** | No earnings / RBI MPC / Budget within 5 calendar days |
| 16 | Price not already inside short strike | BULL_PUT: price must be > short put strike. BEAR_CALL: price must be < short call strike. |

**Condition 5 (PCR):** PCR ≥ 0.8 for BULL_PUT means the market is not strongly bearish in put/call positioning — there is no unusually heavy hedging demand that would suggest the down move is expected. PCR ≤ 1.2 for BEAR_CALL means the market has not turned heavily bullish in a way that would fight a call spread.

**Condition 10 (VIX threshold 12.0):** Below VIX 12, premiums are too cheap market-wide regardless of per-symbol IV Rank. Between VIX 12–14, per-symbol IV Rank is the decisive filter. Both must pass.

**Condition 11 (VWAP):** VWAP is computed over 10 days of 5-min candles — a medium-term trend anchor, not intraday. A BULL_PUT selling puts requires the stock to be at or above VWAP (bullish context). Trading against the 10-day VWAP means the short put has a higher chance of being tested.

**Condition 12 (HV/IV ratio):** `_atr_sigma` (ATR-based realized vol) is the HV baseline. `sigma` is the live ATM IV used for strike selection. The ratio `live_short_IV / _atr_sigma ≥ 1.10` ensures the market is pricing options at least 10% richer than what the stock actually moves — the vol risk premium that funds the strategy's edge. If this ratio is below 1.10, selling premium is not worth the risk.

**Condition 8 (risk/reward):** A ₹50-wide spread collecting only ₹5 has a 1:9 R/R. The 20% floor means at worst a ₹50 wing collects ₹10, giving ~1:4 R/R.

**Condition 4 (DTE rationale):** With a DTE < 7 exit trigger, a fresh entry at DTE 21 gives a 14-day runway before the forced exit. Theta decay is steepest from DTE 25 to DTE 7 — entering at DTE 21 captures the prime theta acceleration segment.

**Condition 4a (re-entry):** If a spread closes at 75%+ profit and sufficient DTE remains (≥ 14 days), the same symbol may re-enter the same day. This can double the theta trades per expiry cycle without increasing overnight gamma risk.

### 5.3 Strike Selection

| Leg | Target Delta | Approximate Position |
|-----|-------------|---------------------|
| Short PE (bull put) | −0.20 | ~1.5–2 intervals below current price |
| Long PE (bull put) | −0.10 | ≥ 2 intervals further OTM from short |
| Short CE (bear call) | +0.20 | ~1.5–2 intervals above current price |
| Long CE (bear call) | +0.10 | ≥ 2 intervals further OTM from short |

Delta ≈ 0.20 → ~80% probability of expiring worthless. If the delta model places the long strike inside the short strike (geometry violation), it is forced to `short_strike ± 2 × interval`.

### 5.4 Entry Example

```
INFY @ ₹1,620  |  ATR14 (5-min) = ₹16  →  ATR_daily = ₹16 × √75 = ₹138.6
σ_realized = (₹138.6 / ₹1,620) × √252 ≈ 0.285  (28.5% annualized HV)
DTE = 25  ✓  |  Lot = 400
EMA20 = 1,625 > EMA50 = 1,608  →  EMA spread% = 1.05%  ≥ 0.1%  →  BULL_PUT_SPREAD
VWAP (10-day) = 1,615  |  1,620 ≥ 1,615 × 0.995 = 1,607  ✓
ADX14 = 22  ✓ (15–30)  |  Breadth = 0.55  ✓ (neutral)  |  PCR = 0.95  ✓ (≥ 0.8)

Strike selection (delta model, sigma from live ATM IV = 30%):
  Short PE: 1,510 (delta ~−0.20, ~6.8% OTM)
  Long  PE: 1,480 (delta ~−0.10)
  Wing width: 30 points

Live LTP:
  SELL INFY25JUL1510PE @ ₹12.50
  BUY  INFY25JUL1480PE @ ₹5.00
  Net credit = ₹7.50  ✓ (≥ 30 × 20% = ₹6.00)
  Total credit = ₹7.50 × 400 = ₹3,000  ✓ (≥ ₹350)

HV/IV ratio: live short IV ≈ 32% / HV 28.5% = 1.12  ✓ (≥ 1.10)

Max profit: ₹3,000  |  Max loss: (₹30 − ₹7.50) × 400 = ₹9,000  |  R/R = 1:3
```

### 5.5 Exit Conditions (Priority Order)

#### Exit 1: DTE < 7 days
Close before gamma explosion. Locks in remaining theta profit.

#### Exit 2: Underlying Breaches Short Strike
- BULL_PUT: underlying price < short put strike
- BEAR_CALL: underlying price > short call strike

Emergency stop — short leg is moving into the money.

#### Exit 3: VIX Spike Dynamic Thresholds
If VIX has risen ≥ 50% from the VIX level at entry (`entry_vix`), thresholds are tightened:
- **SL tightened:** exit if short leg ≥ 1.5× sold price (instead of the normal 2×)
- **Profit target tightened:** exit if short leg ≤ 40% of sold price (60% profit captured, instead of normal 75%)

Rationale: A 50%+ VIX spike from entry means IV expansion is actively working against the short premium — waiting for the full 75% target or 2× SL exposes the position to accelerating losses.

#### Exit 4: DTE-Tiered Profit Target
The profit threshold scales with remaining DTE — take profit sooner as gamma risk rises near expiry:

| DTE | Short leg close threshold | Profit captured |
|-----|--------------------------|-----------------|
| > 21 days | ≤ 25% of sold value | 75% |
| 15 – 21 days | ≤ 35% of sold value | 65% |
| ≤ 14 days | ≤ 45% of sold value | 55% |

```python
_dte_profit_pct = 0.25 if dte > 21 else (0.35 if dte > 14 else 0.45)
# Exit when: cur_short ≤ short_premium × _dte_profit_pct
```

Rationale: With fewer days to expiry, a sudden adverse move converts a 55%-profitable position into a loss much faster due to gamma amplification. Taking profit earlier at DTE ≤ 14 locks in gains.

#### Exit 5: Stop Loss (Normal Conditions)
Short leg rises to ≥ 2× sold value (when VIX spike is not active).
```
Short sold at ₹18 → stop at ₹36
```

#### Exit 6: Delta-Based Adverse Exit
If the short leg's Black-Scholes |delta| (computed from current price, ATR-derived vol, and remaining DTE) exceeds **0.40**, the position is exited. At entry the short leg has delta ≈ 0.20 (~80% OTM probability). At |delta| > 0.40 the short strike has a >40% chance of expiring in-the-money — the original thesis is invalidated.

```
BULL_PUT short put — entry delta: −0.20
If current delta < −0.40: EXIT
```

This fires at the same priority level as stops; whichever triggers first wins.

### 5.6 PnL Calculation

```
Net PnL = [(short_sold − short_close) − (long_paid − long_close)] × lot_size

Profit target (75%) example:
  Short: ₹18 → ₹4.50  =  +₹13.50/share
  Long:  ₹8  → ₹2.00  =  −₹6.00/share  (long also decays; partially offsets profit)
  Net = (₹13.50 − ₹6.00) × 400 = +₹3,000

Stop loss example:
  Short: ₹18 → ₹36   =  −₹18/share
  Long:  ₹8  → ₹14   =  +₹6/share
  Net = (−₹18 + ₹6) × 400 = −₹4,800
```

---

## 6. Strategy 3: Iron Condor

**File:** [src/strategies/iron_condor.py](../src/strategies/iron_condor.py)  
**Registered as:** `IRON_CONDOR`  
**Strategy ID:** `iron_condor_v1`

### 6.1 Concept

Four-leg defined-risk structure for sideways markets. Collects premium from both sides simultaneously: a put spread below current price and a call spread above. Profit if the stock stays within both short strikes until expiry.

```
PUT  wing: SELL OTM Put (δ ≈ −0.20) + BUY further-OTM Put (δ ≈ −0.10)
CALL wing: SELL OTM Call (δ ≈ +0.20) + BUY further-OTM Call (δ ≈ +0.10)
```

Max profit = total net credit collected (both short legs expire worthless).  
Max loss = wider wing spread minus net credit (capped, fully defined).

### 6.2 Entry Conditions (All Must Be Met)

| # | Condition | Threshold / Logic |
|---|-----------|-------------------|
| 1 | ATR% below volatility threshold | < 1.2% |
| 2 | EMA is flat (no directional trend) | EMA spread% < 0.1% |
| 3 | No existing condor for this symbol | Must be flat |
| 3a | No existing spread for this symbol | Stacking guard |
| 4 | **Minimum DTE (fresh entry)** | **≥ 21 days** |
| 4a | **Minimum DTE (re-entry after same-day profit close)** | **≥ 14 days** |
| 4b | Not in `_exited_today` | Adverse exits block same-day re-entry |
| 5 | Absolute net credit minimum | ≥ ₹600 total (covers 8-order round-trip fees) |
| 6 | Each wing credit ≥ 20% of wing width | Both wings individually checked |
| 7 | Margin available | ≈ max_wing_width × lot_size |
| 8 | **VIX ≥ 12.0 AND IV Rank ≥ 0.30** | Premium must be worth selling |
| 9 | **PCR neutrality** | PCR must be 0.7 – 1.4. Outside this range the market is too directional for a range-bound structure. Fails-open if PCR unavailable. |
| 10 | **Market Breadth neutral** | Breadth must be 0.35 – 0.65. Extreme breadth (very bullish or very bearish) indicates a one-sided market unsuitable for condors. |
| 11 | **ADX low filter** | ADX14 < 20. A rising ADX (≥ 20) indicates a developing trend that could breach either wing. A value of 0 bypasses this check. |
| 12 | **Event calendar** | No earnings / RBI MPC / Budget within 5 calendar days |

**Condition 9 (PCR neutrality):** A PCR below 0.7 means put OI is unusually low relative to calls — the market is positioned for a strong rally (bad for the call wing). A PCR above 1.4 means heavy put buying — market hedged for a large drop (bad for the put wing). The 0.7–1.4 neutral zone means neither directional extreme dominates.

**Condition 10 vs. Credit Spread breadth:** Iron condors need true market neutrality — both extremes (< 0.35 and > 0.65) block entry. Credit spreads block only one extreme (the direction that contradicts the spread direction). Condors have no directional thesis to protect and need a calm, non-trending environment on both sides.

**Condition 11 (ADX < 20):** Whereas credit spreads need ADX 15–30 (mild trend confirmed), condors need ADX < 20 — as little trend as possible. An ADX ≥ 20 signals that a trend is developing and could breach either wing.

### 6.3 Leg Placement — All-or-Nothing

All 4 legs are placed sequentially. If any leg fails, all previously placed legs are immediately **unwound** (reversed). The engine checks unwind order results and alerts if the unwind itself fails (naked short risk).

```
Order: SELL put short → BUY put long → SELL call short → BUY call long
If leg 3 fails: reverse legs 1 and 2 immediately
If unwind fails: CRITICAL alert sent, manual intervention required
```

### 6.4 Entry Example

```
HDFCBANK @ ₹1,750  |  ATR14 (5-min) = ₹14.50  →  ATR% = 0.83%  < 1.2%  ✓
EMA20 = 1,750.5,  EMA50 = 1,750.0  →  EMA spread% = 0.03%  < 0.1%  ✓ (flat)
DTE = 20  ✓  |  Lot = 550  |  ADX = 14  ✓ (< 20)  |  Breadth = 0.50  ✓  |  PCR = 0.95  ✓

sigma = 22% (live ATM IV from Zerodha)

Put wing:  SELL 1700PE @ ₹15  |  BUY 1650PE @ ₹4   |  Credit = ₹11  ✓ (≥ 50×20%=₹10)
Call wing: SELL 1800CE @ ₹14  |  BUY 1850CE @ ₹3   |  Credit = ₹11  ✓
Net credit = ₹22.00 per share  |  Total = ₹22 × 550 = ₹12,100  ✓ (≥ ₹600)

Max profit: ₹12,100  |  Max loss: (₹50 − ₹22) × 550 = ₹15,400  |  R/R ≈ 1:1.3
```

### 6.5 Exit Conditions (Priority Order)

#### Exit 1: DTE < 7 days
Close before gamma explosion.

#### Exit 2: Underlying Breaches Either Short Strike
```
Price < put short strike → emergency stop (put wing ITM)
Price > call short strike → emergency stop (call wing ITM)
```
Closes entire condor on breach of either wing.

#### Exit 3: VIX Spike Dynamic Thresholds
Same mechanism as credit spreads (§5.5 Exit 3). If VIX has risen ≥ 50% from entry VIX:
- Profit threshold raised to `max(profit_pct, 0.40)` → take 60% profit early
- SL multiplier lowered to `min(sl_mult, 1.5)` → tighter SL during IV expansion

The `max()` and `min()` ensure the DTE-tiered threshold (if already stricter than the VIX-spike threshold) remains in effect.

#### Exit 4: Stop Loss on Either Short Leg
Either short leg rises to ≥ 2× its sold value (1.5× if VIX spike active).
```
Put short sold at ₹15 → stop at ₹30 (normal) or ₹22.50 (VIX spike)
Call short sold at ₹14 → stop at ₹28 (normal) or ₹21 (VIX spike)
Either trigger → close entire condor
```

#### Exit 5: DTE-Tiered Profit Target (Either Wing, OR Logic)
Same tiered schedule as credit spreads. The condor's profit threshold is the `max()` of the DTE tier and any VIX spike adjustment:

| DTE | Short leg target | Profit captured |
|-----|-----------------|-----------------|
| > 21 days | ≤ 25% of sold value | 75% |
| 15 – 21 days | ≤ 35% of sold value | 65% |
| ≤ 14 days | ≤ 45% of sold value | 55% |

```python
profit_pct = 0.25  # base
if dte <= 14:   profit_pct = max(profit_pct, 0.45)
elif dte <= 21: profit_pct = max(profit_pct, 0.35)
# VIX spike may further raise this via max(profit_pct, 0.40)
# Exit if: cur_ps ≤ put_short_premium × profit_pct  OR  cur_cs ≤ call_short_premium × profit_pct
```

**OR logic:** If one wing decays to target, the stock has moved toward that side. The opposite wing's risk increases. Lock in the winner by closing the entire structure.

#### Exit 6: Regime Shift Post-Entry
If `market:regime` shifts to `TRENDING` or `VOLATILE` after at least 1 full day of holding, the condor is exited — the range-bound neutrality thesis is broken. Same-day regime shifts are ignored (avoids noise from opening-hour volatility).

#### Exit 7: Delta-Based Adverse Exit (Either Wing)
If either short leg's |delta| exceeds **0.40**, the entire condor is closed.
```
Put short entry delta ≈ −0.20 → exit if delta < −0.40
Call short entry delta ≈ +0.20 → exit if delta > +0.40
```

### 6.6 PnL Calculation

```
Net PnL = [(put_short_sold  − put_short_close)
         + (call_short_sold − call_short_close)
         − (put_long_close  − put_long_paid)
         − (call_long_close − call_long_paid)] × lot_size
```

---

## 7. Daily Cycle — How Everything Fits Together

### 7.1 Timeline

```
08:30 AM  Zerodha auto-authentication (daily cron job)
           Lot sizes refreshed from kite.instruments("NFO"), cached in Redis

09:00 AM  Platform running
           LTPPoller fetches 10-day 5-min OHLC history
           RS Ranker ranks all 41 symbols by relative strength

09:15 AM  Market opens (on_market_open):
           - _today_order_count reset to 0
           - risk_manager.reset_daily_state() clears daily PnL accumulators
             and _strategy_deployed capital tracker
           - _strategy_deployed rebuilt from overnight active spreads/condors
           - Event calendar auto-refreshed if today is Monday
           - Zerodha access token validated (live mode)
           Signal cycle starts (every 60 seconds)
           Exit check starts (every 10 seconds)
           ⚠ WARM-UP: exit checks run, entries blocked until 9:30

09:30 AM  Warm-up complete — entries enabled

09:30 AM – 03:20 PM  [every 60 seconds — signal cycle]:
  1.  Market data freshness check (reject >90s stale ticks)
  2.  Expire stale pending orders (>5 min old)
  3.  _check_spread_exits (DTE, breach, VIX spike, tiered profit, SL, delta)
  4.  _check_condor_exits (same exit conditions, 4-leg close)
  5.  _check_open_option_exits (EMA crossover position management)
  6.  Re-read positions → _refresh_risk_state (post-exit risk state)
       • unrealized PnL computed from engine state if broker returns 0 (paper mode)
  7.  _refresh_all_position_market_prices (update market prices for all legs)
  8.  Expire stale orders again
  9.  StrategyMonitor: rolling PF / drawdown check
  10. MarketRegimeDetector: update regime label (TRENDING / RANGE_BOUND / VOLATILE)
  11. PortfolioAnalyzer: concentration / correlation warnings
  12. Log portfolio delta: [PortfolioDelta] bulls=N bears=N condors=N
  13. Entry signals: each active strategy × each symbol in its scoring pool

09:30 AM – 03:20 PM  [every 10 seconds — exit check only]:
  - _check_spread_exits + _check_condor_exits
  - _exit_cycle_lock (asyncio.Lock) prevents concurrent execution with 60s cycle

03:20 PM  EOD square-off:
  - EMA Crossover single-leg positions closed (SELL at current premium)
  - On expiry day (DTE ≤ 1): ALL positions force-closed, including spreads/condors
    - GTT backstop orders cancelled for all spreads and condors BEFORE clearing
      _active_spreads and _active_condors
  - Non-expiry: credit spreads and iron condors held overnight

03:25 PM  EOD report email sent

03:30 PM  Market closes (on_market_close)
```

### 7.2 What Happens When Data is Unavailable

| Scenario | Behaviour |
|----------|-----------|
| Redis tick missing for a symbol | `_get_market_data` returns None → symbol skipped this cycle |
| Redis tick older than 90 seconds | Same as missing — symbol skipped |
| Redis completely unavailable | No market data → no entries. Exit checks also disabled (no prices). EOD square-off fires using entry price as fallback in paper mode. |
| Zerodha broker unavailable (paper mode) | Orders rejected at PaperBroker level, logged as FAILED |
| Kill switch activated | New entries blocked. Exit orders **always** pass through regardless of kill switch state. |

---

## 7A. Multi-Day Holding — Loss Mitigation Controls

Credit spreads and iron condors are held overnight. The following controls guard against adverse multi-day scenarios.

### 7A.1 DTE Floors

| Gate | Value | Purpose |
|------|-------|---------|
| Entry (fresh) | DTE ≥ 21 | Ensures 14-day runway before the DTE < 7 exit fires |
| Entry (re-entry after profit close) | DTE ≥ 14 | Allows a 2nd trade per expiry cycle after profitable close |
| Exit trigger | DTE < 7 | Avoids gamma explosion in the final week |

### 7A.2 Adverse Exit Circuit Breaker

Redis key `sl_freq:{symbol}` (5-day TTL) counts adverse exits (breach/SL) per symbol. After 2 adverse exits within 5 trading days on the same symbol, further entries are blocked until the TTL expires. Prevents repeatedly entering a symbol that has been stopped out twice in a row.

### 7A.3 Regime Shift Exit (Iron Condor)

If `MarketRegimeDetector` shifts to `TRENDING` or `VOLATILE` while an iron condor is open, and the position has been held at least 1 full day, the condor is flagged for exit on the next cycle. The range-bound thesis is broken. Credit spreads are directional and are not subject to this check.

### 7A.4 VIX Spike Early Exit

When VIX rises ≥ 50% from the entry VIX:
- Profit threshold raised: take 60% profit (short decays to ≤ 40% of sold) instead of waiting for 75%
- Stop loss tightened: exit if short rises to 1.5× sold instead of 2×

Applies to both credit spreads and iron condors. VIX at entry is stored in `_active_spreads[symbol]["entry_vix"]` and `_active_condors[symbol]["entry_vix"]` at trade open time.

### 7A.5 GTT Backstop (Exchange-Level Stop, Live Mode)

For live mode only, a Good Till Triggered (GTT) order is placed on each short leg at **2.5× the entry premium** after every spread or condor entry. This fires automatically at Zerodha if the platform goes offline overnight and the short leg deteriorates.

- Spread: 1 GTT on the short leg. GTT ID stored in `_active_spreads[symbol]["gtt_id"]`.
- Condor: 2 GTTs (put short and call short). IDs stored in `put_short_gtt_id` and `call_short_gtt_id`.

GTTs are cancelled by the engine:
- After a normal exit via `_check_spread_exits` / `_check_condor_exits` (via `_cancel_gtt()`)
- Before `_active_spreads.clear()` / `_active_condors.clear()` on expiry day, to prevent exchange-level orders firing on already-closed positions

### 7A.6 DTE Roll Detection on Restart

If the platform restarts while a multi-day position is open and the current DTE differs from the stored entry DTE by more than expected, `_close_on_first_cycle` flags the symbol for immediate force-close on the next signal cycle rather than continuing to hold a potentially mismatched position.

### 7A.7 Portfolio Delta Logging

Every signal cycle logs `[PortfolioDelta] bulls=N bears=N condors=N` — counts of BULL_PUT_SPREADs, BEAR_CALL_SPREADs, and iron condors currently open. Provides a quick directional bias check; a portfolio of 4 BULL_PUT_SPREADs and 0 bearish structures has an implicit net long directional exposure.

### 7A.8 Sector Concentration Check

Maximum 2 open structures per sector (`MAX_SECTOR_POSITIONS = 2`). If HDFCBANK + ICICIBANK spreads are already open, further Banking sector entries are blocked. Prevents correlated sector blow-ups.

### 7A.9 Re-Entry After Profit

`_profit_closed_today` tracks symbols where a position closed at ≥ 75% profit the same day. These symbols are eligible for re-entry at DTE ≥ 14 (instead of the normal 21-day floor). Adverse exits (`_exited_today`) do NOT allow re-entry at any DTE floor until the next session.

### 7A.10 DTE-Tiered Profit Targets

The flat 75% profit target has been replaced with a tiered schedule. See §5.5 Exit 4 and §6.5 Exit 5. Reflects the non-linear gamma expansion as expiry approaches — near-expiry positions need to be exited earlier to avoid the last-week gamma danger zone.

### 7A.11 Delta-Based Exit

When a short leg's Black-Scholes |delta| exceeds 0.40 (vs. ~0.20 at entry), the position is exited regardless of other conditions. At |delta| > 0.40, the short strike has a >40% probability of expiring in-the-money. See §5.5 Exit 6 and §6.5 Exit 7.

### 7A.12 ADX Entry Filters

- **Credit spreads:** ADX14 must be 15–30 (inclusive). Below 15 = no trend (condor regime); above 30 = trend too strong (blowthrough risk).
- **Iron condors:** ADX14 must be < 20. A rising ADX signals a developing directional move that could breach either wing.

### 7A.13 Market Breadth Filter

`market:breadth` (advancing / total) is computed each poll cycle from all 41 symbols:
- Credit spreads: BEAR_CALL blocked when breadth > 0.65 (advancing market contradicts selling calls). BULL_PUT blocked when breadth < 0.35 (declining market contradicts selling puts). Neutral 0.35–0.65 allows both directions.
- Iron condors: blocked when breadth < 0.35 or > 0.65 — market too directional for range-bound structure.

### 7A.14 Event / Earnings Calendar Filter

No new spread or condor entry is made within 5 calendar days of a scheduled corporate event (quarterly results, board meeting) or market-wide macro event (RBI MPC, Union Budget). Calendar auto-refreshed every Monday from NSE's public API. See §2.5 for full details. EMA Crossover entries are not blocked by the calendar.

---

## 8. Position Sizing & Margin Rules

### 8.1 Lot Sizes

Lot sizes are fetched from Zerodha's live instrument data every morning (`kite.instruments("NFO")`) and cached in Redis. Values in `src/core/constants.py` serve as hardcoded fallbacks for startup-before-auth scenarios.

| Symbol | Lot Size | Symbol | Lot Size |
|--------|---------|--------|---------|
| RELIANCE | 250 | SBIN | 1500 |
| TCS | 150 | ITC | 3200 |
| INFY | 300 | M&M | 700 |
| HDFCBANK | 550 | COALINDIA | 4200 |
| ICICIBANK | 700 | TATASTEEL | 5500 |
| BAJFINANCE | 125 | MARUTI | 25 |

All positions are placed in exactly 1 lot. No position scaling currently. Full table in `src/core/constants.py → FNO_LOT_SIZES`.

### 8.2 Margin Requirements

| Strategy | Margin Formula | Example (RELIANCE, 50-pt wing) |
|----------|---------------|-------------------------------|
| EMA Crossover (BUY) | Premium × lot_size (debit) | ₹62 × 250 = ₹15,500 |
| Credit Spread | (short − long strike) × lot_size | ₹50 × 250 = ₹12,500 |
| Iron Condor | max_wing_width × lot_size | ₹50 × 250 = ₹12,500 |

### 8.3 Daily Limits

| Limit | Default |
|-------|---------|
| Max daily portfolio loss (realized + unrealized) | 5% of capital |
| Max orders per day | 20 |
| Max open positions (total legs) | 25 |
| Max positions per sector | 2 |
| Max BUY exposure per trade | 20% of capital |

---

## 9. Execution & Slippage Model

### 9.1 Paper Trading Fill Model

The PaperBroker simulates realistic execution with three components:

**A) Bid-Ask Spread Slippage**

Fills happen at bid (for SELL) or ask (for BUY) — always worse than mid-price.

| Premium Range | Half-Spread Applied |
|--------------|---------------------|
| ≤ ₹0.30 | 40% of premium |
| ₹0.31 – ₹0.75 | 20% of premium |
| ₹0.76 – ₹2.00 | 10% of premium |
| ₹2.01 – ₹5.00 | 6% of premium |
| > ₹5.00 | 3% of premium |

Example: BUY at estimated ₹62 → half-spread = 3% → fill at ₹63.86.

**B) Stochastic Rejection + Enhanced Slippage**

| Premium Range | Rejection Rate | Max Extra Slippage |
|--------------|---------------|-------------------|
| ≤ ₹0.30 | 5% | 30% of premium |
| ₹0.31 – ₹1.00 | 2% | 15% of premium |
| ₹1.01 – ₹5.00 | 1% | 5% of premium |
| > ₹5.00 | 0.5% | 0% |

Extra slippage is sampled uniformly from 0 → max (expected cost = max/2). Applied in addition to the bid-ask half-spread. Rejected orders are treated as `FAILED` status.

**C) Transaction Fees (per order)**

| Fee Type | Rate |
|---------|------|
| Brokerage | ₹20 flat (or 0.03% of turnover, whichever lower) |
| STT | 0.1% of turnover (SELL side only) |
| NSE exchange charges | 0.053% of turnover |
| GST | 18% on (brokerage + exchange charges) |
| SEBI charges | ₹10 per crore of turnover |
| Stamp duty | 0.003% of turnover (BUY side only) |

### 9.2 Live Trading Fill Model

Orders are routed to Zerodha as market/limit orders at the estimated premium. The actual fill price returned by Zerodha is recorded. Fill divergence from the estimated premium reflects actual bid-ask spread and market impact at execution time.

### 9.3 Slippage Tracking in Trade Journal

At every exit, the engine captures the actual fill price from the order object (`order.avg_price`) and computes `total_slippage_pts` as the sum of `|fill_price − requested_price|` across all legs. This is stored in the trade journal for every trade type:

- **EMA single-leg exit:** slippage = `|fill_price − current_p|`
- **Credit spread exit:** slippage = `|short_fill − cur_short| + |long_fill − cur_long|`
- **Iron condor exit:** sum of slippage across all 4 legs

Review `total_slippage_pts` to compare paper vs. live execution quality and calibrate slippage assumptions.

---

## 10. Trade Journal Schema

Every trade (entry + exit) is logged to the `trade_journal` MySQL table. Primary source for all PnL analytics.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BigInt | Auto-increment primary key |
| `strategy_name` | String(50) | `ema_crossover_v1`, `credit_spread_v1`, `iron_condor_v1` |
| `underlying` | String(30) | Stock symbol, e.g. `RELIANCE` |
| `structure_type` | String(30) | `SINGLE_LEG`, `BULL_PUT_SPREAD`, `BEAR_CALL_SPREAD`, `IRON_CONDOR` |
| `contracts` | String(500) | JSON list of all contract symbols |
| `entry_time` | Timestamp | Time of first leg fill |
| `entry_price` | Decimal | Net credit (spread/condor) or debit (single-leg) per share |
| `quantity` | Integer | Lot size |
| `regime_atr_pct` | Float | ATR% at entry — identifies which regime triggered |
| `ema_spread_pct` | Float | EMA spread% at entry |
| `iv_rank` | Float | IV rank [0, 1] at entry; None if unknown |
| `vix_at_entry` | Float | India VIX at entry |
| `day_of_week` | Integer | 0 = Monday … 4 = Friday |
| `hour_of_day` | Integer | Hour of entry (IST, 9–15) |
| `exit_time` | Timestamp | Time of last leg close |
| `exit_price` | Decimal | Net debit paid to close (or fill price for single-leg) |
| `exit_reason` | String(200) | e.g. `DTE=6 < 7`, `Stop loss`, `DTE-tiered profit (DTE=10)`, `VIX spike profit` |
| `pnl` | Decimal | Realized PnL (net of slippage, before brokerage fees) |
| `hold_days` | Integer | Calendar days from entry to exit |
| `atr_at_exit` | Float | ATR% when position closed |
| `vix_at_exit` | Float | India VIX when position closed |
| `regime_label` | String(30) | `TRENDING` / `RANGE_BOUND` / `VOLATILE` |
| `total_slippage_pts` | Float | Sum of |fill − mid| across all legs (actual fill slippage) |

**Useful queries:**
```sql
-- Win rate and average PnL by strategy
SELECT strategy_name,
       COUNT(*) AS trades,
       ROUND(SUM(pnl > 0) / COUNT(*) * 100, 1) AS win_rate_pct,
       ROUND(AVG(pnl), 0) AS avg_pnl
FROM trade_journal WHERE exit_time IS NOT NULL
GROUP BY strategy_name;

-- Exit reason analysis — which exits are most profitable
SELECT exit_reason, COUNT(*) AS count, ROUND(AVG(pnl), 0) AS avg_pnl
FROM trade_journal WHERE exit_time IS NOT NULL
GROUP BY exit_reason ORDER BY avg_pnl DESC;

-- Day-of-week performance
SELECT day_of_week, COUNT(*) AS trades, ROUND(SUM(pnl), 0) AS total_pnl
FROM trade_journal WHERE exit_time IS NOT NULL
GROUP BY day_of_week ORDER BY day_of_week;

-- Average slippage by structure type
SELECT structure_type,
       ROUND(AVG(total_slippage_pts), 2) AS avg_slippage_pts,
       COUNT(*) AS trades
FROM trade_journal WHERE exit_time IS NOT NULL AND total_slippage_pts IS NOT NULL
GROUP BY structure_type;
```

---

## 11. Email Alerts Reference

All alerts carry `[Falcon Trader]` in the subject line.

| Event | When Sent |
|-------|----------|
| `ORDER PLACED` | EMA Crossover option bought (order confirmed) |
| `CREDIT SPREAD OPENED` | Bull put or bear call spread entered |
| `IRON CONDOR OPENED` | Iron condor 4-leg structure entered |
| `POSITION CLOSED` | Single-leg option exited (any exit reason) |
| `CREDIT SPREAD CLOSED` | Spread closed (any exit reason, including VIX spike) |
| `IRON CONDOR CLOSED` | Condor closed (any exit reason) |
| `EOD POSITION UPDATE` | End-of-day: EMA legs closed, spreads/condors held overnight |
| `EXPIRY SQUARE-OFF` | Force-close on expiry day |
| `RISK ALERT` | Kill switch activated or daily loss limit triggered |
| `CRITICAL: ... FAILED` | Unwind of a failed spread/condor leg failed — manual intervention required |

**Alerts are NOT sent when:**
- An order is REJECTED_BY_RISK — position not opened/closed, no alert
- An order FAILED at broker level — same
- Email alerts paused via Admin dashboard

---

## 12. Parameters Quick Reference

### EMA Crossover (`ema_crossover_v1`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fast_period` | 20 | Fast EMA period (5-min bars) |
| `slow_period` | 50 | Slow EMA period (5-min bars) |
| `stop_loss_pct` | 0.50 | Exit if premium falls 50% from entry |
| `target_pct` | 1.00 | Exit if premium rises 100% from entry |
| `trailing_stop_pct` | 0.25 | Exit if premium falls 25% from peak |
| `signal_confirm_bars` | 2 | Crossover must persist on 2 distinct completed 5-min bars |
| `min_dte` | 10 | Minimum DTE for entry |
| `max_dte` | 25 | Maximum DTE for entry |

Engine-level filters (not strategy parameters):

| Filter | Value | Description |
|--------|-------|-------------|
| RVOL minimum | 1.3 | Relative volume must exceed 1.3 for entry |
| ADX minimum | 25 | ADX14 must be ≥ 25 for entry |
| MTF confirmation | 15-min EMA aligned | 15-min EMA20/50 direction must match 5-min signal |
| DTE forced exit | DTE < 4 | Close before gamma explosion |

### Credit Spread (`credit_spread_v1`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fast_period` | 20 | Fast EMA period |
| `slow_period` | 50 | Slow EMA period |
| `low_vol_threshold` | 1.2 | ATR% below which credit spreads activate |
| `profit_close_pct` | 0.25 | Base: close when short decays to 25% of sold price |
| `stop_loss_multiple` | 2.0 | Close when short rises to 2× sold price |
| `min_dte` | 7 | DTE at which exit is forced (gamma risk) |

Engine-level constants:

| Constant | Value | Description |
|----------|-------|-------------|
| `_ENTRY_MIN_DTE` | 21 | Minimum DTE for fresh entry |
| `_REENTRY_MIN_DTE` | 14 | Minimum DTE for re-entry after same-day profit close |
| `_vwap_buffer` | 0.005 (0.5%) | Price must be within 0.5% of VWAP on the correct side |
| `_5MIN_ATR_SCALE` | `sqrt(75)` ≈ 8.66 | Converts 5-min ATR14 to daily equivalent |
| VIX threshold | 12.0 | Minimum India VIX to sell premium |
| IV Rank threshold | 0.30 | Minimum per-symbol IV rank to sell premium |
| ADX range | 15–30 | ADX14 window for credit spread entry |
| PCR min (BULL_PUT) | 0.80 | PCR must not be below this (too bearish) |
| PCR max (BEAR_CALL) | 1.20 | PCR must not be above this (too bullish) |
| Breadth block (BEAR_CALL) | > 0.65 | Block bear calls in advancing markets |
| Breadth block (BULL_PUT) | < 0.35 | Block bull puts in declining markets |
| VIX spike trigger | entry_vix × 1.5 | VIX rise ≥ 50% from entry activates tight thresholds |
| VIX spike SL | 1.5× | SL multiple during VIX spike (vs normal 2×) |
| VIX spike profit | ≤ 40% of sold | 60% profit target during VIX spike (vs normal 75%) |
| Min credit total | ₹350 | Minimum net credit to cover round-trip fees |
| Min credit % of wing | 20% | Guards risk/reward ratio |
| HV/IV ratio | ≥ 1.10 | Market IV must exceed realized HV by at least 10% |

### Iron Condor (`iron_condor_v1`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fast_period` | 20 | Fast EMA period |
| `slow_period` | 50 | Slow EMA period |
| `low_vol_threshold` | 1.2 | ATR% below which condor activates |
| `flat_threshold` | 0.1 | EMA spread% below which EMA is flat |
| `profit_close_pct` | 0.25 | Base: close entire condor when either short decays to 25% |
| `stop_loss_multiple` | 2.0 | Close condor if either short rises to 2× |
| `min_dte` | 7 | DTE at which exit is forced |

Engine-level constants (same DTE, sigma, VWAP constants as credit spread, plus):

| Constant | Value | Description |
|----------|-------|-------------|
| PCR neutrality range | 0.7 – 1.4 | PCR outside this range → market too directional for condor |
| Breadth neutral range | 0.35 – 0.65 | Both extremes block condor (not just one) |
| ADX max | 20 | ADX ≥ 20 blocks condor (trend developing) |
| Min condor credit | ₹600 | Higher minimum than spread (8 order round-trips) |
| VIX threshold | 12.0 | Same as credit spreads |
| IV Rank threshold | 0.30 | Same as credit spreads |

---

## 13. Strategy Performance Metrics

The `StrategyMonitor` class evaluates performance every signal cycle using the `trade_journal` table. Minimum 30 completed trades required before evaluation — fewer produce statistically meaningless signals.

### 13.1 Key Metrics

| Metric | Formula | Healthy Target |
|--------|---------|---------------|
| **Win Rate** | Winning trades ÷ total closed trades | > 55% (spreads/condors); > 45% (EMA) |
| **Profit Factor** | Total gross profit ÷ total gross loss | > 1.5 |
| **Expectancy** | (Win rate × avg win) − (loss rate × avg loss) | > 0 |
| **Max Drawdown** | Peak-to-trough cumulative PnL decline | < 15% of capital |
| **Sharpe (daily)** | Mean daily PnL ÷ std dev × √252 | > 1.0 |

### 13.2 Auto-Kill Thresholds

StrategyMonitor pauses a strategy when it detects deterioration over the rolling 30-trade window.

Thresholds: `ROLLING_WINDOW=30, ROLLING_PF_FLOOR=0.9, DRAWDOWN_MULTIPLIER=1.5, MIN_TRADES_REQUIRED=30`. See `src/risk/strategy_monitor.py`.

When paused by StrategyMonitor, the stale pending crossover signal buffer (`_pending_signal`, `_pending_count`) is cleared in `on_pause()` — so a stale crossover cannot fire immediately when the strategy is resumed.

### 13.3 Reviewing Performance

`/analytics/pnl-summary` returns aggregate PnL. For per-strategy breakdown, query `trade_journal` directly or use the dashboard Risk & PnL page.

---

## 14. Assumptions & Limitations

### 14.1 Data Assumptions

| Assumption | Risk | Mitigation |
|-----------|------|-----------|
| All indicators derived from 5-min candles via Zerodha API | API outage or rate limit → stale data | 90-second staleness circuit breaker |
| ATR14 uses Wilder's EWM (α=1/14) — same as ADX14 | Slight divergence from simple 14-bar rolling mean in most references | Wilder's smoothing is the intended convention; both converge after sufficient history |
| Strike selection uses ATR-derived or live ATM IV, not full option chain surface | Strike placement may slightly differ from ideal delta in skewed markets | Live IV upgrade (step 2) in live mode reduces this; acceptable for paper trading |
| Paper mode premiums estimated via ATR model, not live IV | Paper P&L may differ from live | In live mode, actual Zerodha fills replace all estimates; slippage is tracked per fill |
| VIX and PCR from Zerodha snapshot | Intraday OI data may lag up to 15 minutes | Used only for entry filtering, not exit timing |
| Paper mode unrealized PnL computed from Redis option price cache | Cache may be absent for some contracts (e.g. before first WebSocket tick) | Falls back to zero if prices unavailable; daily loss circuit breaker may not fire until cache warms up |

### 14.2 Operational Assumptions

| Assumption | Notes |
|-----------|-------|
| Sufficient option liquidity | All 41 F&O symbols have reasonable liquidity; far-OTM hedge legs may have wider bid-ask spreads |
| Multi-day spread/condor holding | Credit spreads and iron condors are held overnight. Ensure sufficient margin maintained across overnight sessions. |
| EMA Crossover positions are intraday | Single-leg positions closed at 3:20 PM regardless of PnL |
| Zerodha API available during market hours | Platform depends on Zerodha for both data and execution |
| Multi-session state preserved | Redis persists active spreads, condors, single-leg journals, and today's exit history across restarts. _strategy_deployed is rebuilt from active positions on every market open. |
| Overnight gap risk | Multi-day positions are exposed to gap risk on news. GTT backstop orders at 2.5× entry premium provide exchange-level protection in live mode. |
| Not validated on black swan events | Circuit breakers and daily limits reduce risk; extreme gap-downs or circuit-limit events are not fully modelled |
| NSE F&O equities only | No index options (NIFTY/BANKNIFTY), no currencies, no commodities |

### 14.3 Known Gaps (Roadmap)

| Gap | Impact | Priority |
|-----|--------|---------|
| Position correlation cap (corr > 0.8 between symbols) | Two banking stocks may move together | Medium |
| Partial-lot sizing (scale in/out) | Currently always 1 lot | Low |
| Multi-expiry support (weekly options) | Near-month only | Low |
| Walk-forward backtesting integration with live parameter selection | Parameters fixed at startup | Low |

### 14.4 Not Suitable For

- High-frequency trading (minimum entry granularity = 60 seconds; exit granularity = 10 seconds)
- Stocks outside the 41-symbol F&O universe
- Index options (NIFTY/BANKNIFTY), currency futures, or commodity derivatives
- Capital below ₹3,00,000 (multi-day spread margin requirements require sufficient buffer)

---

*For infrastructure, API, and platform documentation see [PLATFORM_DOCUMENTATION.md](PLATFORM_DOCUMENTATION.md).*  
*Source files: [src/strategies/](../src/strategies/) | [src/live_trading/live_trading_engine.py](../src/live_trading/live_trading_engine.py) | [src/risk/risk_manager.py](../src/risk/risk_manager.py)*
