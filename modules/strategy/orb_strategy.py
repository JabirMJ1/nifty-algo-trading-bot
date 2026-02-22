"""Opening Range Breakout (ORB) Strategy.

Trades the breakout of the first 30 minutes (9:15-9:45) range.
BUY when price breaks above the opening range high.
SELL when price breaks below the opening range low.
"""

from datetime import time
from typing import Any, Dict, List, Optional
from modules.strategy.base_strategy import BaseStrategy
from modules.strategy.signal_models import Direction, Signal, SignalStrength
from modules.sensory.market_hours import now_ist


class ORBStrategy(BaseStrategy):
    """Opening Range Breakout — trade the first 30-min range breakout."""

    def __init__(self, orb_minutes: int = 30):
        super().__init__("orb")
        self.orb_minutes = orb_minutes
        self._orb_high: Dict[str, float] = {}
        self._orb_low: Dict[str, float] = {}
        self._orb_set: Dict[str, bool] = {}
        self._traded_today: Dict[str, bool] = {}
        self._current_day: Dict[str, str] = {}
        self._orb_candles: Dict[str, List[Dict]] = {}

    def _get_day(self, candle: Dict[str, Any]) -> str:
        ts = candle["timestamp"]
        if hasattr(ts, "strftime"):
            return ts.strftime("%Y-%m-%d")
        return str(ts)[:10]

    def _get_time(self, candle: Dict[str, Any]) -> Optional[time]:
        ts = candle["timestamp"]
        if hasattr(ts, "time"):
            return ts.time()
        return None

    def on_candle(self, candle: Dict[str, Any]) -> Optional[Signal]:
        symbol = candle["symbol"]
        day = self._get_day(candle)
        candle_time = self._get_time(candle)

        if candle_time is None:
            return None

        # New day — reset
        if self._current_day.get(symbol) != day:
            self._current_day[symbol] = day
            self._orb_high[symbol] = 0
            self._orb_low[symbol] = float("inf")
            self._orb_set[symbol] = False
            self._traded_today[symbol] = False
            self._orb_candles[symbol] = []

        # Building the opening range (9:15 - 9:45)
        orb_end = time(9, 45)
        if candle_time <= orb_end:
            self._orb_candles[symbol] = self._orb_candles.get(symbol, [])
            self._orb_candles[symbol].append(candle)
            self._orb_high[symbol] = max(self._orb_high[symbol], candle["high"])
            self._orb_low[symbol] = min(self._orb_low[symbol], candle["low"])
            return None

        # ORB is set after 9:45
        if not self._orb_set.get(symbol):
            self._orb_set[symbol] = True
            orb_range = self._orb_high[symbol] - self._orb_low[symbol]
            self.log.info(
                f"ORB set for {symbol}: High={self._orb_high[symbol]:.2f} "
                f"Low={self._orb_low[symbol]:.2f} Range={orb_range:.2f}"
            )

        # Only one trade per day per symbol
        if self._traded_today.get(symbol):
            return None

        # Don't trade after 14:30 (too close to market close)
        if candle_time >= time(14, 30):
            return None

        orb_high = self._orb_high[symbol]
        orb_low = self._orb_low[symbol]
        orb_range = orb_high - orb_low
        price = candle["close"]

        # Skip if range is too narrow (< 0.1% of price) — likely a flat day
        if orb_range < price * 0.001:
            return None

        # Skip if range is too wide (> 1.5% of price) — too risky
        if orb_range > price * 0.015:
            return None

        signal = None

        # BUY: close above ORB high
        if price > orb_high:
            self._traded_today[symbol] = True
            strength = SignalStrength.STRONG if price > orb_high + orb_range * 0.2 else SignalStrength.MODERATE
            signal = Signal(
                instrument=symbol,
                direction=Direction.BUY,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=price,
                metadata={
                    "orb_high": round(orb_high, 2),
                    "orb_low": round(orb_low, 2),
                    "orb_range": round(orb_range, 2),
                    "breakout_pct": round((price - orb_high) / orb_range * 100, 2),
                },
            )

        # SELL: close below ORB low
        elif price < orb_low:
            self._traded_today[symbol] = True
            strength = SignalStrength.STRONG if price < orb_low - orb_range * 0.2 else SignalStrength.MODERATE
            signal = Signal(
                instrument=symbol,
                direction=Direction.SELL,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=price,
                metadata={
                    "orb_high": round(orb_high, 2),
                    "orb_low": round(orb_low, 2),
                    "orb_range": round(orb_range, 2),
                    "breakout_pct": round((orb_low - price) / orb_range * 100, 2),
                },
            )

        return signal
