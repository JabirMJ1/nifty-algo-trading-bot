"""Tick → OHLCV candle aggregation module."""

from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger
from modules.sensory.market_hours import now_ist, utc_to_ist

log = get_logger("candle_builder")


class CandleBuilder:
    """Aggregates ticks into OHLCV candles of configurable interval."""

    def __init__(self, interval_minutes: int = None):
        self.interval = interval_minutes or settings.CANDLE_INTERVAL_MIN
        self._candles: Dict[str, Dict[str, Any]] = {}  # symbol -> building candle

    def _get_candle_start(self, dt: datetime) -> datetime:
        """Round down to nearest candle boundary."""
        minute = (dt.minute // self.interval) * self.interval
        return dt.replace(minute=minute, second=0, microsecond=0)

    async def on_tick(self, tick_data: Dict[str, Any]):
        """Process a tick and build/emit candles."""
        symbol = tick_data["symbol"]
        price = tick_data["last_price"]
        volume = tick_data.get("volume", 0)
        ts = tick_data.get("timestamp") or now_ist()

        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)

        # Kite exchange_timestamp is a naive UTC datetime in Docker (system tz = UTC).
        # Normalize to IST so all strategy time comparisons (9:30, 14:30, 23:00) work correctly.
        if ts.tzinfo is None:
            ts = utc_to_ist(ts)

        candle_start = self._get_candle_start(ts)
        candle_end = candle_start + timedelta(minutes=self.interval)

        current = self._candles.get(symbol)

        # If we have a building candle and the tick belongs to a new candle period
        if current and candle_start > current["timestamp"]:
            # The previous candle is complete — emit it
            await self._emit_candle(current)
            current = None

        if current is None:
            # Start a new candle
            self._candles[symbol] = {
                "symbol": symbol,
                "timestamp": candle_start,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "tick_count": 1,
            }
        else:
            # Update the building candle
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] = volume  # Kite sends cumulative volume
            current["tick_count"] += 1

    async def _emit_candle(self, candle: Dict[str, Any]):
        """Publish a completed candle to the event bus."""
        log.info(
            f"CANDLE {candle['symbol']} | "
            f"O:{candle['open']:.2f} H:{candle['high']:.2f} "
            f"L:{candle['low']:.2f} C:{candle['close']:.2f} "
            f"V:{candle['volume']} | {candle['timestamp']}"
        )
        await event_bus.publish(EventType.CANDLE, candle)

    async def force_close_all(self):
        """Force-close all building candles (e.g., at market close)."""
        for symbol, candle in list(self._candles.items()):
            await self._emit_candle(candle)
        self._candles.clear()
        log.info("All building candles force-closed")

    def start(self):
        """Subscribe to tick events."""
        event_bus.subscribe(EventType.TICK, self.on_tick)
        log.info(f"Candle builder started ({self.interval}min candles)")

    def get_building_candle(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get the current building (incomplete) candle for a symbol."""
        return self._candles.get(symbol)
