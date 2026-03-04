"""Tests for improvements made 2026-03-01.

Covers:
- Adaptive strategy: score logging + MCX trading hours (23:00 cutoff vs NSE 14:30)
- HealthCheck: suppress WebSocket dead warning outside market hours
- NewsSentimentFeed: AlphaVantage retry on empty feed
- JobRunner: all CronTriggers use Asia/Kolkata timezone
"""

import asyncio
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_candle(symbol, hour, minute, close=113.0):
    """Candle with a clear body (body/range = 3/7 ≈ 0.43 → passes doji filter)."""
    return {
        "symbol": symbol,
        "timestamp": datetime(2026, 3, 2, hour, minute, 0),
        "open": close - 3.0,
        "high": close + 2.0,
        "low": close - 5.0,
        "close": close,
        "volume": 1000,
    }


def _build_strategy(symbol, n=25):
    """Return an AdaptiveStrategy pre-loaded with n candles of uptrend history."""
    from modules.strategy.adaptive_strategy import AdaptiveStrategy

    strategy = AdaptiveStrategy()

    history = []
    for i in range(n):
        close = 100.0 + i * 0.5
        history.append({
            "symbol": symbol,
            "timestamp": datetime(2026, 3, 2, 9, 30 + i),
            "open": close - 3.0,
            "high": close + 2.0,
            "low": close - 5.0,
            "close": close,
            "volume": 1000,
        })

    strategy._candle_history = {symbol: history}
    strategy._today_candles = {symbol: list(history)}
    strategy._current_day = {symbol: "2026-03-02"}
    strategy._first_hour_set = {symbol: True}
    strategy._first_hour_direction = {symbol: "BULLISH"}
    strategy._traded_today = {symbol: 0}
    # Simulate previous EMAs such that fast was below slow (crossover pending)
    strategy._prev_fast_ema = {symbol: 99.5}
    strategy._prev_slow_ema = {symbol: 100.0}
    return strategy


# ─── 1. Score logging ─────────────────────────────────────────────────────────

class TestScoreLogging:
    """Score debug line must not crash and must log both buy and sell scores."""

    def test_score_log_emitted_on_valid_candle(self):
        """SCORE debug line is emitted when the strategy reaches the scoring phase."""
        symbol = "MCX:GOLDPETAL26MARFUT"
        strategy = _build_strategy(symbol)
        candle = _make_candle(symbol, 10, 0, close=113.0)

        # loguru doesn't integrate with pytest caplog — patch the module-level logger
        import modules.strategy.adaptive_strategy as mod
        with patch.object(mod.log, "debug") as mock_debug:
            strategy.on_candle(candle)

        calls = [str(c.args[0]) for c in mock_debug.call_args_list]
        score_calls = [c for c in calls if "SCORE" in c]
        assert score_calls, f"Expected a SCORE debug call. Got: {calls}"
        assert symbol in score_calls[0]
        assert "buy=" in score_calls[0]
        assert "sell=" in score_calls[0]

    def test_score_log_not_emitted_before_930(self, caplog):
        """No SCORE log should appear for pre-9:30 candles (strategy returns early)."""
        import logging
        symbol = "NSE:NIFTY26MARFUT"
        strategy = _build_strategy(symbol)
        candle = _make_candle(symbol, 9, 20)  # before 9:30

        with caplog.at_level(logging.DEBUG, logger="adaptive_strategy"):
            result = strategy.on_candle(candle)

        assert result is None
        score_lines = [r.message for r in caplog.records if "SCORE" in r.message]
        assert not score_lines


# ─── 2. MCX vs NSE trading hour cutoffs ──────────────────────────────────────

