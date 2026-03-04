"""Trading Bot — Entry Point.

Brain-inspired modular trading bot for Indian markets.
Connects all modules and starts the event-driven pipeline.
"""

import asyncio
import signal
import sys
from datetime import time as dt_time
from typing import Optional

import redis
from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger
from modules.sensory.market_hours import now_ist
from modules.sensory.kite_feed import KiteFeed
from modules.sensory.candle_builder import CandleBuilder
from modules.sensory.news_sentiment import NewsSentimentFeed
from modules.strategy.adaptive_strategy import AdaptiveStrategy
from modules.strategy.straddle_920 import Straddle920Strategy
from modules.risk.position_sizer import PositionSizer
from modules.risk.stop_manager import StopManager
from modules.risk.circuit_breaker import CircuitBreaker
from modules.execution.paper_trader import PaperTrader
from modules.execution.order_manager import OrderManager
from modules.execution.options_directional_buyer import OptionsDirectionalBuyer
from modules.state.position_tracker import PositionTracker
from modules.state.trade_journal import TradeJournal
from modules.state.market_memory import MarketMemory
from modules.monitor.telegram_alerts import TelegramAlerts
from modules.monitor.health_check import HealthCheck
from modules.monitor.pnl_reporter import PnLReporter
from scheduler.job_runner import JobRunner

log = get_logger("main")


