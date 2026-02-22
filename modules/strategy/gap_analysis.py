"""Gap Fill Strategy.

Nifty gaps fill ~65% of the time within the trading day.
- Gap UP: If open > prev close by > 0.3%, BUY into gap fill (SELL direction)
- Gap DOWN: If open < prev close by > 0.3%, SELL into gap fill (BUY direction)

Key insight: Trade WITH the gap fill, not against it.
"""

from datetime import time
from typing import Any, Dict, List, Optional
from modules.strategy.base_strategy import BaseStrategy
from modules.strategy.signal_models import Direction, Signal, SignalStrength
from modules.sensory.market_hours import now_ist


class GapAnalysis:
    """Detects and tracks gap fills."""

    def __init__(self, min_gap_pct: float = 0.3, max_gap_pct: float = 1.5):
        self.min_gap_pct = min_gap_pct
        self.max_gap_pct = max_gap_pct
        self._prev_day_close: Dict[str, float] = {}
        self._current_day: Dict[str, str] = {}
        self._today_open: Dict[str, float] = {}
        self._gap_pct: Dict[str, float] = {}
        self._gap_filled: Dict[str, bool] = {}

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

    def update(self, candle: Dict[str, Any]):
        """Update gap tracking state."""
        symbol = candle["symbol"]
        day = self._get_day(candle)

        if self._current_day.get(symbol) != day:
            # New day — record yesterday's close, capture today's open
            if symbol in self._current_day:
                # Store previous day's last known close
                pass  # prev_day_close updated below
            self._current_day[symbol] = day
            self._today_open[symbol] = candle["open"]
            self._gap_filled[symbol] = False

            # Calculate gap
            prev_close = self._prev_day_close.get(symbol)
            if prev_close and prev_close > 0:
                gap = ((candle["open"] - prev_close) / prev_close) * 100
                self._gap_pct[symbol] = gap
            else:
                self._gap_pct[symbol] = 0.0

        # Track gap fill
        prev_close = self._prev_day_close.get(symbol)
        gap = self._gap_pct.get(symbol, 0.0)
        if prev_close and abs(gap) >= self.min_gap_pct:
            if gap > 0 and candle["low"] <= prev_close:
                self._gap_filled[symbol] = True
            elif gap < 0 and candle["high"] >= prev_close:
                self._gap_filled[symbol] = True

        # Always update the close (last candle of day becomes prev_day_close)
        self._prev_day_close[symbol] = candle["close"]

    def has_gap(self, symbol: str) -> bool:
        gap = abs(self._gap_pct.get(symbol, 0.0))
        return self.min_gap_pct <= gap <= self.max_gap_pct

    def gap_direction(self, symbol: str) -> str:
        """Returns 'UP', 'DOWN', or 'NONE'."""
        gap = self._gap_pct.get(symbol, 0.0)
        if gap >= self.min_gap_pct:
            return "UP"
        elif gap <= -self.min_gap_pct:
            return "DOWN"
        return "NONE"

    def gap_pct(self, symbol: str) -> float:
        return self._gap_pct.get(symbol, 0.0)

    def is_filled(self, symbol: str) -> bool:
        return self._gap_filled.get(symbol, False)

    def prev_close(self, symbol: str) -> float:
        return self._prev_day_close.get(symbol, 0.0)
