"""Bot health monitoring — heartbeat, WebSocket, and connectivity checks."""

from datetime import datetime, time
from typing import Any, Dict, Optional
import aiohttp
from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger
from modules.sensory.market_hours import now_ist, is_market_open, is_mcx_open

log = get_logger("health_check")

HEARTBEAT_INTERVAL_SEC = 300  # 5 min
ALERT_THRESHOLD_SEC = 600  # 10 min — alert if no heartbeat
_RECONNECT_AFTER_DEAD_CHECKS = 3  # force-reconnect after 3 consecutive dead-tick checks (~3 min)


class HealthCheck:
    """Monitors bot health and sends alerts on issues."""

    def __init__(self, redis_client=None, kite_feed=None):
        self._redis = redis_client
        self._kite_feed = kite_feed
        self._last_heartbeat: Optional[datetime] = None
        self._last_tick_time: Optional[datetime] = None
        self._tick_count = 0
        self._last_health_alert: Optional[datetime] = None
        self._health_alert_interval_min = 10  # max 1 alert per 10 min
        self._consecutive_dead_checks: int = 0

    async def on_tick(self, tick_data: Dict[str, Any]):
        """Track last tick time for connectivity monitoring."""
        self._last_tick_time = now_ist()
        self._tick_count += 1

    async def check_health(self) -> Dict[str, Any]:
        """Run all health checks and return status."""
        now = now_ist()
        status = {
            "timestamp": now.isoformat(),
            "websocket_alive": self._check_websocket(now),
            "redis_alive": self._check_redis(),
            "last_tick_age_sec": self._get_tick_age(now),
            "tick_count": self._tick_count,
        }

        # Publish heartbeat
        self._last_heartbeat = now
        await event_bus.publish(EventType.HEARTBEAT, status)

        # Ping dead-man's switch
        await self._ping_healthchecks()

        # Check for issues
        issues = []
        if not status["websocket_alive"]:
            self._consecutive_dead_checks += 1
            issues.append(
                f"WebSocket feed appears dead (no ticks in 60s, "
                f"check #{self._consecutive_dead_checks})"
            )
            # Force-reconnect after N consecutive dead checks
            if (
                self._consecutive_dead_checks >= _RECONNECT_AFTER_DEAD_CHECKS
                and self._kite_feed
                and not getattr(self._kite_feed, "_restarting", False)
            ):
                log.warning(
                    f"WebSocket dead for {self._consecutive_dead_checks} consecutive checks "
                    f"— triggering force-reconnect"
                )
                import asyncio
                asyncio.create_task(self._kite_feed.force_reconnect())
                self._consecutive_dead_checks = 0
        else:
            self._consecutive_dead_checks = 0

        if not status["redis_alive"]:
            issues.append("Redis connection failed")

        if issues:
            log.warning(f"Health issues detected: {issues}")
            # Rate-limit: only alert once per _health_alert_interval_min minutes
            should_alert = (
                self._last_health_alert is None
                or (now - self._last_health_alert).total_seconds()
                >= self._health_alert_interval_min * 60
            )
            if should_alert:
                self._last_health_alert = now
                await event_bus.publish(
                    EventType.ALERT,
                    {
                        "type": "HEALTH_WARNING",
                        "message": "Health Issues:\n" + "\n".join(f"- {i}" for i in issues),
                    },
                )

        return status

    def _any_market_open(self) -> bool:
        """True if at least one configured instrument's exchange is currently live."""
        exchanges = {
            inst.split(":")[0]
            for inst in settings.instruments_list
            if ":" in inst
        }
        if exchanges & {"NSE", "NFO", "BSE"}:
            if is_market_open():
                return True
        if "MCX" in exchanges:
            if is_mcx_open():
                return True
        return False

    def _check_websocket(self, now) -> bool:
        """Check if WebSocket is alive (received tick in last 60s).

        Returns True (no alert) when no configured market is open — ticks are
        not expected pre-open or after close, so silence is correct.
        """
        if not self._any_market_open():
            return True
        if self._last_tick_time is None:
            return False
        return (now - self._last_tick_time).total_seconds() < 60

    def _check_redis(self) -> bool:
        """Check Redis connectivity."""
        if not self._redis:
            return True  # No Redis configured — not a failure
        try:
            return self._redis.ping()
        except Exception:
            return False

    async def _ping_healthchecks(self):
        """Ping Healthchecks.io to confirm the bot is alive."""
        url = settings.HEALTHCHECKS_URL
        if not url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(url, timeout=aiohttp.ClientTimeout(total=5))
        except Exception as e:
            log.warning(f"Healthchecks.io ping failed: {e}")

    def _get_tick_age(self, now: datetime) -> float:
        """Seconds since last tick."""
        if self._last_tick_time is None:
            return -1
        return (now - self._last_tick_time).total_seconds()

    def start(self):
        event_bus.subscribe(EventType.TICK, self.on_tick)
        log.info("Health check monitor started")
