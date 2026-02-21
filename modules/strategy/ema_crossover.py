"""EMA 9/21 Crossover strategy with RSI confirmation."""

from typing import Any, Dict, List, Optional
from core.config import settings
from modules.strategy.base_strategy import BaseStrategy
from modules.strategy.signal_models import Direction, Signal, SignalStrength
from modules.sensory.market_hours import now_ist


def ema(values: List[float], period: int) -> float:
    """Calculate EMA for the given values and period."""
    if len(values) < period:
        return values[-1] if values else 0.0
    multiplier = 2 / (period + 1)
    ema_val = sum(values[:period]) / period  # SMA seed
    for price in values[period:]:
        ema_val = (price - ema_val) * multiplier + ema_val
    return ema_val


def rsi(closes: List[float], period: int = 14) -> float:
    """Calculate RSI for the given close prices."""
    if len(closes) < period + 1:
        return 50.0  # neutral default

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-(period):]

    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]

    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001  # avoid div by zero

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


class EMACrossoverStrategy(BaseStrategy):
    """EMA 9/21 crossover with RSI(14) confirmation.

    BUY:  fast EMA crosses above slow EMA + RSI > 50
    SELL: fast EMA crosses below slow EMA + RSI < 50
    """

    def __init__(self):
        super().__init__("ema_crossover")
        self.fast_period = settings.EMA_FAST
        self.slow_period = settings.EMA_SLOW
        self.rsi_period = settings.RSI_PERIOD
        self._prev_fast: Dict[str, float] = {}
        self._prev_slow: Dict[str, float] = {}

    def on_candle(self, candle: Dict[str, Any]) -> Optional[Signal]:
        symbol = candle["symbol"]
        history = self.get_history(symbol)

        # Need enough candles for slow EMA
        min_candles = self.slow_period + 2
        if len(history) < min_candles:
            self.log.debug(
                f"{symbol}: waiting for candles ({len(history)}/{min_candles})"
            )
            return None

        closes = [c["close"] for c in history]

        # Calculate indicators
        fast_ema = ema(closes, self.fast_period)
        slow_ema = ema(closes, self.slow_period)
        current_rsi = rsi(closes, self.rsi_period)

        prev_fast = self._prev_fast.get(symbol)
        prev_slow = self._prev_slow.get(symbol)

        # Store for next iteration
        self._prev_fast[symbol] = fast_ema
        self._prev_slow[symbol] = slow_ema

        # Need previous values to detect crossover
        if prev_fast is None or prev_slow is None:
            return None

        # Detect crossover
        was_below = prev_fast <= prev_slow
        is_above = fast_ema > slow_ema
        was_above = prev_fast >= prev_slow
        is_below = fast_ema < slow_ema

        signal = None

        # BUY: fast crosses above slow + RSI > 50
        if was_below and is_above and current_rsi > 50:
            strength = (
                SignalStrength.STRONG if current_rsi > 60 else SignalStrength.MODERATE
            )
            signal = Signal(
                instrument=symbol,
                direction=Direction.BUY,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=candle["close"],
                metadata={
                    "fast_ema": round(fast_ema, 2),
                    "slow_ema": round(slow_ema, 2),
                    "rsi": round(current_rsi, 2),
                    "candle_timestamp": str(candle["timestamp"]),
                },
            )

        # SELL: fast crosses below slow + RSI < 50
        elif was_above and is_below and current_rsi < 50:
            strength = (
                SignalStrength.STRONG if current_rsi < 40 else SignalStrength.MODERATE
            )
            signal = Signal(
                instrument=symbol,
                direction=Direction.SELL,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=candle["close"],
                metadata={
                    "fast_ema": round(fast_ema, 2),
                    "slow_ema": round(slow_ema, 2),
                    "rsi": round(current_rsi, 2),
                    "candle_timestamp": str(candle["timestamp"]),
                },
            )

        return signal