class TestMCXTradingHours:
    """MCX cutoff = 23:00 IST; NSE cutoff = 14:30 IST."""

    # ── NSE cutoff ──────────────────────────────────────────────────────────

    def test_nse_blocked_at_1430(self):
        symbol = "NSE:NIFTY26MARFUT"
        strategy = _build_strategy(symbol)
        assert strategy.on_candle(_make_candle(symbol, 14, 30)) is None

    def test_nse_blocked_after_1430(self):
        symbol = "NSE:NIFTY26MARFUT"
        strategy = _build_strategy(symbol)
        assert strategy.on_candle(_make_candle(symbol, 15, 0)) is None

    def test_nse_blocked_late_evening(self):
        symbol = "NSE:NIFTY26MARFUT"
        strategy = _build_strategy(symbol)
        assert strategy.on_candle(_make_candle(symbol, 20, 0)) is None

    # ── MCX cutoff ──────────────────────────────────────────────────────────

    def test_mcx_blocked_at_2300(self):
        symbol = "MCX:GOLDPETAL26MARFUT"
        strategy = _build_strategy(symbol)
        assert strategy.on_candle(_make_candle(symbol, 23, 0)) is None

    def test_mcx_blocked_after_2300(self):
        symbol = "MCX:GOLDPETAL26MARFUT"
        strategy = _build_strategy(symbol)
        assert strategy.on_candle(_make_candle(symbol, 23, 1)) is None

    def test_mcx_not_blocked_at_1500(self):
        """MCX at 15:00 IST (after NSE close) should NOT be blocked by cutoff."""
        symbol = "MCX:GOLDPETAL26MARFUT"
        strategy = _build_strategy(symbol)
        candle = _make_candle(symbol, 15, 0, close=113.0)

        with patch.object(strategy, "_score_buy", return_value=70), \
             patch.object(strategy, "_score_sell", return_value=0):
            result = strategy.on_candle(candle)

        assert result is not None, (
            "MCX at 15:00 IST should pass the cutoff check (cutoff is 23:00)"
        )

    def test_mcx_not_blocked_at_2000(self):
        """MCX at 20:00 IST (evening session) should NOT be blocked."""
        symbol = "MCX:GOLDPETAL26MARFUT"
        strategy = _build_strategy(symbol)
        candle = _make_candle(symbol, 20, 0, close=113.0)

        with patch.object(strategy, "_score_buy", return_value=70), \
             patch.object(strategy, "_score_sell", return_value=0):
            result = strategy.on_candle(candle)

        assert result is not None

    def test_mcx_not_blocked_at_2259(self):
        """MCX at 22:59 IST (1 min before cutoff) should NOT be blocked."""
        symbol = "MCX:GOLDPETAL26MARFUT"
        strategy = _build_strategy(symbol)
        candle = _make_candle(symbol, 22, 59, close=113.0)

        with patch.object(strategy, "_score_buy", return_value=70), \
             patch.object(strategy, "_score_sell", return_value=0):
            result = strategy.on_candle(candle)

        assert result is not None

    def test_nse_blocked_where_mcx_is_not(self):
        """Key difference: at 15:00 IST, NSE is blocked, MCX is not."""
        mcx = _build_strategy("MCX:GOLDPETAL26MARFUT")
        nse = _build_strategy("NSE:NIFTY26MARFUT")
        candle_time = (15, 0)

        with patch.object(mcx, "_score_buy", return_value=70), \
             patch.object(mcx, "_score_sell", return_value=0), \
             patch.object(nse, "_score_buy", return_value=70), \
             patch.object(nse, "_score_sell", return_value=0):
            mcx_result = mcx.on_candle(_make_candle("MCX:GOLDPETAL26MARFUT", *candle_time))
            nse_result = nse.on_candle(_make_candle("NSE:NIFTY26MARFUT", *candle_time))

        assert mcx_result is not None, "MCX should trade at 15:00 IST"
        assert nse_result is None,     "NSE should be blocked at 15:00 IST"

    def test_symbol_without_exchange_prefix_defaults_to_nse_cutoff(self):
        """Symbol with no exchange prefix defaults to NSE 14:30 cutoff."""
        strategy = _build_strategy("NIFTY26MARFUT")
        strategy._candle_history["NIFTY26MARFUT"] = strategy._candle_history.pop(
            next(iter(strategy._candle_history))
        )
        strategy._today_candles["NIFTY26MARFUT"] = strategy._today_candles.pop(
            next(iter(strategy._today_candles))
        )
        strategy._current_day["NIFTY26MARFUT"] = "2026-03-02"
        strategy._first_hour_set["NIFTY26MARFUT"] = True
        strategy._first_hour_direction["NIFTY26MARFUT"] = "BULLISH"
        strategy._traded_today["NIFTY26MARFUT"] = 0
        strategy._prev_fast_ema["NIFTY26MARFUT"] = 99.5
        strategy._prev_slow_ema["NIFTY26MARFUT"] = 100.0

        candle = _make_candle("NIFTY26MARFUT", 15, 0)
        assert strategy.on_candle(candle) is None  # defaults to NSE 14:30 cutoff


