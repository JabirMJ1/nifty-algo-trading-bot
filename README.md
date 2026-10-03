# Nifty Algo Trading Bot

An event-driven algorithmic trading bot for Indian markets (NSE/NFO) built on Zerodha's
[Kite Connect](https://kite.trade) API. It ingests live ticks, builds candles, scores trade setups
with a multi-factor technical strategy, sizes positions by volatility, and executes either as
Nifty futures or as directional weekly options (buy CE/PE) — in paper or live mode.

> **Disclaimer:** This is a personal engineering project, not financial advice. Trading derivatives
> involves substantial risk of loss. The bot defaults to **paper trading**. No performance claims
> are made in this repository — see [Status & honest limitations](#status--honest-limitations).

## Highlights

- **Fully event-driven.** Every module talks only through a central asyncio `EventBus`
  (`TICK → CANDLE → SIGNAL → TRADE`); there are no direct module-to-module calls, so each piece
  (feed, strategy, risk, execution, alerts) can be tested and replaced in isolation.
- **Rule-based scoring strategy** (0–100) built from MACD, StochRSI, VWAP, ADX, multi-touch
  support/resistance zones, opening-range bias and a volatility regime — with hard gates that
  filter out low-quality days. Details in [`docs/STRATEGY.md`](docs/STRATEGY.md).
- **Defence-in-depth risk management:** ATR-based sizing capped by available margin, hard +
  trailing stops, exchange-side SL-M orders with acceptance verification, a daily-loss circuit
  breaker, a position cap and a forced end-of-day square-off.
- **Built to run unattended:** automatic daily Kite token refresh (TOTP), WebSocket auto-reconnect,
  indicator warm-up from historical data on startup, holiday-aware auto-shutdown, a dead-man's
  switch (Healthchecks.io) and Telegram alerts.
- **Graceful degradation:** Redis and PostgreSQL are optional — state falls back to memory and the
  trade journal falls back to log files.
- **Cloud deployment with CI/CD:** Docker, GitHub Actions deploys, CloudWatch logging and S3
  archival of logs/data (see [`deploy/`](deploy) and [`AWS.md`](AWS.md)).
- **Backtesting harness** with the same ATR sizing and cost model (brokerage, STT, exchange
  charges, slippage) as live, reporting Sharpe, drawdown, win rate and profit factor.

## Architecture

```
Kite WebSocket ─► KiteFeed ──TICK──► CandleBuilder ──CANDLE──► AdaptiveStrategy
                      │                                              │
                      │                                           SIGNAL
                      ▼                                              ▼
            PositionTracker (live PnL)             PositionSizer ─► StopManager
            HealthCheck                                              │
                                                      PaperTrader / OrderManager /
                                                      OptionsDirectionalBuyer
                                                                     │
                                                                   TRADE
                                          ┌──────────────┬───────────┴───────────┐
                                    PositionTracker  TradeJournal            TelegramAlerts
                                     (Redis*)        (PostgreSQL*)

     CircuitBreaker ──KILL_SWITCH──► all execution modules      * optional
     JobRunner (APScheduler, IST) ── market open reset · pre-market token check ·
                                     force-exit 15:30 · EOD report 15:35 · hourly status
```

| Package | Responsibility |
|---|---|
| `core/` | `EventBus`, pydantic-settings config (validated at startup), logging |
| `modules/sensory/` | Kite WebSocket feed, tick→candle aggregation, market hours/holidays, auto token refresh |
| `modules/strategy/` | Strategies, signal models, gap analysis, straddle logic and validator |
| `modules/risk/` | Position sizing, stop management, circuit breaker |
| `modules/execution/` | Paper trader, live order manager, options buyer/executor |
| `modules/state/` | Position tracker, trade journal, market memory, volatility regime |
| `modules/monitor/` | Telegram alerts, health checks, PnL reports |
| `scheduler/` | APScheduler jobs on IST (market open, close, EOD, pre-market checks) |
| `backtesting/` | Backtest runner, performance metrics, options & straddle backtests, data fetcher |
| `deploy/`, `.github/workflows/` | Docker, AWS bootstrap, CI/CD |

## Strategy at a glance

The active strategy is the **Adaptive scoring strategy** (`modules/strategy/adaptive_strategy.py`).
In short: trade only on confirmed trend days after 10:30 IST; require a MACD trigger that agrees
with the 50-EMA, the hourly MACD and the morning's direction; then add or subtract points for
StochRSI, VWAP, ADX, opening-range alignment and proximity to well-tested support/resistance. A
signal fires at a score of **60+** (≥75 is "strong"), max two trades per symbol per day.

Full write-up, scoring tables and design rationale: **[`docs/STRATEGY.md`](docs/STRATEGY.md)**.

## Quick start

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # add -r requirements-backtest.txt for backtests
cp .env.example .env                       # fill in your own keys; .env is gitignored
python generate_token.py                   # Kite access token expires daily

PAPER_TRADE=true  python main.py           # paper mode (default, safe)
PAPER_TRADE=false python main.py           # live — only after you've validated everything
```

Docker (bot + PostgreSQL + Redis):

```bash
docker-compose up --build
docker-compose -f docker-compose.services.yml up -d   # infra only, run the bot locally
```

Backtesting (needs historical data fetched through your Kite session):

```bash
python run_backtest.py                 # all strategies, 15-min candles
python run_backtest.py adaptive        # adaptive strategy only
python run_backtest.py all 5           # all strategies, 5-min candles
```

### Key configuration

All settings live in `core/config.py` and load from `.env`. A bad config fails fast at startup
(invalid exchange/symbol format, or missing Kite credentials in live mode).

| Variable | Default | Meaning |
|---|---|---|
| `PAPER_TRADE` | `true` | Simulated fills vs. real orders |
| `INSTRUMENTS` | — | Comma-separated `EXCHANGE:SYMBOL`, e.g. `NFO:NIFTY` |
| `CAPITAL` | `100000` | Starting capital (₹) |
| `RISK_PER_TRADE_PCT` | `1.0` | Risk budget per trade for ATR sizing |
| `MAX_DAILY_LOSS_PCT` | `2.0` | Circuit-breaker threshold |
| `MAX_OPEN_POSITIONS` | `3` | Concurrent position cap |
| `STOP_ATR_MULTIPLIER` / `TRAIL_ATR_MULTIPLIER` / `TARGET_ATR_MULTIPLIER` | `1.5` / `1.0` / `3.0` | ATR-based stop, trail, target |
| `TRADE_OPTIONS` | `false` | Buy CE/PE instead of trading futures |
| `OPTIONS_STOP_PCT` / `OPTIONS_TARGET_MULT` | `45` / `2.5` | Premium stop (% drop) and target (× entry) |

## Testing

```bash
pytest tests/                         # full suite
pytest tests/test_risk.py -v          # a single file
```

The suite covers indicators, strategy scoring, risk modules, paper/live execution paths, market
close handling and the options buyer.

## Status & honest limitations

Being upfront about where this project stands:

- **No published backtest or live results.** I'm deliberately not quoting P&L or win rates here:
  the backtest harness exists, but results depend heavily on the data window and the cost/slippage
  model, and I don't want to present unvalidated numbers as evidence of an edge.
- **Test suite is partly stale.** Currently **264 of 301 tests pass**. The 37 failures come from
  recent refactors (e.g. removing news sentiment from scoring changed `_score_buy`/`_score_sell`
  signatures, and position-sizing now caps by margin) that the older tests haven't been updated for.
  Bringing them back to green is next on the list.
- **Volatility regime is a proxy.** India VIX is not fetched; the regime uses 30-day realized
  volatility as an estimate.
- **Options P&L in paper mode is modelled** (delta ≈ 0.5 plus a theta-decay estimate) rather than
  driven by real option ticks.
- **The 9:20 short straddle is not live.** The strategy, validator and backtest exist, but it only
  drives scheduler alerts — `OptionsExecutor` is not yet wired to it.
- `multi_strategy.py`, `orb_strategy.py` and `vwap_reversion.py` are implemented but not wired
  into `main.py`; the EMA V1/V2 strategies are kept for backtest comparison.
- Backlog and known gaps are tracked in [`TODO.md`](TODO.md).

## How this was built

The commit history is intact and shows the real development path — from an EMA-crossover
prototype, to a scoring engine, to live deployment on AWS, to simplifying the strategy to pure
technical signals by removing news sentiment. A short narrative of the major decisions is in
[`docs/STRATEGY.md`](docs/STRATEGY.md#how-the-strategy-evolved). AI-assisted tooling (Claude Code)
was used during development; see [`CLAUDE.md`](CLAUDE.md).

## Security

Secrets are loaded exclusively from environment variables / `.env` (gitignored) and, in the
original deployment, AWS Secrets Manager. No credentials are committed. Infrastructure identifiers
in the docs (account ID, IPs, instance IDs) are placeholders.
