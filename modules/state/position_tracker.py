"""Live position tracking with unrealized PnL calculation."""

import json
from typing import Any, Dict, Optional
from core.event_bus import event_bus, EventType
from core.logger import get_logger

log = get_logger("position_tracker")


class PositionTracker:
    """Tracks open positions and calculates unrealized PnL.

    Uses Redis for persistence, falls back to in-memory.
    """

    REDIS_KEY_PREFIX = "positions:"

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._last_prices: Dict[str, float] = {}

    async def on_trade(self, trade_data: Dict[str, Any]):
        """Update positions on trade events."""
        trade_type = trade_data.get("type")

        if trade_type == "ENTRY":
            symbol = trade_data["instrument"]
            self._positions[symbol] = {
                "order_id": trade_data.get("order_id"),
                "direction": trade_data["direction"],
                "entry_price": trade_data["price"],
                "quantity": trade_data["quantity"],
                "strategy": trade_data.get("strategy"),
                "stop_loss": trade_data.get("stop_loss", 0),
                "target": trade_data.get("target", 0),
                "unrealized_pnl": 0.0,
            }
            await self._persist(symbol)
            log.info(f"Position opened: {trade_data['direction']} {symbol}")

        elif trade_type == "EXIT":
            trade = trade_data.get("trade", {})
            symbol = trade.get("instrument") or trade_data.get("instrument")
            if symbol and symbol in self._positions:
                del self._positions[symbol]
                await self._remove(symbol)
                log.info(f"Position closed: {symbol}")

        await event_bus.publish(
            EventType.POSITION_UPDATE,
            {"positions": self.get_all_positions()},
        )

    async def on_tick(self, tick_data: Dict[str, Any]):
        """Update unrealized PnL on each tick."""
        symbol = tick_data["symbol"]
        price = tick_data["last_price"]
        self._last_prices[symbol] = price

        if symbol in self._positions:
            pos = self._positions[symbol]
            if pos["direction"] == "BUY":
                pos["unrealized_pnl"] = (price - pos["entry_price"]) * pos["quantity"]
            else:
                pos["unrealized_pnl"] = (pos["entry_price"] - price) * pos["quantity"]

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._positions.get(symbol)

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._positions)

    def get_open_count(self) -> int:
        return len(self._positions)

    def get_total_unrealized_pnl(self) -> float:
        return sum(p.get("unrealized_pnl", 0) for p in self._positions.values())

    async def _persist(self, symbol: str):
        if self._redis:
            key = f"{self.REDIS_KEY_PREFIX}{symbol}"
            self._redis.set(key, json.dumps(self._positions[symbol]), ex=86400)

    async def _remove(self, symbol: str):
        if self._redis:
            self._redis.delete(f"{self.REDIS_KEY_PREFIX}{symbol}")

    async def load_from_redis(self):
        """Load positions from Redis on startup."""
        if not self._redis:
            return
        keys = self._redis.keys(f"{self.REDIS_KEY_PREFIX}*")
        for key in keys:
            data = self._redis.get(key)
            if data:
                symbol = key.decode().replace(self.REDIS_KEY_PREFIX, "")
                self._positions[symbol] = json.loads(data)
        log.info(f"Loaded {len(self._positions)} positions from Redis")

    def start(self):
        """Subscribe to events."""
        event_bus.subscribe(EventType.TRADE, self.on_trade)
        event_bus.subscribe(EventType.TICK, self.on_tick)
        log.info("Position tracker started")
