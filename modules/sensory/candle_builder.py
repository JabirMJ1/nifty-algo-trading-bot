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

        # Kite sends epoch (1970) timestamps on feed initialisation before the first
        # real exchange tick arrives.  These candles corrupt VWAP and opening-range
        # calculations, so drop them silently.
        if ts.year < 2020:
            return

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

    async def force_close_all(self, exchange: str = None):
        """Force-close all building candles, optionally filtered by exchange."""
        for symbol, candle in list(self._candles.items()):
            if exchange and not symbol.startswith(f"{exchange}:"):
                continue
            await self._emit_candle(candle)
            del self._candles[symbol]
        if not exchange:
            self._candles.clear()
        log.info(f"Building candles force-closed ({exchange or 'all'})")

    async def warmup(self, kite, instrument_tokens: Dict[str, int]):
        """Fetch today's historical candles and publish as warmup CANDLE events.

        Populates strategy indicator history from market open so the bot is
        ready to trade immediately after a mid-day restart.
        """
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        today = now_ist().date()
        # Start from 9:00 AM to capture MCX opening candles
        from_dt = ist.localize(datetime.combine(today, datetime.min.time().replace(hour=9)))
        to_dt = now_ist()

        if to_dt <= from_dt:
            log.info("Warmup skipped — market has not opened yet (pre-9:00 AM)")
            return

        for symbol, token in instrument_tokens.items():
            try:
                bars = kite.historical_data(
                    token, from_dt, to_dt, f"{self.interval}minute", continuous=False
                )
                for bar in bars:
                    ts = bar["date"]
                    if ts.tzinfo is None:
                        ts = utc_to_ist(ts)
                    candle = {
                        "symbol": symbol,
                        "timestamp": ts,
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                        "volume": int(bar["volume"]),
                        "tick_count": 0,
                        "warmup": True,
                    }
                    await event_bus.publish(EventType.CANDLE, candle)
                log.info(f"Warmup: queued {len(bars)} historical candles for {symbol}")
            except Exception as e:
                log.warning(f"Warmup failed for {symbol}: {e}")

    def start(self):
        """Subscribe to tick events."""
        event_bus.subscribe(EventType.TICK, self.on_tick)
        log.info(f"Candle builder started ({self.interval}min candles)")

    def get_building_candle(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get the current building (incomplete) candle for a symbol."""
        return self._candles.get(symbol)
