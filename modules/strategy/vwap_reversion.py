"""VWAP Mean Reversion Strategy.

Trades reversions to VWAP when price deviates significantly.
BUY when price drops far below VWAP and starts reverting up.
SELL when price rises far above VWAP and starts reverting down.
"""

from datetime import time
from typing import Any, Dict, List, Optional
from modules.strategy.base_strategy import BaseStrategy
from modules.strategy.signal_models import Direction, Signal, SignalStrength
from modules.sensory.market_hours import now_ist


def calculate_vwap_bands(candles: List[Dict[str, Any]]):
    """Calculate VWAP and standard deviation bands for today's candles."""
    if not candles:
        return 0, 0, 0

    cum_vp = 0.0
    cum_vol = 0.0
    vwap_values = []

    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        vol = max(c.get("volume", 1), 1)
        cum_vp += typical * vol
        cum_vol += vol
        vwap_values.append(cum_vp / cum_vol)

    vwap = vwap_values[-1] if vwap_values else 0

    # Standard deviation of price from VWAP
    if len(candles) > 1:
        closes = [c["close"] for c in candles]
        mean_diff_sq = sum((c - vwap) ** 2 for c in closes) / len(closes)
        std_dev = mean_diff_sq ** 0.5
    else:
        std_dev = 0

    return vwap, std_dev, vwap_values


class VWAPReversionStrategy(BaseStrategy):
    """VWAP mean reversion — buy below VWAP-1.5σ, sell above VWAP+1.5σ."""

    def __init__(self, deviation_threshold: float = 1.5):
        super().__init__("vwap_reversion")
        self.deviation = deviation_threshold
        self._today_candles: Dict[str, List[Dict]] = {}
        self._current_day: Dict[str, str] = {}
        self._traded_today: Dict[str, int] = {}
        self.max_trades_per_day = 2

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

        # New day reset
        if self._current_day.get(symbol) != day:
            self._current_day[symbol] = day
            self._today_candles[symbol] = []
            self._traded_today[symbol] = 0

        self._today_candles[symbol].append(candle)

        # Need at least 8 candles (2 hours) for reliable VWAP
        if len(self._today_candles[symbol]) < 8:
            return None

        # Don't trade before 10:30 or after 14:30
        if candle_time < time(10, 30) or candle_time >= time(14, 30):
            return None

        # Max trades per day
        if self._traded_today.get(symbol, 0) >= self.max_trades_per_day:
            return None

        vwap_val, std_dev, _ = calculate_vwap_bands(self._today_candles[symbol])

        if vwap_val == 0 or std_dev == 0:
            return None

        price = candle["close"]
        deviation_from_vwap = (price - vwap_val) / std_dev if std_dev > 0 else 0

        signal = None

        # BUY: price is significantly below VWAP and showing reversal
        # (close > open means bullish candle = potential reversal)
        if (
            deviation_from_vwap < -self.deviation
            and candle["close"] > candle["open"]  # bullish candle
        ):
            self._traded_today[symbol] = self._traded_today.get(symbol, 0) + 1
            strength = SignalStrength.STRONG if deviation_from_vwap < -2.0 else SignalStrength.MODERATE
            signal = Signal(
                instrument=symbol,
                direction=Direction.BUY,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=price,
                metadata={
                    "vwap": round(vwap_val, 2),
                    "std_dev": round(std_dev, 2),
                    "deviation": round(deviation_from_vwap, 2),
                    "target": round(vwap_val, 2),  # target is VWAP itself
                },
            )

        # SELL: price is significantly above VWAP and showing reversal
        elif (
            deviation_from_vwap > self.deviation
            and candle["close"] < candle["open"]  # bearish candle
        ):
            self._traded_today[symbol] = self._traded_today.get(symbol, 0) + 1
            strength = SignalStrength.STRONG if deviation_from_vwap > 2.0 else SignalStrength.MODERATE
            signal = Signal(
                instrument=symbol,
                direction=Direction.SELL,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=price,
                metadata={
                    "vwap": round(vwap_val, 2),
                    "std_dev": round(std_dev, 2),
                    "deviation": round(deviation_from_vwap, 2),
                    "target": round(vwap_val, 2),
                },
            )

        return signal
