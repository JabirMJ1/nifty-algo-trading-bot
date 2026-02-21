# Trading Bot — Build Plan for Claude Code

## Project Overview
Build a modular, production-ready algorithmic trading bot for Indian markets 
(Nifty 50 Futures + Gold) using Zerodha Kite Connect API. 
Architecture is brain-inspired with isolated modules that communicate via 
a central event bus.

## Tech Stack
- Language: Python 3.11+
- Broker API: Zerodha Kite Connect (kiteconnect SDK)
- Database: PostgreSQL (persistent) + Redis (live state/cache)
- Scheduler: APScheduler
- Async: asyncio + websockets
- Backtesting: VectorBT
- Alerts: Telegram Bot API
- Config: pydantic-settings + .env files
- Logging: loguru
- Deployment: Docker + docker-compose

## Project Structure
trading-bot/
├── core/
│   ├── event_bus.py          # Central pub/sub message broker
│   ├── config.py             # All env vars and settings
│   └── logger.py             # Structured logging setup
├── modules/
│   ├── sensory/
│   │   ├── kite_feed.py      # WebSocket tick ingestion
│   │   ├── candle_builder.py # Tick → OHLCV candle aggregation
│   │   └── market_hours.py   # NSE market session checks
│   ├── strategy/
│   │   ├── base_strategy.py  # Abstract strategy class
│   │   ├── ema_crossover.py  # First strategy: EMA 9/21 crossover
│   │   └── signal_models.py  # Pydantic models for signals
│   ├── risk/
│   │   ├── position_sizer.py # ATR-based position sizing
│   │   ├── stop_manager.py   # Hard stop + trailing stop logic
│   │   └── circuit_breaker.py# Daily loss limit kill switch
│   ├── execution/
│   │   ├── order_manager.py  # Place/modify/cancel orders
│   │   ├── paper_trader.py   # Simulated execution engine
│   │   └── order_models.py   # Pydantic order/trade models
│   ├── state/
│   │   ├── position_tracker.py # Open positions + PnL tracking
│   │   ├── trade_journal.py    # Log all trades to PostgreSQL
│   │   └── market_memory.py    # Regime detection (trend/range)
│   └── monitor/
│       ├── telegram_alerts.py  # Send alerts via Telegram Bot
│       ├── health_check.py     # Bot heartbeat monitoring
│       └── pnl_reporter.py     # EOD PnL summary
├── backtesting/
│   ├── backtest_runner.py    # VectorBT integration
│   ├── data_fetcher.py       # Historical data download
│   └── performance.py        # Sharpe, drawdown, win rate metrics
├── scheduler/
│   └── job_runner.py         # APScheduler jobs for all timing
├── tests/
│   ├── test_strategy.py
│   ├── test_risk.py
│   └── test_execution.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── main.py                   # Entry point

## Build Order (do this sequentially)

### Step 1 — Core Infrastructure
1. Set up project structure and virtual environment
2. Build config.py using pydantic-settings (load from .env)
   - KITE_API_KEY, KITE_ACCESS_TOKEN
   - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
   - DB_URL, REDIS_URL
   - PAPER_TRADE=true (default)
   - MAX_DAILY_LOSS_PCT=2.0
   - INSTRUMENTS=["NSE:NIFTY25FEBFUT", "MCX:GOLDPETAL25FEBFUT"]
3. Build event_bus.py — simple asyncio pub/sub
   - Events: TICK, CANDLE, SIGNAL, ORDER, TRADE, ALERT, HEARTBEAT
4. Set up loguru logger with file rotation

### Step 2 — Sensory Layer
1. kite_feed.py
   - Authenticate with Kite Connect
   - Subscribe to instrument tokens via WebSocket
   - On each tick: publish TICK event to event bus
2. candle_builder.py
   - Subscribe to TICK events
   - Aggregate into 5min OHLCV candles
   - On candle close: publish CANDLE event
3. market_hours.py
   - NSE session: 9:15 AM – 3:30 PM IST
   - Pre-market: 9:00–9:15 AM
   - Helper: is_market_open(), minutes_to_close()

### Step 3 — Strategy Engine
1. base_strategy.py — abstract class with:
   - on_candle(candle) → Signal | None
   - Signal model: {instrument, direction, strength, timestamp, metadata}
2. ema_crossover.py
   - Fast EMA: 9 period, Slow EMA: 21 period on 5min candles
   - BUY signal: fast crosses above slow + RSI(14) > 50
   - SELL signal: fast crosses below slow + RSI(14) < 50
   - No signal in first 30 min of market (9:15–9:45 AM)
   - Publish SIGNAL event on trigger

