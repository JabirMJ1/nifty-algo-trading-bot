# Strategy Notes

This document explains what the bot actually trades, why each rule exists, and how the strategy
evolved. It describes the code as it is today (`modules/strategy/adaptive_strategy.py`); where the
code and an older doc disagree, the code wins.

> Not financial advice. These are rules I built and am studying, not a claim of proven edge.
> No performance figures are given — see the README's *Status & honest limitations*.

---

## 1. Active strategy: Adaptive scoring (V3)

**Instrument:** Nifty (futures, or ATM weekly options when `TRADE_OPTIONS=true`).
**Candles:** 5-minute (configurable). **Style:** intraday trend-following with a mean-reversion
branch in high-volatility regimes. No positions are carried overnight.

The strategy is a pipeline: **time gates → day-quality filter → directional gates → scoring →
threshold → risk module**. Most candles are rejected before scoring ever happens; that is
intentional. The goal is few, high-conviction trades (max 2 per symbol per day).

### 1.1 Time gates

| Time (IST) | Rule |
|---|---|
| 09:15 – 09:30 | Observe only. Records the **opening-range direction** (BULLISH / BEARISH / NEUTRAL: close vs open ±0.1%). |
| 09:30 – 10:30 | No signals. Lets the trend establish and the morning metrics form. |
| 10:30 – 14:30 | Signals allowed. |
| 14:30 onward | No new entries (leaves time before the 15:30 forced square-off). |

The 10:30 start came from debugging: an earlier, stricter momentum-*acceleration* check combined
with an earlier gate produced almost no signals, because by the time the window opened momentum had
already peaked. Switching to momentum *direction* (see MACD gate below) unblocked signal flow.

### 1.2 Day-quality filter (trend days only)

Using the 09:15–10:30 candles the strategy requires **both**:

- Morning range ≥ **0.6%** of price (volatility expansion), and
- Net morning move |close − open| ≥ **0.3%** of the open (clear direction).

Choppy, narrow-range mornings produce no trades at all. The morning's direction
(`close > open`) then becomes the **only direction allowed** that day — buys on a bullish morning,
sells on a bearish one.

### 1.3 Hard directional gates

A BUY is blocked (score forced to 0) unless **all** of these hold; SELL is the mirror image:

| Gate | BUY requires | SELL requires |
|---|---|---|
| ADX(14) | ≥ 20 (otherwise no signal either way — ranging market) | same |
| Trend filter | price ≥ 50-EMA | price ≤ 50-EMA |
| MACD histogram (12/26/9) | > 0 | < 0 |
| Hourly MACD (every 12th 5-min close) | MACD ≥ signal | MACD ≤ signal |
| Morning bias | bullish morning | bearish morning |
| Candle body | body ≥ 30% of candle range (skips dojis) | same |
| Trades today | < 2 | < 2 |

The hourly-MACD gate stops the strategy from fighting the higher-timeframe trend; it only applies
once ~420 5-minute candles of history are available.

### 1.4 Scoring (0–100)

Gates decide *whether* a trade is allowed; the score decides *whether it's good enough*. Points
depend on the volatility regime (§1.5). BUY shown; SELL mirrors it.

**Primary trigger (required — no trigger, score is 0):**

| Trigger | Points |
|---|---|
| Fresh MACD bullish crossover | +30 |
| Established uptrend: MACD > signal, histogram growing, green candle | +25 |
| *Mean-reversion regime only:* price < VWAP × 0.997, green candle, StochRSI < 30 | +30 |

**Confirmation factors:**

| Factor | Momentum / Normal regime | Mean-reversion regime |
|---|---|---|
| StochRSI (14,14,3) | +15 if 50–80; +5 if 40–50 | +15 if < 20; +10 if < 40 |
| VWAP position | +15 if price > VWAP (+10 in mixed regime) | +15 if price < VWAP |
| ADX | +10 if > 25 (+8 if > 20 in mixed) | +10 if < 20 |
| Opening-range alignment | +15 bullish, +3 neutral, **−15** if opposed | same, but no penalty when mean-reverting |
| Support/resistance (§1.6) | +20 on support, +10 near it; **−20 / −10** when running into resistance | same |

**Penalties:** StochRSI > 85 on a BUY (< 15 on a SELL) costs −20 — don't chase exhausted moves.

**Threshold:** a signal is emitted when `score ≥ 60` **and** it beats the opposite side's score.
`score ≥ 75` is tagged `STRONG`, otherwise `MODERATE`. Every signal carries its full metadata
(score, regime, ADX, StochRSI, MACD, S/R levels, gap, VWAP, stop/target multipliers) into the
trade journal so any trade can be audited after the fact.

### 1.5 Volatility regime

`VIXRegime` estimates annualised volatility from the last 30 daily closes (log returns × √252)
as a stand-in for India VIX, which isn't fetched:

| Regime | Realised vol | Preferred mode |
|---|---|---|
| `HIGH_VOL` | > 18% | `MEAN_REVERSION` |
| `NORMAL` | 13% – 18% | `MIXED` |
| `LOW_VOL` | < 13% | `MOMENTUM` |

The regime changes which scoring branch applies (table above). It is a *proxy*; replacing it with
live India VIX is a known improvement.

### 1.6 Support / resistance zones

`find_support_resistance` looks back ~1,500 candles (~2 months of 15-min data), finds swing pivots
(a bar that is the extreme of ±8 bars), clusters pivots within 0.3% of each other, and keeps only
zones **tested at least 3 times** (top 5). Distance to the nearest zone is measured in ATRs:
within 0.5 ATR is a strong signal, within 1.5 ATR a moderate one. Requiring multiple touches
keeps one-off wicks from being treated as levels.

