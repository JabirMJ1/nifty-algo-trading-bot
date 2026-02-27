"""APScheduler-based job runner for all timed operations."""

import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from core.event_bus import event_bus, EventType
from core.logger import get_logger

log = get_logger("job_runner")


class JobRunner:
    """Manages all scheduled jobs using APScheduler."""

    def __init__(
        self,
        health_check=None,
        circuit_breaker=None,
        pnl_reporter=None,
        paper_trader=None,
        order_manager=None,
        candle_builder=None,
        kite_feed=None,
        straddle_strategy=None,
    ):
        self.scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        self._health = health_check
        self._breaker = circuit_breaker
        self._reporter = pnl_reporter
        self._trader = paper_trader
        self._order_manager = order_manager
        self._candle_builder = candle_builder
        self._kite_feed = kite_feed
        self._straddle = straddle_strategy

    def setup_jobs(self):
        """Configure all scheduled jobs."""

        # Health check — every 1 minute
        if self._health:
            self.scheduler.add_job(
                self._health.check_health,
                IntervalTrigger(minutes=1),
                id="health_check",
                name="Health Check",
            )

        IST = "Asia/Kolkata"

        # Market open (9:15 IST) — reset circuit breaker, initialize state
        self.scheduler.add_job(
            self._on_market_open,
            CronTrigger(hour=9, minute=15, day_of_week="mon-fri", timezone=IST),
            id="market_open",
            name="Market Open Reset",
        )

        # Market close (15:30 IST) — close all positions
        self.scheduler.add_job(
            self._on_market_close,
            CronTrigger(hour=15, minute=30, day_of_week="mon-fri", timezone=IST),
            id="market_close",
            name="Market Close",
        )

        # EOD report (15:35 IST)
        if self._reporter:
            self.scheduler.add_job(
                self._reporter.send_eod_report,
                CronTrigger(hour=15, minute=35, day_of_week="mon-fri", timezone=IST),
                id="eod_report",
                name="EOD Report",
            )

        # Pre-market checks (8:55 IST) — validate tokens, run checks
        self.scheduler.add_job(
            self._pre_market_check,
            CronTrigger(hour=8, minute=55, day_of_week="mon-fri", timezone=IST),
            id="pre_market",
            name="Pre-Market Check",
        )

        # Hourly status during market hours (10:00 - 14:00 IST)
        for hour in [10, 11, 12, 13, 14]:
            self.scheduler.add_job(
                self._hourly_status,
                CronTrigger(hour=hour, minute=0, day_of_week="mon-fri", timezone=IST),
                id=f"hourly_status_{hour}",
                name=f"Hourly Status {hour}:00",
            )

        # 9:20 Straddle entry trigger
        if self._straddle:
            self.scheduler.add_job(
                self._on_straddle_entry,
                CronTrigger(hour=9, minute=20, day_of_week="mon-fri", timezone=IST),
                id="straddle_entry",
                name="9:20 Straddle Entry",
            )

            # 3:10 Straddle exit trigger
            self.scheduler.add_job(
                self._on_straddle_exit,
                CronTrigger(hour=15, minute=10, day_of_week="mon-fri", timezone=IST),
                id="straddle_exit",
                name="3:10 Straddle Exit",
            )

        log.info("All scheduled jobs configured")

    async def _on_market_open(self):
        """Actions at market open."""
        log.info("=== MARKET OPEN ===")

        # Reset circuit breaker
        if self._breaker:
            await self._breaker.reset()

        await event_bus.publish(
            EventType.ALERT,
            {"type": "MARKET_OPEN", "message": "Market is now open. Trading active."},
        )

    async def _on_market_close(self):
        """Actions at market close — close all positions."""
        log.info("=== MARKET CLOSE ===")

        # Close all open positions
        if self._trader:
            await self._trader.close_all_positions("EOD_CLOSE")
        if self._order_manager:
            await self._order_manager.close_all_positions("EOD_CLOSE")

        # Force close building candles
        if self._candle_builder:
            await self._candle_builder.force_close_all()

        await event_bus.publish(
            EventType.ALERT,
            {"type": "MARKET_CLOSE", "message": "Market closed. All positions squared off."},
        )

    async def _pre_market_check(self):
        """Pre-market checks before trading starts."""
        log.info("=== PRE-MARKET CHECK ===")
        issues = []

        # Check Kite token validity
        if self._kite_feed:
            try:
                kite = self._kite_feed.get_kite()
                profile = kite.profile()
                log.info(f"Kite token valid. User: {profile.get('user_name')}")
            except Exception as e:
                issues.append(f"Kite token invalid: {e}")

        # Check Redis
        if self._health and self._health._redis:
            if not self._health._check_redis():
                issues.append("Redis connection failed")

        if issues:
            msg = "Pre-Market Issues:\n" + "\n".join(f"- {i}" for i in issues)
            log.warning(msg)
            await event_bus.publish(
                EventType.ALERT,
                {"type": "PRE_MARKET_WARNING", "message": msg},
            )
        else:
            log.info("Pre-market checks passed")
            await event_bus.publish(
                EventType.ALERT,
                {"type": "PRE_MARKET_OK", "message": "All pre-market checks passed. Ready to trade."},
            )

    async def _hourly_status(self):
        """Send hourly status update during market hours."""
        from modules.sensory.market_hours import now_ist

        pnl = self._breaker.get_daily_pnl() if self._breaker else 0.0
        trades = self._breaker._trades_today if self._breaker else 0
        mode = "PAPER" if self._trader else "LIVE"
        time_str = now_ist().strftime("%H:%M IST")

        msg = (
            f"<b>Status {time_str}</b>\n"
            f"Mode: {mode}\n"
            f"Daily PnL: {pnl:+.2f}\n"
            f"Trades today: {trades}"
        )
        if self._trader:
            msg += f"\nBalance: {self._trader.balance:.2f}"

        await event_bus.publish(
            EventType.ALERT, {"type": "HOURLY_STATUS", "message": msg}
        )

    async def _on_straddle_entry(self):
        """Trigger 9:20 straddle entry."""
        log.info("=== 9:20 STRADDLE ENTRY TRIGGER ===")
        await event_bus.publish(
            EventType.ALERT,
            {"type": "STRADDLE_ENTRY", "message": "9:20 AM — Straddle entry triggered."},
        )

    async def _on_straddle_exit(self):
        """Trigger 3:10 straddle exit."""
        log.info("=== 3:10 STRADDLE EXIT TRIGGER ===")
        await event_bus.publish(
            EventType.ALERT,
            {"type": "STRADDLE_EXIT", "message": "3:10 PM — Closing straddle positions."},
        )

    def start(self):
        """Start the scheduler."""
        self.setup_jobs()
        self.scheduler.start()
        log.info("Scheduler started")

    def stop(self):
        """Stop the scheduler."""
        self.scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")