# ─── 3. Health check market hours ─────────────────────────────────────────────

class TestHealthCheckMarketHours:
    """WebSocket dead alert should be silent outside 9:00–23:30 IST."""

    import pytz
    _IST = pytz.timezone("Asia/Kolkata")

    def _ist(self, hour, minute):
        import pytz
        tz = pytz.timezone("Asia/Kolkata")
        return datetime(2026, 3, 2, hour, minute, 0, tzinfo=tz)

    def test_no_alarm_before_market_open(self):
        """Pre-market (8:00 AM IST): no alarm even with no ticks."""
        from modules.monitor.health_check import HealthCheck
        check = HealthCheck()
        result = check._check_websocket(self._ist(8, 0))
        assert result is True

    def test_no_alarm_early_morning(self):
        """Very early morning (6:00 AM IST): no alarm."""
        from modules.monitor.health_check import HealthCheck
        check = HealthCheck()
        result = check._check_websocket(self._ist(6, 0))
        assert result is True

    def test_alarm_during_nse_hours(self):
        """During market hours (10:00 AM IST) with no ticks → should alarm."""
        from modules.monitor.health_check import HealthCheck
        check = HealthCheck()  # _last_tick_time is None
        result = check._check_websocket(self._ist(10, 0))
        assert result is False

    def test_alarm_during_mcx_evening(self):
        """During MCX evening session (7:00 PM IST) with no ticks → should alarm."""
        from modules.monitor.health_check import HealthCheck
        check = HealthCheck()
        result = check._check_websocket(self._ist(19, 0))
        assert result is False

    def test_no_alarm_after_mcx_close(self):
        """After all markets close (11:35 PM IST): no alarm."""
        from modules.monitor.health_check import HealthCheck
        check = HealthCheck()
        result = check._check_websocket(self._ist(23, 35))
        assert result is True

    def test_boundary_exactly_9am(self):
        """Exactly at 9:00 AM IST — market hours begin, alarms active."""
        from modules.monitor.health_check import HealthCheck
        check = HealthCheck()
        result = check._check_websocket(self._ist(9, 0))
        assert result is False  # Market hours started, no ticks → alarm

    def test_tick_within_60s_returns_healthy(self):
        """During market hours, a recent tick should return True (healthy)."""
        from modules.monitor.health_check import HealthCheck
        check = HealthCheck()
        now = self._ist(11, 0)
        check._last_tick_time = now  # Just received a tick
        result = check._check_websocket(now)
        assert result is True


# ─── 4. AlphaVantage retry ────────────────────────────────────────────────────

