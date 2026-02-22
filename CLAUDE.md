# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Algorithmic trading bot for Indian markets (NSE/MCX) using Zerodha's Kite Connect API. Trades Nifty Futures and options during IST market hours (9:15 AM – 3:30 PM, Mon–Fri). Architecture is event-driven: all modules communicate exclusively through a central asyncio `EventBus` — no direct module-to-module calls.

## Commands

### Local Development Setup
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in API keys
python generate_token.py  # run daily before market open — Kite token expires each day
```

### Running the Bot
```bash
PAPER_TRADE=true python main.py   # paper mode (default, safe)
PAPER_TRADE=false python main.py  # live mode
```

### Docker
```bash
docker-compose up --build                              # full stack: bot + postgres + redis
docker-compose -f docker-compose.services.yml up -d   # infra only; run bot locally
```

### Backtesting
```bash
python run_backtest.py                    # all strategies, 15-min candles
python run_backtest.py v1                 # EMA v1 only
python run_backtest.py v2                 # EMA v2 only
python run_backtest.py adaptive           # adaptive strategy only
python run_backtest.py straddle           # 9:20 short straddle only
python run_backtest.py all 5             # all strategies on 5-min candles
```

### Tests
```bash
pytest tests/              # all tests
pytest tests/test_risk.py -v              # single file
pytest tests/test_risk.py::TestCircuitBreaker -v  # single class
```

No linting configuration (no `ruff.toml`, `pyproject.toml`, or `.flake8`) exists in this repo.

## Architecture

### Event Bus (core/event_bus.py)
The nervous system. All inter-module communication uses `EventBus.publish(event_type, data)` and `EventBus.subscribe(event_type, handler)`. Event types are defined in `EventType` enum. Never bypass this with direct module references.

### Data Flow
```
Kite WebSocket → KiteFeed (TICK) → CandleBuilder (CANDLE) → Strategy (SIGNAL)
                                                                    ↓
                                         PaperTrader / OrderManager (TRADE)
                                                                    ↓
                                    PositionTracker, TradeJournal, TelegramAlerts
```

Tick events also go directly to `PositionTracker` (unrealized PnL) and `HealthCheck`.

### Configuration (core/config.py)
`pydantic-settings` singleton — `from core.config import settings`. All env vars are documented there. Key operational vars: `PAPER_TRADE`, `CAPITAL`, `INSTRUMENTS` (comma-separated `"EXCHANGE:SYMBOL"` format), `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS_PCT`.

### Strategies
- **V1** (`ema_crossover.py`): EMA 9/21 crossover + RSI — backtesting only
- **V2** (`ema_crossover_v2.py`): V1 + ADX filter + VWAP + cooldown — backtesting only
- **V3 / Adaptive** (`adaptive_strategy.py`): Scoring engine (0–100, threshold 65) — **currently active in main.py**. Combines EMA crossover, RSI, VWAP, ADX, gap fill, first-hour direction, and VIX regime. No trades before 10:15 AM or after 2:30 PM. Max 2 trades/symbol/day.
- **Straddle** (`straddle_920.py`): 9:20 AM short straddle on Nifty weekly ATM options — wired only as scheduler alerts, not live execution.
- `multi_strategy.py` and `vwap_reversion.py` exist but are not wired into `main.py`.

### Risk Management
- **Position sizing** (`risk/position_sizer.py`): `lots = (capital * risk_pct) / ATR` (default 1% risk)
- **Stops** (`risk/stop_manager.py`): Hard stop at 1.5×ATR, trailing at 1.0×ATR, target at 3.0×ATR
- **Circuit breaker** (`risk/circuit_breaker.py`): Kills trading when daily loss exceeds `MAX_DAILY_LOSS_PCT` (default 2%). Redis-backed, resets at 9:15 AM. Redis is optional — falls back to in-memory.

### Scheduler (scheduler/job_runner.py)
APScheduler (`AsyncIOScheduler`, IST timezone). Key jobs: pre-market token check (8:55), market open reset (9:15), straddle alert (9:20), market close + force-exit (15:30), EOD report (15:35).

### State & Persistence
- **Redis** (optional): `PositionTracker`, `CircuitBreaker` — both fall back to in-memory if unavailable
- **PostgreSQL** (optional): `TradeJournal` — disables itself gracefully if connection fails. Trades still logged to file.
- **Logs**: `loguru` writes to `logs/bot_YYYY-MM-DD.log` (DEBUG+) and `logs/trades_YYYY-MM-DD.log` (trade-tagged only)

### Backtesting (backtesting/)
Separate `BacktestRunner` class iterates historical parquet files in `data/`. Uses same ATR-based sizing and cost model as live. `StradleBacktest` has options-specific cost logic. Run via `run_backtest.py`.

## Key Gotchas

- **Kite access token expires daily** — must run `python generate_token.py` each morning before market open.
- **Instrument format** is `"EXCHANGE:TRADINGSYMBOL"` (e.g., `"NSE:NIFTY26MARFUT"`).
- **VIX is estimated**, not fetched — `VIXRegime` uses 30-day realized volatility as a proxy for India VIX.
- **Straddle is not live** — `Straddle920Strategy` in `main.py` only triggers Telegram alerts, no actual orders.
- The `options_executor.py` module exists (`modules/execution/options_executor.py`) but is not yet wired into `main.py`.