class TradingBot:
    """Main bot class — wires all modules together."""

    def __init__(self):
        self._redis: Optional[redis.Redis] = None
        self._shutdown_event = asyncio.Event()

        # Modules
        self.kite_feed = KiteFeed()
        self.candle_builder = CandleBuilder()
        self.strategy = AdaptiveStrategy()
        self.straddle = Straddle920Strategy()
        self.position_sizer = PositionSizer()
        self.stop_manager = StopManager()
        self.circuit_breaker: Optional[CircuitBreaker] = None
        self.paper_trader: Optional[PaperTrader] = None
        self.order_manager: Optional[OrderManager] = None
        self.options_buyer: Optional[OptionsDirectionalBuyer] = None
        self.position_tracker: Optional[PositionTracker] = None
        self.news_feed: Optional[NewsSentimentFeed] = None
        self.trade_journal = TradeJournal()
        self.market_memory = MarketMemory()
        self.telegram = TelegramAlerts()
        self.health_check: Optional[HealthCheck] = None
        self.pnl_reporter: Optional[PnLReporter] = None
        self.scheduler: Optional[JobRunner] = None

    def _connect_redis(self):
        """Connect to Redis (optional — graceful fallback)."""
        try:
            self._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
            self._redis.ping()
            log.info(f"Redis connected: {settings.REDIS_URL}")
        except Exception as e:
            log.warning(f"Redis unavailable ({e}). Running without Redis.")
            self._redis = None

    def _init_modules(self):
        """Initialize all modules with dependencies."""
        self._connect_redis()

        # Risk modules
        self.circuit_breaker = CircuitBreaker(redis_client=self._redis)

        # Execution — paper trade or live
        if settings.PAPER_TRADE:
            self.paper_trader = PaperTrader(
                position_sizer=self.position_sizer,
                stop_manager=self.stop_manager,
                circuit_breaker=self.circuit_breaker,
            )
            log.info("Mode: PAPER TRADING")
        else:
            self.order_manager = OrderManager(
                kite=self.kite_feed.get_kite(),
                position_sizer=self.position_sizer,
                stop_manager=self.stop_manager,
                circuit_breaker=self.circuit_breaker,
            )
            log.info("Mode: LIVE TRADING")

        # State
        self.position_tracker = PositionTracker(redis_client=self._redis)

        # Monitoring
        self.health_check = HealthCheck(
            redis_client=self._redis, kite_feed=self.kite_feed
        )
        self.pnl_reporter = PnLReporter(
            trade_journal=self.trade_journal,
            circuit_breaker=self.circuit_breaker,
            paper_trader=self.paper_trader,
        )

        # Options directional buyer
        if settings.TRADE_OPTIONS:
            self.options_buyer = OptionsDirectionalBuyer(
                kite=self.kite_feed.get_kite() if not settings.PAPER_TRADE else None,
                circuit_breaker=self.circuit_breaker,
            )
            log.info("Options mode: DIRECTIONAL BUYING (CE/PE)")

        # News sentiment
        if settings.NEWS_SENTIMENT_ENABLED:
            self.news_feed = NewsSentimentFeed()

        # Scheduler
        self.scheduler = JobRunner(
            health_check=self.health_check,
            circuit_breaker=self.circuit_breaker,
            pnl_reporter=self.pnl_reporter,
            paper_trader=self.paper_trader,
            order_manager=self.order_manager,
            candle_builder=self.candle_builder,
            kite_feed=self.kite_feed,
            straddle_strategy=self.straddle,
        )

    def _start_modules(self):
        """Start all modules (subscribe to events)."""
        self.candle_builder.start()
        self.strategy.start()

        if self.paper_trader:
            self.paper_trader.start()
        if self.order_manager:
            self.order_manager.start()
        if self.options_buyer:
            self.options_buyer.start()

        self.position_tracker.start()
        self.trade_journal.start()
        self.market_memory.start()
        self.telegram.start()
        self.health_check.start()
        self.pnl_reporter.start()
        self.scheduler.start()

        log.info("All modules started")

    async def _graceful_shutdown(self):
        """Graceful shutdown — close positions, stop modules."""
        log.info("Initiating graceful shutdown...")

        # Close all positions
        if self.paper_trader:
            await self.paper_trader.close_all_positions("SHUTDOWN")
        if self.order_manager:
            await self.order_manager.close_all_positions("SHUTDOWN")
        if self.options_buyer:
            await self.options_buyer.close_all_positions("SHUTDOWN")

        # Send EOD report only when shutting down after market close (15:30 IST).
        # The scheduler already sends the report at 15:35 for normal EOD.
        # Skipping here avoids spurious "No trades today" messages on every deploy.
        ist_now = now_ist().time()
        if self.pnl_reporter and ist_now >= dt_time(15, 30):
            await self.pnl_reporter.send_eod_report()

        # Stop modules
        if self.news_feed:
            await self.news_feed.stop()
        if self.scheduler:
            self.scheduler.stop()
        await self.kite_feed.stop()
        await event_bus.stop()
        await self.telegram.close()

        log.info("Shutdown complete")

    def _setup_signal_handlers(self):
        """Register OS signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()

        def handle_shutdown(sig):
            log.info(f"Received signal {sig.name}. Shutting down...")
            self._shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig: handle_shutdown(s))
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f, sig=sig: handle_shutdown(sig))

    async def run(self):
        """Main run loop."""
        log.info("=" * 50)
        log.info("  TRADING BOT STARTING")
        log.info(f"  Mode: {'PAPER' if settings.PAPER_TRADE else 'LIVE'}")
        log.info(f"  Instruments: {settings.instruments_list}")
        log.info(f"  Capital: Rs. {settings.CAPITAL}")
        log.info("=" * 50)

        # Initialize
        self._init_modules()
        self._start_modules()
        self._setup_signal_handlers()

        # Start news sentiment feed (async)
        if self.news_feed:
            await self.news_feed.start()

        # Start event bus in background
        event_bus_task = asyncio.create_task(event_bus.start())

        # Start Kite WebSocket feed
        await self.kite_feed.start()

        # Warm up strategy with today's historical candles so indicators are
        # ready immediately — no need to wait 21+ candles after a restart.
        await self.candle_builder.warmup(
            self.kite_feed.kite, self.kite_feed._instrument_tokens
        )

        # Send startup alert
        await event_bus.publish(
            EventType.ALERT,
            {
                "type": "STARTUP",
                "message": (
                    f"Bot started\n"
                    f"Mode: {'PAPER' if settings.PAPER_TRADE else 'LIVE'}\n"
                    f"Instruments: {', '.join(settings.instruments_list)}\n"
                    f"Capital: Rs. {settings.CAPITAL}"
                ),
            },
        )

        # Wait for shutdown signal
        await self._shutdown_event.wait()
        await self._graceful_shutdown()
        event_bus_task.cancel()


def main():
    """Entry point."""
    bot = TradingBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Keyboard interrupt received. Exiting.")
    except Exception as e:
        log.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