### 1.7 Gap analysis

`GapAnalysis` tracks the overnight gap (configured for gaps between 0.3% and 1.5%) and whether it
has filled. In the current strategy the gap value is recorded in signal metadata rather than
added to the score. The premise behind it (large Nifty gaps tend to fill intraday) is a hypothesis,
not a validated edge.

---

## 2. Risk management

| Layer | Rule |
|---|---|
| **Position sizing** | `lots = (capital × risk%) / ATR`, then capped so required margin (`price × lot × MARGIN_PCT`) fits in free capital after margin already deployed. Margin-first sizing was added after live "insufficient funds" rejections. |
| **Stops (futures)** | Hard stop 1.5×ATR, trailing stop 1.0×ATR once 1.0×ATR in profit, target 3.0×ATR. Exchange-side SL-M order placed on entry; acceptance is verified and retried on rejection. |
| **Stops (options)** | Exit if premium falls `OPTIONS_STOP_PCT`% from entry or reaches `OPTIONS_TARGET_MULT`× entry; startup warns if worst-case single-trade loss would exceed the daily-loss limit. |
| **Circuit breaker** | Daily PnL ≤ −`MAX_DAILY_LOSS_PCT` of capital publishes `KILL_SWITCH`; all execution modules flatten and refuse new entries. State persisted in Redis (in-memory fallback); resets at 09:15. |
| **Position cap** | `MAX_OPEN_POSITIONS` (default 3). |
| **EOD** | Scheduler force-closes everything at 15:30 IST; EOD report at 15:35. |
| **Costs** | Brokerage, STT, exchange charges and slippage ticks are deducted in both backtests and live PnL tracking. Live PnL uses actual Kite fill prices, not candle closes. |

---

## 3. Options execution (`TRADE_OPTIONS=true`)

`OptionsDirectionalBuyer` converts the strategy's directional signal into a long-premium trade:

- **BUY signal → buy ATM Call (CE). SELL signal → buy ATM Put (PE).** Strike = spot rounded to 50.
- Long premium only — defined maximum loss, no naked short options.
- Weekly expiry handled as Tuesday (post-2025 NSE schedule); days-to-expiry feeds the premium model.
- Paper mode estimates option P&L with a simple model (delta ≈ 0.5 on spot movement minus a
  theta-decay term) instead of subscribing to option ticks. Live mode routes through
  `OptionsExecutor` and real Kite orders.

---

## 4. Other strategies in the repo

| Strategy | File | Status |
|---|---|---|
| **EMA crossover V1** — EMA 9/21 + RSI | `ema_crossover.py` | Backtest baseline only |
| **EMA crossover V2** — V1 + ADX filter + VWAP + cooldown | `ema_crossover_v2.py` | Backtest baseline only |
| **9:20 short straddle** — sell ATM CE+PE at 09:20, 25% per-leg stop, exit 15:10–15:15 | `straddle_920.py`, `straddle_validator.py` | Backtest + scheduler alerts; **not live**. The validator records real vs. modelled option premiums daily to test the pricing model. |
| **Opening range breakout** — 30-min range breakout | `orb_strategy.py` | Implemented, not wired |
| **VWAP reversion** — fade ±1.5σ from VWAP | `vwap_reversion.py` | Implemented, not wired |
| **Multi-strategy consensus** | `multi_strategy.py` | Implemented, not wired |

V1 → V2 → Adaptive is deliberate: each version is a measurable baseline for the next, and
`run_backtest.py` runs them side by side on the same data and cost model.

---

## 5. How the strategy evolved

The git history tells this story in detail; the major turns:

1. **Feb 2026 — prototype to pipeline.** EMA-crossover strategy, paper trader, event bus. Added
   options directional buying, SL-M stops and Docker + GitHub Actions deployment to AWS.
2. **Late Feb – early Mar — operating it for real.** Automatic daily token refresh, WebSocket
   reconnect handling, IST/UTC timestamp bugs found in Docker (strategy gates assume IST), warm-up
   of indicators from historical data on startup, Telegram alerts, CloudWatch/S3 logging, a
   dead-man's switch. First live run (small capital, MCX gold) exposed margin-sizing and
   end-of-day edge cases, fixed in early March.
3. **Mid Mar – Apr — correctness of money paths.** Using actual fill prices and real transaction
   costs in PnL, verifying SL-M acceptance, persisting straddle state across restarts, holiday-aware
   shutdown, automatic front-month contract resolution.
4. **May — strategy overhaul.** Options directional backtest, margin-first sizing, scoring engine
   rebuilt around MACD, StochRSI and S/R zones; focus moved from MCX to Nifty/NFO.
5. **June — simplify.** Diagnosed why signals weren't firing (over-strict acceleration test and
   time gate) and fixed it; then **removed news sentiment from scoring entirely**, moving to pure
   technical signals. The news modules remain in the repo but are no longer part of scoring.

## 6. Ideas not yet done

- Replace realised-vol proxy with live India VIX.
- Drive paper options P&L from real option ticks.
- Wire the 9:20 straddle to `OptionsExecutor` once paper results justify it.
- Walk-forward / out-of-sample validation of the score threshold and gate parameters (current
  values were tuned by hand and by debugging, which risks overfitting).
- Replay-based end-to-end tests from recorded tick files.
- Refresh the stale tests listed in the README.
