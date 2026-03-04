# Trading Bot — Improvement Backlog

## High Impact (Reliability & Safety)

- [x] **Fix SQLAlchemy session leak** — `_record_entry` / `_record_exit` / `get_trades_today` in
  `modules/state/trade_journal.py` call `session.close()` manually. An exception before that line
  leaks the session and will eventually exhaust the connection pool. Switch to `with self.SessionLocal() as session:`.

- [x] **Validate required config at startup** — `KITE_API_KEY`, `KITE_API_SECRET`, and
  `KITE_ACCESS_TOKEN` default to `""`. The bot starts silently and fails at runtime. Add
  `field_validator`s in `core/config.py` that raise immediately when live mode is active.

- [x] **Pin `anthropic` in requirements.txt** — every other dep is pinned; `anthropic>=0.40.0` is
  open-ended. Pin to the current version to prevent silent breakage on major API changes.

- [x] **Test AdaptiveStrategy signal generation** — 31 tests added in `tests/test_adaptive_strategy.py`
  covering indicators, `_score_buy` / `_score_sell` scoring, and `on_candle` signal emission.
  Also fixed 3 pre-existing test bugs in `tests/test_improvements.py` (wrong `now` arg passed to `_check_websocket`).

## Medium Impact (Operational)

- [x] **Dead-man's-switch / Healthchecks.io** — `check_health()` now pings `HEALTHCHECKS_URL`
  every 5 min. If the bot goes silent, Healthchecks.io sends an alert. URL stored in `.env`.

- [ ] **Block deploys during market hours** — every push to `main` triggers `docker compose down` then
  `up`, causing missed ticks. Add a step in `.github/workflows/deploy.yml` that fails the job
  between 09:00–15:35 IST Mon–Fri (allow override with an env flag for emergencies).

- [x] **Split backtesting dependencies** — `pandas`, `numpy`, `vectorbt`, `pyarrow` moved to
  `requirements-backtest.txt`. Run `pip install -r requirements.txt -r requirements-backtest.txt`
  locally for backtesting.

- [x] **Remove unused dependencies** — removed `python-telegram-bot` (Telegram uses raw `aiohttp`),
  `asyncpg` (TradeJournal uses sync psycopg2), and `websockets` (Kite has its own WS client).

## Lower Priority (Features & Polish)

- [ ] **Wire straddle for live execution** — `Straddle920Strategy` and `OptionsExecutor` both exist
  but aren't connected. Once paper results look good, it's mostly plumbing to enable live orders.

- [ ] **Prometheus / Grafana metrics** — expose a `/metrics` endpoint (PnL, trade count, strategy
  scores, tick latency). Enables dashboards and degradation alerts beyond what Telegram provides.

- [ ] **Replay-based integration tests** — record a day's ticks to a fixture file and replay them
  through the event bus. Gives a fast, reproducible end-to-end regression test without a live
  market connection.