### Step 4 — Risk Engine
1. position_sizer.py
   - ATR(14)-based sizing: risk 1% of capital per trade
   - Formula: position_size = (capital * risk_pct) / (ATR * multiplier)
   - Hard cap: max 3 open positions at once
2. stop_manager.py
   - Initial hard stop: 1.5x ATR from entry
   - Trailing stop: activate after 1x ATR profit, trail at 1x ATR
   - Target: 3x ATR (1:2 risk-reward minimum)
3. circuit_breaker.py
   - Track daily PnL in Redis
   - If daily loss > MAX_DAILY_LOSS_PCT: set KILL_SWITCH=true
   - When KILL_SWITCH=true: close all positions, stop taking signals
   - Reset at market open next day

### Step 5 — Execution Engine
1. paper_trader.py (build this first)
   - Simulate fills at next candle open price
   - Simulate slippage: +/- 2 ticks
   - Track virtual balance and positions
   - Publish TRADE event on fill
2. order_manager.py (real execution — build after paper trade validated)
   - Kite Connect order placement
   - Order types: MARKET, LIMIT, SL
   - Retry logic: 3 retries with exponential backoff
   - On fill confirmation: publish TRADE event

### Step 6 — State Layer
1. position_tracker.py
   - Subscribe to TRADE events
   - Maintain live positions dict in Redis
   - Calculate unrealized PnL on each TICK
2. trade_journal.py
   - Write every trade to PostgreSQL trades table
   - Schema: id, instrument, direction, entry_price, exit_price,
     quantity, pnl, entry_time, exit_time, strategy, stop_loss, target
3. market_memory.py
   - Simple regime detection: ADX > 25 = trending, < 25 = ranging
   - Expose current_regime() for strategy to consume

### Step 7 — Monitor & Alerts
1. telegram_alerts.py
   - Alert types: TRADE_OPENED, TRADE_CLOSED, STOP_HIT, 
     CIRCUIT_BREAKER, HEARTBEAT, EOD_SUMMARY
   - Format: clean, readable messages with PnL and context
2. health_check.py
   - Ping every 5 min, alert if no heartbeat in 10 min
   - Check WebSocket connection alive
   - Check Redis/DB connectivity
3. pnl_reporter.py
   - EOD at 3:35 PM: send daily summary to Telegram
   - Include: trades taken, win/loss, gross PnL, net PnL after costs

### Step 8 — Scheduler
job_runner.py with APScheduler:
- Every 1 min: health_check
- On market open (9:15): reset circuit breaker, initialize state
- On market close (3:30): trigger EOD close of all positions
- At 3:35: send EOD report
- At 8:55: run pre-market checks, validate tokens

### Step 9 — Backtesting
1. data_fetcher.py
   - Download historical OHLCV from Kite historical API
   - Store locally as parquet files
   - Instruments: Nifty continuous futures, Gold
2. backtest_runner.py
   - Load strategy, run via VectorBT
   - Include realistic costs: ₹20/order + 0.05% STT + 0.0019% exchange
3. performance.py
   - Output: Sharpe ratio, max drawdown, win rate, avg R:R, 
     profit factor, monthly PnL breakdown

### Step 10 — Docker Setup
1. Dockerfile for the bot
2. docker-compose.yml with services:
   - bot (main app)
   - postgres
   - redis
3. .env.example with all required variables

## Key Rules for Claude Code to Follow
- Every module must be independently testable
- No hardcoded values — everything via config
- Paper trade mode must be default (PAPER_TRADE=true)
- All monetary values in paise internally, display in rupees
- All times in IST, stored in UTC
- Every order placement must log before and after
- Circuit breaker check must happen before every order
- Graceful shutdown: on SIGTERM, close all positions then exit

## First Deliverable
Get Step 1–3 working end-to-end:
- Bot connects to Kite WebSocket
- Receives ticks for Nifty Futures
- Builds 5min candles
- EMA crossover strategy generates signals
- Signals logged to console
No execution yet. Validate the data pipeline first.

## Environment Variables Needed (.env)
KITE_API_KEY=
KITE_API_SECRET=
KITE_ACCESS_TOKEN=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DB_URL=postgresql://user:pass@localhost:5432/tradingbot
REDIS_URL=redis://localhost:6379
PAPER_TRADE=true
CAPITAL=100000
MAX_DAILY_LOSS_PCT=2.0
RISK_PER_TRADE_PCT=1.0
INSTRUMENTS=NSE:NIFTY25FEBFUT