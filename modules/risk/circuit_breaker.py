"""Circuit breaker — daily loss limit kill switch."""

import asyncio
import json
from typing import Optional
from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger

log = get_logger("circuit_breaker")


class CircuitBreaker:
    """Monitors daily PnL and triggers kill switch if loss limit exceeded.

    Uses Redis for state persistence. Falls back to in-memory if Redis unavailable.
    """

    REDIS_KEY = "circuit_breaker:state"

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._kill_switch = False
        self._daily_pnl: float = 0.0
        self._max_loss = settings.CAPITAL * (settings.MAX_DAILY_LOSS_PCT / 100)
        self._trades_today: int = 0

    async def _save_state(self):
        """Persist state to Redis (non-blocking)."""
        if self._redis:
            state = json.dumps({
                "kill_switch": self._kill_switch,
                "daily_pnl": self._daily_pnl,
                "trades_today": self._trades_today,
            })
            await asyncio.to_thread(
                self._redis.set, self.REDIS_KEY, state, ex=86400
            )

    async def _load_state(self):
        """Load state from Redis (non-blocking)."""
        if self._redis:
            data = await asyncio.to_thread(self._redis.get, self.REDIS_KEY)
            if data:
                state = json.loads(data)
                self._kill_switch = state["kill_switch"]
                self._daily_pnl = state["daily_pnl"]
                self._trades_today = state["trades_today"]

    async def update_pnl(self, pnl: float):
        """Update daily PnL with a new trade result."""
        self._daily_pnl += pnl
        self._trades_today += 1

        log.info(
            f"Daily PnL: {self._daily_pnl:.2f} | "
            f"Loss limit: -{self._max_loss:.2f} | "
            f"Trades: {self._trades_today}"
        )

        if self._daily_pnl <= -self._max_loss and not self._kill_switch:
            self._kill_switch = True
            log.warning(
                f"CIRCUIT BREAKER TRIGGERED! Daily loss {self._daily_pnl:.2f} "
                f"exceeds limit -{self._max_loss:.2f}"
            )
            await event_bus.publish(
                EventType.KILL_SWITCH,
                {
                    "reason": "daily_loss_limit",
                    "daily_pnl": self._daily_pnl,
                    "max_loss": self._max_loss,
                },
            )
            await event_bus.publish(
                EventType.ALERT,
                {
                    "type": "CIRCUIT_BREAKER",
                    "message": (
                        f"CIRCUIT BREAKER TRIGGERED!\n"
                        f"Daily PnL: {self._daily_pnl:.2f}\n"
                        f"Loss Limit: -{self._max_loss:.2f}\n"
                        f"All trading stopped."
                    ),
                },
            )

        await self._save_state()

    def is_active(self) -> bool:
        """Check if kill switch is active (trading should stop)."""
        return self._kill_switch

    def can_trade(self) -> bool:
        """Check if new trades are allowed."""
        return not self._kill_switch

    async def reset(self):
        """Reset circuit breaker for new trading day."""
        self._kill_switch = False
        self._daily_pnl = 0.0
        self._trades_today = 0
        await self._save_state()
        log.info("Circuit breaker reset for new trading day")

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_status(self) -> dict:
        return {
            "kill_switch": self._kill_switch,
            "daily_pnl": self._daily_pnl,
            "max_loss": self._max_loss,
            "trades_today": self._trades_today,
            "can_trade": self.can_trade(),
        }