class TestAlphaVantageRetry:
    """AV should retry once on empty feed, then fall back to RSS-only."""

    def _make_mock_session(self, responses):
        """Build an aiohttp.ClientSession mock that returns responses in order."""
        counter = [0]

        class MockResponse:
            def __init__(self):
                self._data = responses[min(counter[0], len(responses) - 1)]
                counter[0] += 1
                self.status = 200

            async def json(self):
                return self._data

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

        class MockSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def get(self, *args, **kwargs):
                return MockResponse()

        return MockSession, counter

    @pytest.mark.asyncio
    async def test_returns_headlines_on_first_attempt(self):
        """No retry needed — first response has headlines."""
        from modules.sensory.news_sentiment import NewsSentimentFeed

        feed = NewsSentimentFeed()
        MockSession, counter = self._make_mock_session([
            {"feed": [{"title": "Gold rallies on safe-haven demand"}]},
        ])

        with patch("modules.sensory.news_sentiment.settings") as s, \
             patch("aiohttp.ClientSession", MockSession), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            s.ALPHAVANTAGE_API_KEY = "test_key"
            headlines = await feed._fetch_alphavantage_headlines()

        assert headlines == ["Gold rallies on safe-haven demand"]
        assert counter[0] == 1        # Only one HTTP call
        mock_sleep.assert_not_called()  # No retry sleep

    @pytest.mark.asyncio
    async def test_retries_once_on_empty_feed(self):
        """First response empty → sleep 5s → second response has data."""
        from modules.sensory.news_sentiment import NewsSentimentFeed

        feed = NewsSentimentFeed()
        MockSession, counter = self._make_mock_session([
            {"feed": []},
            {"feed": [{"title": "Nifty recovers from lows"}]},
        ])

        with patch("modules.sensory.news_sentiment.settings") as s, \
             patch("aiohttp.ClientSession", MockSession), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            s.ALPHAVANTAGE_API_KEY = "test_key"
            headlines = await feed._fetch_alphavantage_headlines()

        assert headlines == ["Nifty recovers from lows"]
        assert counter[0] == 2          # Two HTTP calls
        mock_sleep.assert_called_once_with(5)  # Slept 5s between attempts

    @pytest.mark.asyncio
    async def test_returns_empty_after_two_empty_feeds(self):
        """Both attempts return empty → return [] without crashing."""
        from modules.sensory.news_sentiment import NewsSentimentFeed

        feed = NewsSentimentFeed()
        MockSession, counter = self._make_mock_session([
            {"feed": []},
            {"feed": []},
        ])

        with patch("modules.sensory.news_sentiment.settings") as s, \
             patch("aiohttp.ClientSession", MockSession), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            s.ALPHAVANTAGE_API_KEY = "test_key"
            headlines = await feed._fetch_alphavantage_headlines()

        assert headlines == []
        assert counter[0] == 2  # Still tried twice

    @pytest.mark.asyncio
    async def test_returns_empty_immediately_on_non_200(self):
        """Non-200 response → return [] immediately, no retry."""
        from modules.sensory.news_sentiment import NewsSentimentFeed

        counter = [0]

        class MockResponse:
            def __init__(self):
                counter[0] += 1
                self.status = 429  # rate limited

            async def json(self):
                return {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

        class MockSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def get(self, *args, **kwargs):
                return MockResponse()

        feed = NewsSentimentFeed()
        with patch("modules.sensory.news_sentiment.settings") as s, \
             patch("aiohttp.ClientSession", MockSession), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            s.ALPHAVANTAGE_API_KEY = "test_key"
            headlines = await feed._fetch_alphavantage_headlines()

        assert headlines == []
        assert counter[0] == 1   # Only one call — no retry on non-200
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_api_key(self):
        """No API key → skip entirely, return []."""
        from modules.sensory.news_sentiment import NewsSentimentFeed

        feed = NewsSentimentFeed()
        with patch("modules.sensory.news_sentiment.settings") as s:
            s.ALPHAVANTAGE_API_KEY = ""
            headlines = await feed._fetch_alphavantage_headlines()

        assert headlines == []


# ─── 5. JobRunner CronTrigger timezone ───────────────────────────────────────

class TestJobRunnerTimezone:
    """Every CronTrigger in the scheduler must use Asia/Kolkata."""

    def test_all_cron_triggers_use_ist(self):
        from apscheduler.triggers.cron import CronTrigger
        from scheduler.job_runner import JobRunner

        runner = JobRunner(
            health_check=MagicMock(),
            pnl_reporter=MagicMock(),
            straddle_strategy=MagicMock(),
        )
        runner.setup_jobs()

        non_ist = []
        ist_count = 0

        for job in runner.scheduler.get_jobs():
            trigger = job.trigger
            if not isinstance(trigger, CronTrigger):
                continue
            tz_str = str(trigger.timezone)
            if "Kolkata" in tz_str:
                ist_count += 1
            else:
                non_ist.append(f"{job.id} (tz={tz_str})")

        assert not non_ist, f"CronTriggers without IST timezone: {non_ist}"
        assert ist_count >= 8, f"Expected at least 8 IST CronTriggers, got {ist_count}"

    def test_expected_job_ids_present(self):
        """Verify all expected job IDs are registered."""
        from scheduler.job_runner import JobRunner

        runner = JobRunner(
            health_check=MagicMock(),
            pnl_reporter=MagicMock(),
            straddle_strategy=MagicMock(),
        )
        runner.setup_jobs()

        job_ids = {job.id for job in runner.scheduler.get_jobs()}
        required = {
            "market_open", "market_close", "eod_report",
            "pre_market", "straddle_entry", "straddle_exit",
            "hourly_status_10", "hourly_status_11",
            "hourly_status_12", "hourly_status_13", "hourly_status_14",
        }
        missing = required - job_ids
        assert not missing, f"Missing job IDs: {missing}"
