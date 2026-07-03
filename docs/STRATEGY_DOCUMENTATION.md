# Falcon Trader — Strategy Documentation

**Version:** 2.2  
**Last Updated:** 2026-07-03  
**Platform Version:** 3.1  

---

## Table of Contents

1. [Overview — Three-Strategy Architecture](#1-overview)
2. [Market Data & Signal Infrastructure](#2-market-data--signal-infrastructure)
3. [Risk Filters (Common to All Strategies)](#3-risk-filters)
4. [Strategy 1: EMA Crossover (Momentum Long Options)](#4-strategy-1-ema-crossover)
5. [Strategy 2: Credit Spread (Theta Collection, Directional)](#5-strategy-2-credit-spread)
6. [Strategy 3: Iron Condor (Theta Collection, Range-Bound)](#6-strategy-3-iron-condor)
7. [Daily Cycle — How Everything Fits Together](#7-daily-cycle)
8. [Position Sizing & Margin Rules](#8-position-sizing--margin-rules)
9. [Execution & Slippage Model](#9-execution--slippage-model)
10. [Trade Journal Schema](#10-trade-journal-schema)
11. [Email Alerts Reference](#11-email-alerts-reference)
12. [Parameters Quick Reference](#12-parameters-quick-reference)
13. [Strategy Performance Metrics](#13-strategy-performance-metrics)
14. [Assumptions & Limitations](#14-assumptions--limitations)

---

## 1. Overview

The platform runs three strategies simultaneously on the same set of F&O symbols. They are designed to **never conflict** — each fires in a different market regime:

| Regime | ATR% | EMA spread | Strategy Used |
|--------|------|-----------|---------------|
| Explosive move / trending | ≥ 1.2% | Any direction | **EMA Crossover** — BUY options directionally |
| Mild trend | < 1.2% | > 0.1% (trending) | **Credit Spread** — collect premium from one side |
| Flat / sideways | < 1.2% | ≤ 0.1% (flat) | **Iron Condor** — collect premium from both sides |

Only one strategy will fire signals for a given stock at any given time. If market conditions don't match any regime cleanly, all three return HOLD.

**Symbols traded:** All 41 NSE F&O symbols (full universe — top 5 per strategy are ranked dynamically each cycle from the full pool)  
**Instruments:** NSE F&O options (near-month expiry, equity options on lot-size basis)  
**Execution:** Signal cycle runs once per minute, 9:15 AM – 3:30 PM IST  
**Hold period:** EMA Crossover positions are intraday (closed by 3:20 PM). Credit Spread and Iron Condor positions are **multi-day** — held overnight until their exit conditions trigger (DTE < 7, profit target, or stop loss). Intraday square-off does NOT apply to spreads/condors.

---

## 2. Market Data & Signal Infrastructure

### 2.1 Data Sources (3-Tier)

The engine reads from a three-tier data pipeline. Each tier is a fallback for the tier above it:

| Tier | Source | Latency | Used For |
|------|--------|---------|---------|
| 1 | Zerodha WebSocket (ZerodhaTicker) | Real-time tick | LTP updates pushed to Redis |
| 2 | Zerodha REST LTP poll (ZerodhaLTPPoller) | Every 5 seconds | Fallback when WebSocket unavailable |
| 3 | Zerodha 5-min OHLC + indicators (LTPPoller) | Every 60 seconds | EMA20, EMA50, ATR14, VWAP computed from 5-min candles |

### 2.2 Indicator Timeframe — 5-Minute Candles

**The LTP poller fetches 10 days of 5-minute OHLC candles** from Zerodha (`kite.historical_data` with interval `"5minute"`). All indicators (EMA20, EMA50, ATR14) are computed on these 5-minute bars. The cache is refreshed every 5 minutes; indicators are recomputed every 60 seconds from the cached bars.

This means:
- EMA20 = 20-bar EMA on 5-minute candles (covers ~100 minutes of intraday data)
- EMA50 = 50-bar EMA on 5-minute candles (covers ~250 minutes = ~4.17 hours of market time, roughly 2/3 of a trading day)
- ATR14 = 14-bar ATR on 5-minute candles (covers ~70 minutes)

Signals **can and do change intraday** as new 5-minute bars form. A crossover that occurs at 11:30 AM is distinct from the opening state.

### 2.3 Indicators Used by Strategies

| Indicator | Description | Used By |
|-----------|-------------|---------|
| **EMA20** | 20-bar EMA on 5-min close | All 3 strategies |
| **EMA50** | 50-bar EMA on 5-min close | All 3 strategies |
| **ATR14** | 14-bar ATR on 5-min bars (absolute price range per bar) | All 3 strategies |
| **ATR%** | ATR14 ÷ Close × 100 — volatility as % of price | Regime filter |
| **EMA spread%** | abs(EMA20 − EMA50) ÷ EMA50 × 100 — trend strength | Iron condor filter |
| **IV Rank** | Current implied volatility vs 52-week range [0, 1] | Risk layer 3 |
| **VIX** | Nifty VIX from Zerodha | Risk layer 3 |
| **PCR** | Put-Call Ratio from NSE OI data | Credit spread direction filter |
| **ADX14** | Wilder's Average Directional Index on 5-min bars — trend strength 0–100 | Entry filter (all strategies) |
| **RVOL** | Relative Volume: current bar volume ÷ 20-bar average volume | EMA Crossover entry filter |
| **Market Breadth** | Advancing symbols ÷ (advancing + declining) across all 41 symbols | Credit spread / condor entry filter |
| **EMA20 (15-min)** | 20-bar EMA on 15-minute candles | MTF confirmation for EMA Crossover |
| **EMA50 (15-min)** | 50-bar EMA on 15-minute candles | MTF confirmation for EMA Crossover |

**ADX14** uses Wilder's exponential smoothing (α = 1/14) — the same smoothing convention as ATR14. It is computed in the LTPPoller from the same 5-min OHLC history.

**Market Breadth** is computed after every LTP poll cycle, once per 60 seconds. It counts all 41 symbols where `close > prev_close` (advancing) and `close < prev_close` (declining) and publishes the ratio to Redis key `market:breadth` with a 2-minute TTL. A breadth above 0.65 means ≥ 65% of the universe is advancing (bullish); below 0.35 means ≥ 65% are declining (bearish).

**15-min OHLC** is fetched separately from Zerodha (`kite.historical_data` with interval `"15minute"`, 30 days of history) and cached in Redis key `tick15:{SYMBOL}` with 30-minute TTL. The cache is refreshed every 15 minutes. The LTPPoller computes EMA20 and EMA50 on these 15-min candles and stores them for the MTF filter.

### 2.4 Symbol Scoring (Three Separate Pools)

The LTP poller doesn't just compute indicators — it **ranks all 40 F&O symbols** into three pools, one per strategy regime, and publishes the top-5 from each pool to Redis every 60 seconds:

| Pool | Redis Key | Scoring formula |
|------|-----------|----------------|
| EMA Crossover | `nfo:top5` | ATR% × 0.6 + EMA_spread% × 0.4 (rewards volatile, trending stocks) |
| Credit Spread | `nfo:top5:spread` | (1.2 − ATR%) × 0.4 + EMA_spread% × 0.6 (rewards gentle trend, low vol) |
| Iron Condor | `nfo:top5:condor` | (1.2 − ATR%) × 0.6 + (0.1 − EMA_spread%) × 0.4 (rewards flat, stable stocks) |

Each strategy reads from its own pool, so EMA Crossover always operates on the most volatile stocks of the day and Iron Condor always operates on the most range-bound.

### 2.5 Event / Earnings Calendar Filter

**File:** `src/market_data/event_calendar.py`, `src/market_data/calendar_refresh.py`

Entry signals are blocked within **5 calendar days** of a scheduled corporate event (quarterly results, board meetings) or a market-wide macro event (RBI MPC meeting, Union Budget).

**Data sources (in order of priority):**
1. Redis key `event:calendar` — JSON dict `{SYMBOL: ["YYYY-MM-DD", ...], "*": [...]}`. Updated every Monday at market open by an automatic NSE fetch.
2. `config/event_calendar.json` — static fallback file (updated by the weekly auto-refresh; can also be manually edited for custom overrides).

**Auto-refresh:** Every Monday at 9:15 AM, the engine fires `_refresh_event_calendar()` as a background task. It fetches upcoming results dates from two NSE API endpoints and merges them with hardcoded RBI MPC dates (`"*"` key). On success, both Redis and the JSON file are updated. If NSE is unreachable, the existing calendar is left intact.

**Hardcoded market-wide dates (FY 2026-27):**
- RBI MPC: Aug 7, Oct 8, Dec 5 2026; Feb 5 2027
- Union Budget: Feb 1 2027

**Blocking logic:** If `has_event_within_days(symbol, redis, days=5)` returns True, the credit spread or iron condor entry for that symbol is skipped silently. EMA Crossover entries are not blocked by the calendar.

**Failure mode:** If the Redis lookup fails for any reason, `has_event_within_days` returns `False` (safe default — does not block trading due to a data issue).

### 2.6 Stale Data Circuit Breaker

Market data is considered stale if the Redis tick timestamp is older than **90 seconds**. If `_get_market_data()` returns a tick older than 90 seconds, it returns `None` for that symbol — no signal is generated, no order is placed.

This protects against:
- LTP poller job falling behind (Zerodha API lag)
- Redis becoming temporarily unavailable
- WebSocket stream going silent

During the warm-up window (9:15–9:30 AM), stale ticks from the previous session's close are automatically rejected by this check.

### 2.7 Option Pricing

In paper trading mode, option premiums are **estimated** using an ATR-based model:

```
estimated_premium = ATR14_5min × sqrt(DTE / 252) × OTM_discount
```

Where `OTM_discount` reduces the premium for each strike interval away from ATM:
- 0 intervals OTM (ATM): 100% weight
- 1 interval OTM: ~65%
- 2 intervals OTM: ~42%
- 3 intervals OTM: ~27%

In **live mode**, actual option LTP is fetched from Zerodha `kite.ltp()` for all open contracts every cycle. ATR-based estimates are not used for live fills.

### 2.8 Strike Selection

Strikes are chosen using the **Black-Scholes delta model**:

- `find_delta_strike()` finds the strike where the option has the target delta
- Short legs target **delta ≈ 0.20** (roughly 80% probability of expiring worthless)
- Long legs (hedges) target **delta ≈ 0.10** (further OTM, cheaper hedge)

The ATM strike is rounded to the symbol's standard interval (e.g. RELIANCE: ₹50, HDFCBANK: ₹20, TCS: ₹100).

**ATR scaling (step 1):** LTPPoller supplies 5-minute ATR14. The sigma formula requires daily ATR. Without correction, sigma is ~7× too low and short strikes are placed only ~1% OTM instead of ~5-6% OTM. The engine first computes a realized-vol baseline:

```
_atr_sigma = atr_to_annualised_vol(atr_5min × sqrt(75), price)
             # _5MIN_ATR_SCALE = sqrt(375 min/day ÷ 5 min/bar) = sqrt(75)
```

With this correction, `_atr_sigma` ≈ 25-30% for typical F&O stocks.

**Live IV upgrade (step 2, live mode only):** Before calling `find_delta_strike()`, the engine calls `_get_live_sigma()` which fetches the ATM CE and PE quotes from Zerodha, solves their implied vols, and returns the average. This ensures that delta targets are met against the actual market pricing surface rather than a historical vol proxy.

```
sigma = _get_live_sigma(symbol, price, dte, interval, expiry, fallback=_atr_sigma)
        # Falls back to _atr_sigma in paper mode or if kite is unavailable
```

`_atr_sigma` is retained as the **realized vol baseline** for the HV/IV ratio filter (`market_iv / _atr_sigma ≥ 1.10`). Using live ATM IV in that denominator would measure volatility skew instead of the vol risk premium, which is the intended check.

### 2.9 Market Open Warm-Up

A 15-minute warm-up window blocks all **entry** signals until 9:30 AM. At 9:15 AM, the 5-min bar history from the previous session may produce misleading EMA/ATR readings for the current day's conditions. Exit checks and position management still run during warm-up.

```
9:15 AM  — Market opens. Exit checks run. No new entries.
9:16–9:29 — Each 5-min bar adds fresh intraday data to the EMA/ATR calculations.
9:30 AM  — Warm-up complete. Entry signals enabled.
```

---

## 3. Risk Filters

Every order passes through a multi-layer risk manager before being placed. Exit orders bypass most layers.

### 3.1 Risk Layers

| Layer | Check | Applies To |
|-------|-------|-----------|
| 1 | **Kill switch** — if manually activated, all orders rejected | All orders |
| 2 | **Daily PnL limit** — if combined realized + unrealized loss today ≥ 5% of capital, stop trading | All orders |
| 3 | **IV Rank / VIX gate** — premium-selling strategies blocked when options are too cheap | Entry (spread/condor) only |
| 4 | **Sector concentration** — max 2 positions per sector | Entry (new positions) only |
| 5 | **Per-strategy capital allocation** — each strategy has a fixed budget | Entry only |
| 6 | **Max open positions** — hard cap of 25 total legs | Entry only |
| 7 | **BUY exposure limit** — long option premium capped at 20% of capital per trade | Entry (BUY) only |

**Exit orders** (`is_exit_order=True`) skip layers 3–7. **Spread legs 2–4** (`is_spread_leg=True`) also skip layers 3–7 to prevent the hedge legs from being rejected independently.

### 3.2 Portfolio-Level PnL Cap (Layer 2 Details)

Layer 2 checks **combined** daily PnL — both realized and unrealized:

```python
total_daily_pnl = daily_realized_pnl + daily_unrealized_pnl
max_allowed_loss = -(initial_capital × 5%)
if total_daily_pnl ≤ max_allowed_loss → kill switch activated, all trading stops
```

With ₹3,00,000 capital, this caps total portfolio loss at ₹15,000 per day. Since unrealized losses count toward this limit, a single large open loss can trigger the cap even before any position closes.

When the daily loss limit triggers, the kill switch is automatically activated. **New entries are blocked. Exit orders are always allowed through — the kill switch never traps you in an open position.** Deactivate manually via the API or dashboard to re-enable entries.

### 3.3 IV Rank / VIX Gate (Layer 3 — Exact Thresholds)

This layer only applies to premium-**selling** strategies (Credit Spread, Iron Condor). Selling options when IV is low means collecting little premium for the risk taken.

| Check | Threshold | Action |
|-------|-----------|--------|
| IV Rank | < 0.30 (30th percentile) | Skip spread/condor entry — options too cheap |
| India VIX | < 14.0 | Skip spread/condor entry — market-wide IV too low |

EMA Crossover (which **buys** options) has no IV gate — it actually benefits from buying in low-IV environments before a move.

### 3.4 End-of-Day Square-Off

At **3:20 PM IST**, the engine closes **EMA Crossover single-leg positions only**:
- All open single-leg option positions: SELL at estimated current premium

**Credit spreads and iron condors are NOT closed at EOD.** They are multi-day theta strategies and are designed to be held overnight. They close only when their own exit conditions trigger:
- DTE falls below 7 (gamma risk near expiry)
- Underlying breaches a short strike (emergency stop)
- Short leg premium doubles (stop loss)
- Short leg decays to 25% (profit target)

This prevents premature theta capture loss. A spread opened on Day 1 with 25 DTE may not reach its 75% profit target for 10-15 days — closing it intraday would forfeit most of the expected profit.

---

## 4. Strategy 1: EMA Crossover

**File:** [src/strategies/ema_crossover.py](../src/strategies/ema_crossover.py)  
**Registered as:** `EMA_CROSSOVER`  
**Strategy ID:** `ema_crossover_v1`

### 4.1 Concept

Buys a **single-leg option** in the direction of a confirmed EMA crossover on 5-minute bars. When EMA20 crosses above EMA50, the stock is trending up → BUY a Call option. When it crosses below → BUY a Put option (reversal entry).

This strategy **buys** options (long premium). It benefits from a strong directional move after entry. It is used in high-volatility regimes (ATR% ≥ 1.2%) where the move is expected to be large enough to overcome premium decay.

### 4.2 Entry Conditions

**Step 1 — Crossover detection (on 5-min EMA bars):**

A crossover is detected by comparing current EMA values to the **previous cycle's** EMA values:

- **BUY signal:** EMA20 was ≤ EMA50 last cycle AND EMA20 > EMA50 this cycle
- **SELL signal:** EMA20 was ≥ EMA50 last cycle AND EMA20 < EMA50 this cycle

**Step 2 — Confirmation filter:**

The crossover must persist for **2 distinct 5-minute bars** before a trade fires. The engine tracks an `ohlc_bar_key` (the timestamp of the last completed 5-min candle). The pending count only increments when the bar key changes — engine cycles within the same 5-minute candle are ignored. If the crossover reverses before 2 bar confirmations, the pending count resets.

```
Bar 1 closes: EMA20 crosses above EMA50 → pending BUY (1/2)
Bar 2 closes: EMA20 still above EMA50   → CONFIRMED → BUY order placed
```

This ensures true 2-candle confirmation, not 2 engine-cycle confirmation within the same bar.

**Step 3 — Duplicate prevention:**

If a CE option for the symbol is already open, a new BUY CE order is skipped. The engine first closes any opposite-type option (PE) if open (reversal), then checks for an existing CE before placing.

**Step 4 — Volume Confirmation (RVOL filter):**

RVOL (Relative Volume) must be ≥ **1.3**. RVOL = current bar volume ÷ 20-bar average volume. A momentum crossover on below-average volume is not trusted — it may be a false breakout or thin market. A value of 0 (indicator not yet available) bypasses this check.

**Step 5 — ADX Trend Strength filter:**

ADX14 must be ≥ **25**. ADX < 25 indicates the trend is too weak to sustain a momentum trade; the EMA crossover is likely noise in a ranging market. A value of 0 bypasses this check.

**Step 6 — Multi-Timeframe (MTF) Confirmation:**

The 15-minute EMA direction must **agree** with the 5-minute EMA signal:
- BUY signal (5-min EMA20 > EMA50): requires 15-min EMA20 > 15-min EMA50
- SELL signal (5-min EMA20 < EMA50): requires 15-min EMA20 < 15-min EMA50

If the 15-min EMA alignment contradicts the 5-min signal (or 15-min data is unavailable), the entry is skipped. This eliminates counter-trend trades at intermediate timeframe resistance/support levels.

**Step 7 — Market open warm-up:**

No entries before 9:30 AM regardless of signals.

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
RELIANCE @ ₹2,850  |  ATR14 (5-min) = ₹45  →  ATR% = 1.58%
EMA20 (5-min) = 2,855  crossed above  EMA50 (5-min) = 2,840
Confirmed over 2 completed 5-min bars

→ BUY  RELIANCE25JUN2850CE  @ ₹62  (1 lot × 250 shares)
  Total outlay:  ₹15,500
```

Email: `ORDER PLACED — BUY 250 RELIANCE25JUN2850CE @ ₹62.00`

### 4.5 Exit Conditions (Priority Order)

#### Exit 1: DTE < 4 days
Forced close to avoid gamma explosion near expiry. Overrides all other checks.

#### Exit 2: Hard Stop Loss
Premium has fallen ≥ 50% from entry price.

```
Entry: ₹62  →  Stop at: ₹31  →  If current ≤ ₹31: EXIT
Max loss: ₹31 × 250 = ₹7,750
```

#### Exit 3: Profit Target
Premium has risen ≥ 100% from entry (doubled).

```
Entry: ₹62  →  Target at: ₹124  →  If current ≥ ₹124: EXIT
Profit: ₹62 × 250 = ₹15,500
```

#### Exit 4: Trailing Stop
Only activates **after** the position has been profitable. Tracks the peak premium seen. Exits if premium falls ≥ 25% from the peak.

```
Peak premium: ₹95
Trailing stop: ₹95 × 75% = ₹71.25
If current < ₹71.25: EXIT
```

### 4.6 Risk/Reward Profile

| Scenario | P&L per lot |
|----------|------------|
| Stop loss hit (50% premium loss) | −₹7,750 |
| Profit target hit (100% premium gain) | +₹15,500 |
| Trailing stop hit after 53% gain | +₹8,060 |

---

## 5. Strategy 2: Credit Spread

**File:** [src/strategies/credit_spread.py](../src/strategies/credit_spread.py)  
**Registered as:** `CREDIT_SPREAD`  
**Strategy ID:** `credit_spread_v1`

### 5.1 Concept

Sells premium and collects it upfront. A two-leg structure: SELL one leg (collect premium), BUY a further-OTM leg (cap the maximum loss). Time decay (theta) works in favour — the short leg loses value as expiry approaches, and we buy it back cheaper to close.

- **Bull Put Spread** — mild uptrend: SELL OTM put, BUY further-OTM put. Profit if stock stays above short strike.
- **Bear Call Spread** — mild downtrend: SELL OTM call, BUY further-OTM call. Profit if stock stays below short strike.

### 5.2 Entry Conditions (All must be met)

| # | Condition | Threshold |
|---|-----------|-----------|
| 1 | ATR% below low-volatility threshold | < 1.2% |
| 2 | EMA directional (not flat) | EMA spread% > 0.1% |
| 3 | No existing spread for this symbol | Must be flat |
| 3a | No existing condor for this symbol | Stacking guard — no spread on top of live condor |
| 4 | **Minimum DTE (fresh entry)** | **≥ 21 days** |
| 4a | **Minimum DTE (re-entry after same-day profit close)** | **≥ 14 days** |
| 4b | Not in `_exited_today` | Adverse exits (breach/SL) block same-day re-entry entirely |
| 5 | PCR aligns with spread direction | PCR filters by put-call sentiment |
| 6 | Short strike not crowded OI | Moves 1 interval further if crowded |
| 7 | Absolute net credit minimum | ≥ ₹350 total per spread |
| 8 | Net credit ≥ 20% of wing width | Guards risk/reward ratio |
| 9 | Margin available | (short − long strike) × lot_size |
| 10 | IV Rank ≥ 0.30 and VIX ≥ 14 | Premium must be worth selling |
| 11 | **VWAP alignment** | BULL_PUT_SPREAD: underlying ≥ VWAP × 0.995; BEAR_CALL_SPREAD: underlying ≤ VWAP × 1.005 |
| 12 | **HV/IV ratio** | Market implied vol (from live short-leg LTP) ÷ realized vol (sigma) ≥ 1.10 |
| 13 | **Market Breadth alignment** | BULL_PUT_SPREAD: breadth must not be ≤ 0.35 (heavily bearish market). BEAR_CALL_SPREAD: breadth must not be ≥ 0.65 (heavily bullish market). Neutral breadth (0.35–0.65) allows both directions. |
| 14 | **ADX range filter** | ADX14 must be between 15 and 30 (exclusive). ADX < 15 = no clear trend (condor regime); ADX > 30 = trend too strong for a directional spread (blowthrough risk). Both extremes are blocked. |
| 15 | **Event calendar** | No scheduled earnings / RBI MPC / Budget within 5 calendar days of today. See §2.5. |

**On condition 4 (DTE rationale):** With a DTE < 7 exit trigger, a fresh position needs at least 14 days of runway before the exit fires. The 21-day floor provides a full theta curve segment — theta decay accelerates from DTE 25 toward DTE 7, capturing the steepest portion of the curve. Entering at DTE 21 means the position typically closes profitably (75% target) before DTE 7 is reached.

**On condition 4a (re-entry):** If a spread closes at 75% profit on day 1 (DTE 18 remaining), the system may re-enter the same symbol the same day at DTE ≥ 14. This enables two complete theta trades per expiry cycle on the same symbol without carrying excessive gamma risk into the final week.

**On condition 11 (VWAP):** VWAP is computed from 10 days of 5-minute candles — it is a medium-term trend anchor, not an intraday signal. A BULL_PUT_SPREAD selling puts below the market is only appropriate when the stock is trading at or above the medium-term VWAP (bullish context). Trading against VWAP increases the probability the short put gets tested.

**On condition 12 (HV/IV ratio):** Selling options when market implied volatility exceeds realized volatility by at least 10% ensures there is an IV risk premium to collect. If the ratio is below 1.10, the market is pricing options cheaply relative to actual realized movement — the expected edge is absent.

**On condition 8 (risk/reward):** A spread with a 50-point wing must collect at least 10 points of premium per share. If a spread can only collect 5 points on a 50-point wing, the R/R is 1:9 (risk ₹45 to make ₹5), which requires a very high win rate to be profitable over time. The minimum 20% ensures R/R never exceeds approximately 1:4.

### 5.3 Strike Selection (Delta-Based)

| Leg | Target Delta | Approximate Position |
|-----|-------------|---------------------|
| Short PE (bull put) | −0.20 | ~1.5–2 intervals below current price |
| Long PE (bull put) | −0.10 | 2+ intervals further OTM from short |
| Short CE (bear call) | +0.20 | ~1.5–2 intervals above current price |
| Long CE (bear call) | +0.10 | 2+ intervals further OTM from short |

Delta ≈ 0.20 means the short option has ~80% probability of expiring worthless.

### 5.4 Entry Example

```
INFY @ ₹1,620  |  ATR14 (5-min) = ₹16  →  ATR_daily = ₹16 × √75 = ₹138.6
σ = (₹138.6 / ₹1,620) × √252 ≈ 0.285 (28.5% annualized vol)
DTE = 25  ✓ (≥ 21 minimum)  |  Lot = 400
EMA20 = 1,625 > EMA50 = 1,608  →  EMA spread% = 1.05%  →  BULL_PUT_SPREAD
VWAP (10-day) = 1,615  |  INFY (₹1,620) ≥ VWAP × 0.995 (₹1,607)  ✓

Strike selection (delta model, σ = 28.5%):
  Short PE: 1,510  (delta ~−0.20, ~6.8% OTM)
  Long  PE: 1,480  (delta ~−0.10)
  Wing width: 30 points
  Minimum credit: 30 × 20% = ₹6 per share

Live LTP from kite.ltp():
  SELL INFY25JUL1510PE @ ₹12.50
  BUY  INFY25JUL1480PE @ ₹5.00
  Net credit = ₹7.50  ✓ (≥ ₹6 minimum)
  Total credit = ₹7.50 × 400 = ₹3,000  ✓ (≥ ₹350 minimum)

HV/IV check:
  Implied vol from ₹12.50 short leg price ≈ 32%
  IV/HV ratio = 32% / 28.5% = 1.12  ✓ (≥ 1.10)

Max profit: ₹3,000  |  Max loss: (₹30 − ₹7.50) × 400 = ₹9,000  |  R/R = 1:3
```

Email: `CREDIT SPREAD OPENED — BULL_PUT_SPREAD INFY | Net credit: ₹7.50 × 400 = ₹3,000 | DTE=25`

### 5.5 Exit Conditions (Priority Order)

#### Exit 1: DTE < 7 days
Close spread before gamma risk explodes near expiry. Locks in most theta profit.

#### Exit 2: Underlying Breaches Short Strike
- BULL_PUT_SPREAD: underlying price < short put strike  
- BEAR_CALL_SPREAD: underlying price > short call strike

Emergency stop — the short leg is moving into the money.

#### Exit 3: DTE-Tiered Profit Target

The profit target threshold scales with the remaining DTE — closer to expiry we take profit sooner to avoid gamma risk:

| DTE | Short leg target (% of sold value) | Profit captured |
|-----|------------------------------------|-----------------|
| > 21 days | ≤ 25% | 75% |
| 15 – 21 days | ≤ 35% | 65% |
| ≤ 14 days | ≤ 45% | 55% |

```
DTE = 10, short sold at ₹18 → target at ₹18 × 45% = ₹8.10
If current short ≤ ₹8.10: EXIT (55% profit captured)
```

**Rationale:** With fewer days to expiry, a sudden move can quickly convert a 55%-profitable position into a loss. Taking profit earlier at DTE ≤ 14 locks in gains before gamma amplification. At DTE > 21, the full 75% target remains — there is still significant theta decay to capture.

#### Exit 4: Stop Loss
Short leg rises to ≥ 2× sold value.

```
Short sold at ₹18 → stop at ₹36
If current short ≥ ₹36: EXIT
```

#### Exit 5: Delta-Based Adverse Exit

If the short leg's Black-Scholes delta (computed from current price, ATR-derived vol, and remaining DTE) exceeds **|δ| > 0.40**, the position is exited. At entry the short strike has delta ≈ 0.20 (~80% probability of expiring worthless). When delta grows to 0.40+, the option has become ~40% likely to expire in-the-money — the original thesis is invalidated.

```
BULL_PUT_SPREAD short put — entry delta: −0.20
If current delta < −0.40 (e.g. −0.43): EXIT
```

This exit fires at the same priority as stop loss — whichever triggers first wins.

### 5.6 PnL Calculation

```
Net PnL = [(short_sold − short_close) − (long_paid − long_close)] × lot_size

Profit target example:
  Short: ₹18 → ₹4.50  =  +₹13.50/share
  Long:  ₹8  → ₹2.00  =  −₹6.00/share
  Net = (₹13.50 − ₹6.00) × 400 = ₹3,000 profit (75% of max ₹4,000)

Stop loss example:
  Short: ₹18 → ₹36     =  −₹18/share
  Long:  ₹8  → ₹14.00  =  +₹6/share
  Net = (−₹18 + ₹6) × 400 = −₹4,800 loss
```

---

## 6. Strategy 3: Iron Condor

**File:** [src/strategies/iron_condor.py](../src/strategies/iron_condor.py)  
**Registered as:** `IRON_CONDOR`  
**Strategy ID:** `iron_condor_v1`

### 6.1 Concept

A four-leg, defined-risk structure for sideways markets. Collects premium from both sides simultaneously: a put spread below the current price and a call spread above. Profit if the stock stays within the two short strikes at expiry.

```
PUT  wing: SELL OTM Put (delta ~−0.20) + BUY further OTM Put (delta ~−0.10)
CALL wing: SELL OTM Call (delta ~+0.20) + BUY further OTM Call (delta ~+0.10)
```

Max profit = total net credit (both short legs expire worthless).  
Max loss = wider wing spread minus net credit (capped, fully defined).

### 6.2 Entry Conditions (All must be met)

| # | Condition | Threshold |
|---|-----------|-----------|
| 1 | ATR% below low-volatility threshold | < 1.2% |
| 2 | EMA is flat (no directional trend) | EMA spread% < 0.1% |
| 3 | No existing condor for this symbol | Must be flat |
| 3a | No existing spread for this symbol | Stacking guard — no condor on top of live spread |
| 4 | **Minimum DTE (fresh entry)** | **≥ 21 days** |
| 4a | **Minimum DTE (re-entry after same-day profit close)** | **≥ 14 days** |
| 4b | Not in `_exited_today` | Adverse exits block same-day re-entry entirely |
| 5 | Absolute net credit minimum | ≥ ₹600 total (covers 8-order round-trip fees) |
| 6 | Each wing credit ≥ 20% of wing width | Both wings individually checked |
| 7 | Margin available | max_wing_width × lot_size |
| 8 | IV Rank ≥ 0.30 and VIX ≥ 14 | Premium must be worth selling |
| 9 | **Market Breadth neutral** | Breadth must be between 0.35 and 0.65 — condors need a market neither clearly advancing nor declining. |
| 10 | **ADX low filter** | ADX14 must be < 20. A rising ADX (≥ 20) indicates a developing trend; condors are exposed in trending markets. |
| 11 | **Event calendar** | No scheduled earnings / RBI MPC / Budget within 5 calendar days of today. See §2.5. |

### 6.3 Leg Placement — All-or-Nothing

All 4 legs are placed sequentially. If any leg fails, all previously placed legs are immediately **unwound** (reversed). This prevents being left with a naked short leg.

```
Order: SELL put short → BUY put long → SELL call short → BUY call long
If leg 3 fails: reverse legs 1 and 2 immediately, abandon entry
```

### 6.4 Entry Example

```
HDFCBANK @ ₹1,750  |  ATR14 (5-min) = ₹14.50  →  ATR% = 0.83%
EMA20 = 1,750.5,  EMA50 = 1,750.0  →  EMA spread% = 0.03%  (flat)
DTE = 20,  Lot = 550

Put wing:  SELL 1700PE @ ₹12  |  BUY 1650PE @ ₹5   |  Credit = ₹7, width = 50
  20% check: ₹7 ≥ 50 × 20% = ₹10? NO — trade skipped if credit < ₹10.

(Better conditions with wider credit, e.g.:)
Put wing:  SELL 1700PE @ ₹15  |  BUY 1650PE @ ₹4   |  Credit = ₹11 ✓
Call wing: SELL 1800CE @ ₹14  |  BUY 1850CE @ ₹3   |  Credit = ₹11 ✓
Net credit = ₹22.00  |  Total = ₹22 × 550 = ₹12,100  ✓

Max profit: ₹12,100  |  Max loss: (₹50 − ₹22) × 550 = ₹15,400  |  R/R ~1:1.3
```

### 6.5 Exit Conditions (Priority Order)

#### Exit 1: DTE < 7 days
Close before gamma explosion.

#### Exit 2: Underlying Breaches Either Short Strike
```
Price < put short strike → emergency stop (put wing losing)
Price > call short strike → emergency stop (call wing losing)
```
Closes the entire condor even if only one wing is breached.

#### Exit 3: Either Short Leg Doubles (Stop Loss)
```
Put short sold at ₹15 → stop at ₹30 (2×)
Call short sold at ₹14 → stop at ₹28 (2×)
If either triggers → close entire condor
```

#### Exit 4: DTE-Tiered Profit Target (Either Wing)

Same DTE-tiered thresholds as credit spreads — the profit target for each short wing scales down as expiry approaches:

| DTE | Short leg target (% of sold value) | Profit captured |
|-----|------------------------------------|-----------------|
| > 21 days | ≤ 25% | 75% |
| 15 – 21 days | ≤ 35% | 65% |
| ≤ 14 days | ≤ 45% | 55% |

The threshold never goes below any VIX-spike-adjusted threshold (whichever is higher triggers).

```
Either short leg decays to ≤ threshold × sold value → EXIT entire condor.
```

**Rationale for OR logic:** If one wing decays to profit threshold, the underlying has moved toward that side. Directional risk on the opposite wing increases. Lock in the winner and exit the full structure.

#### Exit 5: Delta-Based Adverse Exit (Either Wing)

If either the put short or call short leg's |delta| exceeds **0.40**, the entire condor is closed:
```
Put short entry delta ≈ −0.20 → exit if |delta| > 0.40
Call short entry delta ≈ +0.20 → exit if |delta| > 0.40
```
Either condition triggers a full condor exit, since the breached wing is increasingly likely to expire in-the-money.

### 6.6 PnL Calculation

```
Net PnL = [(put_short_sold − put_short_close)
         + (call_short_sold − call_short_close)
         − (put_long_paid − put_long_close)
         − (call_long_paid − call_long_close)] × lot_size
```

---

## 7. Daily Cycle — How Everything Fits Together

### 7.1 Timeline

```
08:30 AM  Zerodha auto-authentication (daily cron)
           Lot sizes refreshed from kite.instruments("NFO")

09:00 AM  Platform running
           LTP poller fetches 10-day 5-min OHLC history from Zerodha
           RS Ranker ranks all 41 symbols by relative strength

09:15 AM  Market opens
           Signal cycle starts (every 60 seconds)
           Exit check starts (every 10 seconds — faster stop-loss response)
           ⚠ WARM-UP: exit checks run, entries blocked

09:30 AM  Warm-up complete — entries enabled

09:30 AM – 03:20 PM  (every 60 seconds — signal cycle):
           1. Check market data freshness — skip symbols with >90s stale data
           2. Expire stale pending orders (>5 min old)
           3. Check spread exits (_check_spread_exits)
           4. Check condor exits (_check_condor_exits)
           5. Check single-leg exits (_check_open_option_exits)
           6. Re-read positions (post-exit risk state refresh)
           7. StrategyMonitor evaluates rolling PF / drawdown
           8. MarketRegimeDetector updates regime label
           9. PortfolioAnalyzer logs concentration / correlation warnings
           10. Log portfolio delta ([PortfolioDelta] bulls/bears/condors)
           11. Entry signals: each strategy × each symbol in its pool

09:30 AM – 03:20 PM  (every 10 seconds — exit check only):
           - _check_spread_exits + _check_condor_exits
           - _exit_cycle_lock prevents concurrent execution with 60s cycle

03:20 PM  Square-off: EMA Crossover single-leg positions closed
           ⚠ Credit spreads and iron condors are NOT closed — held overnight

03:45 PM  EOD report email sent; _profit_closed_today cleared for next session
03:30 PM  Market closes
```

### 7.2 What Happens When Data is Unavailable

| Scenario | Behaviour |
|----------|-----------|
| Redis tick missing for a symbol | `_get_market_data` returns None → symbol skipped this cycle |
| Redis tick older than 90 seconds | Same as missing — symbol skipped |
| Redis completely unavailable | No market data → no entries; intraday stop-loss/trailing-stop exits also disabled until Redis recovers. 3:15 PM square-off still fires (engine holds the expiry timestamp in memory), using entry price as fallback exit price in paper mode. |
| Zerodha broker unavailable (paper mode) | Orders rejected at PaperBroker level, logged as FAILED |
| Kill switch activated | New entries blocked; exit orders are **always allowed through** regardless of kill switch state |

---

## 7A. Multi-Day Holding — Loss Mitigation Controls

Credit spreads and iron condors are multi-day positions. The following controls guard against adverse multi-day scenarios.

> **v3.1 additions (2026-07-03):** Controls 7A.10–7A.14 were added as part of a 7-improvement package. Controls 7A.5–7A.9 below are unchanged.

### 7A.1 DTE Floors

| Gate | Value | Purpose |
|------|-------|---------|
| Entry (fresh) | DTE ≥ 21 | Ensures 14-day runway before the DTE < 7 exit fires |
| Entry (re-entry) | DTE ≥ 14 | Allows 2nd trade in same expiry cycle after profit close |
| Exit trigger | DTE < 7 | Avoids gamma explosion in final week |

### 7A.2 Adverse Exit Circuit Breaker

Redis key `sl_freq:{symbol}` (5-day TTL) counts adverse exits (breach/SL) for each symbol. After 2 adverse exits within 5 trading days on the same symbol, a circuit breaker fires and blocks further entries on that symbol until the TTL expires. This prevents repeatedly entering a symbol that has been stopped out twice in a row.

### 7A.3 Regime Shift Exit

If `MarketRegimeDetector` detects a significant regime change while a position is open (e.g. VIX spikes from RANGE_BOUND into VOLATILE), open positions may be flagged for exit. This prevents holding through a volatility regime transition that invalidates the original entry thesis.

### 7A.4 VIX Spike Early Exit

When India VIX spikes (VOLATILE regime detected), the profit target is tightened from 75% to 60% (short leg decays to 40% of sold price instead of 25%). This captures available profit faster rather than waiting for the full 75% while IV expansion is increasing adverse wing risk.

### 7A.5 GTT Backstop (Zerodha Exchange-Level Stop)

For live mode, a Good Till Triggered (GTT) order is placed on the short leg at **2.5× the entry premium** after every spread or condor entry. If the platform goes offline overnight (network failure, crash) and the short leg doubles or more, Zerodha's exchange-level GTT fires automatically — without the platform running.

This is a last-resort backstop only. The engine's own stop-loss at 2× will typically fire first during normal operation. The GTT at 2.5× is the safety net for platform-offline scenarios.

### 7A.6 DTE Roll Detection on Restart

If the platform restarts while a multi-day position is open and the DTE in Redis has changed from the stored entry DTE (indicating the position rolled through an expiry), `_close_on_first_cycle` flags the symbol for immediate close on the next signal cycle rather than continuing to hold a potentially mismatched position.

### 7A.7 Portfolio Delta Logging

Every signal cycle logs `[PortfolioDelta] bulls=N bears=N condors=N` — a count of bullish spreads (BULL_PUT_SPREAD), bearish spreads (BEAR_CALL_SPREAD), and iron condors currently open. This gives a quick directional bias check; a portfolio of 4 BULL_PUT_SPREADs and 0 bearish structures is concentrated long in an implicit way.

### 7A.8 Sector Concentration Check

Maximum 2 open structures per sector (`MAX_SECTOR_POSITIONS = 2`). Banking sector with HDFCBANK + ICICIBANK open already blocks further banking entries. This prevents correlated sector blow-ups (two Banking spreads losing simultaneously in a sector sell-off).

### 7A.9 Re-Entry After Profit

`_profit_closed_today` tracks symbols where a position closed at 75%+ profit the same day. These symbols are eligible for re-entry at DTE ≥ 14 (instead of the 21-day fresh-entry floor). This enables a second trade in the same expiry cycle when conditions remain favourable — roughly doubling the theta trades per expiry without increasing overnight gamma risk.

Adverse exits (`_exited_today`) do NOT allow re-entry at any DTE floor until the next session.

### 7A.10 DTE-Tiered Profit Targets

The 75% profit target from 7A (flat across all DTE) has been replaced with a tiered schedule that takes profit earlier as expiry approaches. See §5.5 Exit 3 and §6.5 Exit 4 for the full table. The tiering reflects the non-linear gamma expansion in the final two weeks before expiry.

### 7A.11 Delta-Based Exit

When a short leg's Black-Scholes |delta| exceeds 0.40 (vs. ~0.20 at entry), the position is exited regardless of other conditions. This is a probability-based exit: at |delta| > 0.40, the short strike has more than a 40% chance of expiring in-the-money. See §5.5 Exit 5 and §6.5 Exit 5.

### 7A.12 ADX Entry Filter

Credit spreads require ADX14 between 15 and 30 (mild trend confirmed, not too strong). Iron condors require ADX14 < 20 (low trend — market is ranging). These filters prevent entering credit spreads in choppy markets and condors in trending markets, where each structure would be exposed to the wrong regime.

### 7A.13 Market Breadth Filter

`market:breadth` (advancing / total) is computed each poll cycle from all 41 symbols:
- Credit spreads: BEAR_CALL_SPREAD blocked when breadth > 0.65 (too bullish to sell calls); BULL_PUT_SPREAD blocked when breadth < 0.35 (too bearish to sell puts).
- Iron condors: blocked when breadth < 0.35 or > 0.65 (market not neutral enough for range-bound strategy).
- Neutral zone (0.35–0.65) is permissive for all structures.

### 7A.14 Event / Earnings Calendar Filter

No new spread or condor entry is made within 5 calendar days of a scheduled corporate event (quarterly results, board meeting) or market-wide macro event (RBI MPC decision, Union Budget). The calendar is auto-refreshed every Monday from NSE's public API. See §2.5 for full details.

---

## 8. Position Sizing & Margin Rules

### 8.1 Lot Sizes

Lot sizes are fetched from Zerodha's live instrument data every morning (`kite.instruments("NFO")`) and cached in Redis. The values in `src/core/constants.py` serve as hardcoded fallbacks for startup-before-auth scenarios. Sample lot sizes:

| Symbol | Lot Size | Symbol | Lot Size |
|--------|---------|--------|---------|
| RELIANCE | 250 | SBIN | 1500 |
| TCS | 150 | ITC | 3200 |
| INFY | 300 | M&M | 700 |
| HDFCBANK | 550 | COALINDIA | 4200 |
| ICICIBANK | 700 | TATASTEEL | 5500 |
| BAJFINANCE | 125 | MARUTI | 25 |

All positions are placed in exactly 1 lot. No position scaling currently. Full lot size table in `src/core/constants.py → FNO_LOT_SIZES`.

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

The PaperBroker simulates realistic execution with three components applied to every order:

**A) Bid-Ask Spread Slippage**

Options are quoted at the mid-price. The engine's estimated premium is treated as the mid. Fills happen at the bid (for SELL) or ask (for BUY) — always worse than mid.

| Premium Range | Half-Spread Applied | Typical Scenario |
|--------------|--------------------|-|
| ≤ ₹0.30 | 40% of premium | Deep-OTM hedge, near-expiry |
| ₹0.31 – ₹0.75 | 20% of premium | Cheap far-OTM leg |
| ₹0.76 – ₹2.00 | 10% of premium | Standard hedge leg |
| ₹2.01 – ₹5.00 | 6% of premium | Short leg near expiry |
| > ₹5.00 | 3% of premium | Liquid ATM/ITM contract |

Example: BUY at estimated ₹62 → half-spread = 3% → fill at ₹63.86.  
Example: SELL at estimated ₹62 → fill at ₹60.14.

**B) Stochastic Rejection + Enhanced Slippage**

In live trading, some orders fail (no market maker, margin edge cases, exchange connectivity). Cheap options also fill at worse prices than the bid-ask model alone because the last-traded price can be stale when the order reaches the exchange.

| Premium Range | Rejection Rate | Max Extra Slippage |
|--------------|---------------|-------------------|
| ≤ ₹0.30 | 5% | 30% of premium |
| ₹0.31 – ₹1.00 | 2% | 15% of premium |
| ₹1.01 – ₹5.00 | 1% | 5% of premium |
| > ₹5.00 | 0.5% | 0% |

Rejected orders raise `ValueError("Order rejected by exchange: <reason>")`. The engine's unwind logic handles this identically to an insufficient-margin rejection, ensuring consistent position cleanup.

Extra slippage is sampled uniformly from 0 → max (expected cost = max/2). Applied in addition to the bid-ask half-spread.

**C) Transaction Fees (per order)**

Modelled on actual Zerodha F&O fee structure:

| Fee Type | Rate |
|---------|------|
| Brokerage | ₹20 flat (or 0.03% of turnover, whichever lower) |
| STT | 0.1% of turnover (SELL side only) |
| NSE exchange charges | 0.053% of turnover |
| GST | 18% on (brokerage + exchange charges) |
| SEBI charges | ₹10 per crore of turnover |
| Stamp duty | 0.003% of turnover (BUY side only) |

### 9.2 Live Trading Fill Model

In live mode, orders are routed to Zerodha. The engine submits **market/limit orders at the estimated premium**, and the actual fill price returned by Zerodha is recorded. The fill price diverges from the estimated premium in proportion to actual bid-ask spread and market impact at the time of execution.

### 9.3 Slippage Tracking in Trade Journal

Every trade records `total_slippage_pts` (sum of actual slippage across all legs × lot size) in the trade journal. Review this field to compare paper vs. live execution quality over time.

---

## 10. Trade Journal Schema

Every trade (entry + exit) is logged to the `trade_journal` table. This is the primary source for all PnL analytics and strategy optimization.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BigInt | Auto-increment primary key |
| `strategy_name` | String(50) | e.g. `ema_crossover_v1`, `credit_spread_v1` |
| `underlying` | String(30) | Stock symbol, e.g. `RELIANCE` |
| `structure_type` | String(30) | `SINGLE_LEG`, `BULL_PUT_SPREAD`, `BEAR_CALL_SPREAD`, `IRON_CONDOR` |
| `contracts` | String(500) | JSON list of all contract symbols in the trade |
| `entry_time` | Timestamp | Time of first leg fill |
| `entry_price` | Decimal | Net credit (spread/condor) or debit (single-leg) per share |
| `quantity` | Integer | Lot size |
| `regime_atr_pct` | Float | ATR% at entry — identifies which regime triggered |
| `ema_spread_pct` | Float | EMA spread% at entry |
| `iv_rank` | Float | IV rank [0, 1] at entry, None if unknown |
| `vix_at_entry` | Float | India VIX at entry |
| `day_of_week` | Integer | 0 = Monday … 4 = Friday |
| `hour_of_day` | Integer | Hour of entry (IST, 9–15) |
| `exit_time` | Timestamp | Time of last leg close |
| `exit_price` | Decimal | Net debit paid to close |
| `exit_reason` | String(200) | e.g. `DTE=6 < 7`, `Stop loss`, `Target hit` |
| `pnl` | Decimal | Realized PnL (net of slippage; before brokerage fees) |
| `hold_days` | Integer | Calendar days from entry to exit |
| `atr_at_exit` | Float | ATR% when position closed |
| `vix_at_exit` | Float | India VIX when position closed |
| `regime_label` | String(30) | `TRENDING` / `RANGE_BOUND` / `VOLATILE` |
| `total_slippage_pts` | Float | Total execution slippage (all legs × lot) |

**Useful queries:**
```sql
-- Win rate by strategy
SELECT strategy_name,
       COUNT(*) AS trades,
       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*) AS win_rate,
       AVG(pnl) AS avg_pnl
FROM trade_journal WHERE exit_time IS NOT NULL
GROUP BY strategy_name;

-- Best/worst exit reasons
SELECT exit_reason, COUNT(*) AS count, AVG(pnl) AS avg_pnl
FROM trade_journal WHERE exit_time IS NOT NULL
GROUP BY exit_reason ORDER BY avg_pnl;

-- Day-of-week performance
SELECT day_of_week, COUNT(*) AS trades, SUM(pnl) AS total_pnl
FROM trade_journal WHERE exit_time IS NOT NULL
GROUP BY day_of_week ORDER BY day_of_week;
```

---

## 11. Email Alerts Reference

All alerts have `[Falcon Trader]` prefix in the subject line.

| Event | When Sent |
|-------|----------|
| `ORDER PLACED` | EMA Crossover option bought (order confirmed) |
| `CREDIT SPREAD OPENED` | Bull put or bear call spread entered |
| `IRON CONDOR OPENED` | Iron condor 4-leg structure entered |
| `POSITION CLOSED` | Single-leg option exited |
| `CREDIT SPREAD CLOSED` | Spread closed for any exit reason |
| `IRON CONDOR CLOSED` | Condor closed for any exit reason |
| `EOD REPORT` | End-of-day summary at 3:25 PM |
| `RISK ALERT` | Kill switch activated or daily loss limit triggered |

**Alerts are NOT sent when:**
- An order is REJECTED_BY_RISK — position was not opened/closed, no alert
- An order FAILED at broker level — same
- Email alerts are paused via the Admin dashboard

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
| `signal_confirm_bars` | 2 | Crossover must persist on 2 distinct 5-min bars |
| `min_dte` | 10 | Minimum days-to-expiry before entry |
| `max_dte` | 25 | Maximum days-to-expiry before entry |

### Credit Spread (`credit_spread_v1`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fast_period` | 20 | Fast EMA period |
| `slow_period` | 50 | Slow EMA period |
| `low_vol_threshold` | 1.2 | ATR% below which credit spreads activate |
| `profit_close_pct` | 0.25 | Close when short decays to 25% of sold price (75% profit) |
| `stop_loss_multiple` | 2.0 | Close when short rises to 2× sold price |
| `min_dte` | 7 | DTE at which exit is forced (gamma risk) |
| `min_credit_pct` | 0.20 | Minimum net credit as fraction of wing width (hard-coded) |

Engine-level constants (not strategy parameters):

| Constant | Value | Description |
|----------|-------|-------------|
| `_ENTRY_MIN_DTE` | 21 | Minimum DTE for a fresh credit spread entry |
| `_REENTRY_MIN_DTE` | 14 | Minimum DTE for re-entry after same-day profit close |
| `_vwap_buffer` | 0.005 (0.5%) | Price must be within 0.5% of VWAP on the correct side |
| `_5MIN_ATR_SCALE` | `sqrt(75)` ≈ 8.66 | Converts 5-min ATR to daily equivalent before sigma computation |

### Iron Condor (`iron_condor_v1`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fast_period` | 20 | Fast EMA period |
| `slow_period` | 50 | Slow EMA period |
| `low_vol_threshold` | 1.2 | ATR% below which condor activates |
| `flat_threshold` | 0.1 | EMA spread% below which EMA is flat |
| `profit_close_pct` | 0.25 | Close entire condor when EITHER short leg decays to 25% |
| `stop_loss_multiple` | 2.0 | Close condor if EITHER short leg rises to 2× |
| `min_dte` | 7 | DTE at which exit is forced (gamma risk) |
| `min_wing_credit_pct` | 0.20 | Minimum credit per wing as fraction of wing width (hard-coded) |

Same engine-level constants as credit spread apply (DTE floors, sigma scaling, VWAP buffer).

---

## 13. Strategy Performance Metrics

The `StrategyMonitor` class evaluates performance every signal cycle using the `trade_journal` table. If a strategy shows statistical deterioration, it can be automatically paused (strategy kill switch).

### 13.1 Key Metrics to Track

| Metric | Formula | Healthy Target |
|--------|---------|---------------|
| **Win Rate** | Winning trades ÷ total closed trades | > 55% (spreads/condors); > 45% (EMA crossover) |
| **Average Win** | Mean PnL of profitable trades | — |
| **Average Loss** | Mean PnL of losing trades | — |
| **Profit Factor** | Total gross profit ÷ total gross loss | > 1.5 |
| **Expectancy** | (Win rate × avg win) − (loss rate × avg loss) | > 0 |
| **Max Drawdown** | Peak-to-trough cumulative PnL decline | < 15% of capital |
| **Sharpe (daily)** | Mean daily PnL ÷ std dev of daily PnL × √252 | > 1.0 |

### 13.2 StrategyMonitor Auto-Kill Thresholds

StrategyMonitor pauses a strategy automatically when it detects deterioration. Minimum 30 completed trades are required before evaluation — fewer trades produce statistically meaningless signals (2 losses in 5 trades would give PF = 0.2, which means nothing without sample size). Once 30+ trades accumulate, rolling profit factor and drawdown are checked every cycle.

Current thresholds: `ROLLING_WINDOW=30, ROLLING_PF_FLOOR=0.9, DRAWDOWN_MULTIPLIER=1.5, MIN_TRADES_REQUIRED=30`. See `src/risk/strategy_monitor.py`.

### 13.3 Reviewing Performance

The `/analytics/pnl-summary` API endpoint returns aggregate PnL. For per-strategy breakdown, query `trade_journal` directly or use the dashboard's Risk & PnL page.

---

## 14. Assumptions & Limitations

These are the known constraints and design assumptions of the current system. Review before deploying with real capital.

### 14.1 Data Assumptions

| Assumption | Risk | Mitigation |
|-----------|------|-----------|
| EMA/ATR derived from 5-min candles via Zerodha API | API outage or rate limit → stale data | 90-second staleness circuit breaker; warn in logs |
| Strike selection uses ATR-derived implied volatility, not live option chain Greeks | Strike placement may be slightly off from ideal delta | Delta model calibrated against NSE ranges; acceptable for paper trading |
| Option premium estimates (paper mode) use ATR model, not live IV | Paper P&L may differ from live | In live mode, actual fills replace all estimates |
| VIX and OI/PCR from Zerodha snapshot | Intraday OI data may lag 15 minutes | Used only for entry filtering, not exit timing |

### 14.2 Operational Assumptions

| Assumption | Notes |
|-----------|-------|
| Sufficient option liquidity | All 41 F&O symbols have reasonable liquidity; far-OTM hedge legs may have wider bid-ask spreads |
| Multi-day spread/condor holding | Credit spreads and iron condors are held overnight. Ensure sufficient margin is maintained across overnight sessions. |
| EMA Crossover positions are intraday | Single-leg positions closed at 3:20 PM regardless of PnL |
| Zerodha API available during market hours | Platform is Zerodha-dependent for both data and execution |
| Multi-session state preserved | Redis persists active spreads, condors, and today's exit history across restarts; DB state is permanent |
| Overnight gap risk | Multi-day positions are exposed to overnight gap risk (large gap-down/up on news). GTT backstop orders provide exchange-level protection at 2.5× entry premium on short legs. |
| Not validated on black swan events | Circuit breakers and daily limits reduce risk; extreme gap-downs or circuit-limit events are not fully modelled |
| NSE F&O equities only | No index options (NIFTY/BANKNIFTY), no currencies, no commodities |

### 14.3 Known Gaps (Roadmap)

| Gap | Impact | Priority |
|-----|--------|---------|
| Position correlation cap (> 0.8 between symbols) | Two banking stocks may move together | Medium |
| Partial-lot sizing (scale in/out) | Currently always 1 lot | Low |
| Multi-expiry support (weekly options) | Near-month only currently | Low |
| Monte Carlo / Walk-Forward backtesting integration with live parameter selection | Parameters currently fixed at startup | Low |

### 14.4 Not Suitable For

- High-frequency trading (minimum signal granularity = 60 seconds for entries; 10 seconds for exits)
- Stocks outside the 41-symbol F&O universe
- Index options (NIFTY/BANKNIFTY), currency futures, or commodity derivatives
- Capital requirements below ₹3,00,000 (margin requirements for multi-day option spreads require sufficient buffer)

**Note:** Overnight / multi-day holding of credit spreads and iron condors is now fully supported and is the intended operating mode. EMA Crossover single-leg positions remain intraday-only.

---

*For infrastructure, API, and platform documentation see [PLATFORM_DOCUMENTATION.md](PLATFORM_DOCUMENTATION.md).*  
*Source files: [src/strategies/](../src/strategies/) | [src/live_trading/live_trading_engine.py](../src/live_trading/live_trading_engine.py) | [src/risk/risk_manager.py](../src/risk/risk_manager.py)*
