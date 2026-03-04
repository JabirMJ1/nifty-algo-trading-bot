"""Tests for exchange-aware market close logic.

Catches the class of bug where NSE EOD (15:30) incorrectly affects MCX positions,
or MCX-specific logic is missing. Run with: pytest tests/test_market_close.py -v
"""

import pytest
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch
import pytz

IST = pytz.timezone("Asia/Kolkata")
_NSE = "NSE:NIFTY26MARFUT"
_MCX = "MCX:GOLDPETAL26MARFUT"


# ─── helpers ─────────────────────────────────────────────────────────────────

def _ist(h, m):
    """Return a timezone-aware IST datetime for today at h:m."""
    now = datetime.now(IST)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


# ─── 1. PaperTrader.close_all_positions exchange filter ──────────────────────

class TestPaperTraderExchangeFilter:
    """NSE EOD must not close MCX positions and vice versa."""

    def _trader(self, *symbols):
        from modules.execution.paper_trader import PaperTrader
        pt = PaperTrader.__new__(PaperTrader)
        pt.positions = {s: {"entry_price": 100, "direction": "BUY", "quantity": 1}
                        for s in symbols}
        pt._candle_history = {s: [{"close": 101}] for s in symbols}
        pt._close_position = AsyncMock()
        return pt

    @pytest.mark.asyncio
    async def test_nse_eod_skips_mcx(self):
        pt = self._trader(_NSE, _MCX)
        await pt.close_all_positions("EOD_CLOSE", exchange="NSE")
        closed = [c.args[0] for c in pt._close_position.call_args_list]
        assert _NSE in closed
        assert _MCX not in closed

    @pytest.mark.asyncio
    async def test_mcx_eod_skips_nse(self):
        pt = self._trader(_NSE, _MCX)
        await pt.close_all_positions("EOD_CLOSE", exchange="MCX")
        closed = [c.args[0] for c in pt._close_position.call_args_list]
        assert _MCX in closed
        assert _NSE not in closed

    @pytest.mark.asyncio
    async def test_shutdown_closes_all(self):
        """Bot shutdown (no exchange filter) must close everything."""
        pt = self._trader(_NSE, _MCX)
        await pt.close_all_positions("SHUTDOWN")
        closed = [c.args[0] for c in pt._close_position.call_args_list]
        assert _NSE in closed
        assert _MCX in closed


# ─── 2. OrderManager.close_all_positions exchange filter ─────────────────────

class TestOrderManagerExchangeFilter:

    def _manager(self, *symbols):
        from modules.execution.order_manager import OrderManager
        om = OrderManager.__new__(OrderManager)
        om.positions = {s: {"entry_price": 100, "direction": "BUY", "quantity": 1}
                        for s in symbols}
        om._close_position_market = AsyncMock()
        return om

    @pytest.mark.asyncio
    async def test_nse_eod_skips_mcx(self):
        om = self._manager(_NSE, _MCX)
        await om.close_all_positions("EOD_CLOSE", exchange="NSE")
        closed = [c.args[0] for c in om._close_position_market.call_args_list]
        assert _NSE in closed
        assert _MCX not in closed

    @pytest.mark.asyncio
    async def test_mcx_eod_skips_nse(self):
        om = self._manager(_NSE, _MCX)
        await om.close_all_positions("EOD_CLOSE", exchange="MCX")
        closed = [c.args[0] for c in om._close_position_market.call_args_list]
        assert _MCX in closed
        assert _NSE not in closed


# ─── 3. CandleBuilder.force_close_all exchange filter ────────────────────────

class TestCandleBuilderExchangeFilter:

    def _builder(self, *symbols):
        from modules.sensory.candle_builder import CandleBuilder
        cb = CandleBuilder.__new__(CandleBuilder)
        cb._candles = {s: {"symbol": s, "open": 100, "high": 101,
                           "low": 99, "close": 100, "volume": 10}
                       for s in symbols}
        cb._emit_candle = AsyncMock()
        return cb

    @pytest.mark.asyncio
    async def test_nse_close_leaves_mcx_candle_building(self):
        cb = self._builder(_NSE, _MCX)
        await cb.force_close_all(exchange="NSE")
        emitted = [c.args[0]["symbol"] for c in cb._emit_candle.call_args_list]
        assert _NSE in emitted
        assert _MCX not in emitted
        assert _MCX in cb._candles  # MCX candle still building

    @pytest.mark.asyncio
    async def test_mcx_close_leaves_nse_candle_building(self):
        cb = self._builder(_NSE, _MCX)
        await cb.force_close_all(exchange="MCX")
        emitted = [c.args[0]["symbol"] for c in cb._emit_candle.call_args_list]
        assert _MCX in emitted
        assert _NSE not in emitted
        assert _NSE in cb._candles


# ─── 4. is_signal_allowed: exchange-aware time gates ─────────────────────────

class TestIsSignalAllowed:
    """Signal gates must respect each exchange's session, not just NSE."""

    def _patch_time(self, h, m):
        """Patch now_ist() to return a fixed IST time."""
        return patch(
            "modules.sensory.market_hours.now_ist",
            return_value=_ist(h, m),
        )

    def test_mcx_allowed_at_noon(self):
        from modules.sensory.market_hours import is_signal_allowed
        with self._patch_time(12, 0):
            assert is_signal_allowed("MCX") is True

    def test_mcx_allowed_at_1730(self):
        """MCX should still allow signals at 17:30 — after NSE closes."""
        from modules.sensory.market_hours import is_signal_allowed
        with self._patch_time(17, 30):
            assert is_signal_allowed("MCX") is True

    def test_mcx_blocked_after_2330(self):
        from modules.sensory.market_hours import is_signal_allowed
        with self._patch_time(23, 35):
            assert is_signal_allowed("MCX") is False

    def test_nse_blocked_after_1530(self):
        from modules.sensory.market_hours import is_signal_allowed
        with self._patch_time(15, 35):
            assert is_signal_allowed("NSE") is False

    def test_nse_blocked_does_not_block_mcx(self):
        """After NSE closes, MCX signals must still be allowed."""
        from modules.sensory.market_hours import is_signal_allowed
        with self._patch_time(16, 0):
            assert is_signal_allowed("NSE") is False
            assert is_signal_allowed("MCX") is True


# ─── 5. Scheduler job configuration ──────────────────────────────────────────

class TestSchedulerJobs:
    """Verify correct cron times for NSE and MCX close jobs."""

    def _job_trigger(self, runner, job_id):
        job = runner.scheduler.get_job(job_id)
        assert job is not None, f"Job '{job_id}' not found in scheduler"
        return job.trigger

    def _cron_fields(self, trigger):
        fields = {f.name: str(f) for f in trigger.fields}
        return fields

    def setup_method(self):
        from scheduler.job_runner import JobRunner
        from unittest.mock import AsyncMock
        reporter = MagicMock()
        reporter.send_eod_report = AsyncMock()
        self.runner = JobRunner(pnl_reporter=reporter)
        self.runner.setup_jobs()

    def teardown_method(self):
        # Scheduler was never started — just remove jobs cleanly
        self.runner.scheduler.remove_all_jobs()

    def test_nse_close_at_1530(self):
        trigger = self._job_trigger(self.runner, "market_close")
        fields = self._cron_fields(trigger)
        assert fields["hour"] == "15"
        assert fields["minute"] == "30"

    def test_mcx_close_at_2330(self):
        trigger = self._job_trigger(self.runner, "mcx_close")
        fields = self._cron_fields(trigger)
        assert fields["hour"] == "23"
        assert fields["minute"] == "30"

    def test_eod_report_after_mcx_close(self):
        """EOD report must fire after MCX closes, not at 15:35."""
        trigger = self._job_trigger(self.runner, "eod_report")
        fields = self._cron_fields(trigger)
        assert fields["hour"] == "23"
        assert int(fields["minute"]) >= 1  # at least 1 min after MCX close
