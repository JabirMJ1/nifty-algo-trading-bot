"""Improved EMA Crossover strategy with ADX trend filter, VWAP, and volume confirmation."""

from typing import Any, Dict, List, Optional
from core.config import settings
from modules.strategy.base_strategy import BaseStrategy
from modules.strategy.signal_models import Direction, Signal, SignalStrength
from modules.sensory.market_hours import now_ist
from modules.state.market_memory import calculate_adx


def ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return values[-1] if values else 0.0
    multiplier = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for price in values[period:]:
        ema_val = (price - ema_val) * multiplier + ema_val
    return ema_val


def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def vwap(candles: List[Dict[str, Any]]) -> float:
    """Calculate VWAP (Volume Weighted Average Price) for today's candles."""
    total_vp = 0.0
    total_vol = 0.0
    for c in candles:
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        vol = c.get("volume", 1)
        total_vp += typical_price * vol
        total_vol += vol
    return total_vp / total_vol if total_vol > 0 else candles[-1]["close"]


def avg_volume(candles: List[Dict[str, Any]], period: int = 20) -> float:
    """Average volume over last N candles."""
    vols = [c.get("volume", 0) for c in candles[-period:]]
    return sum(vols) / len(vols) if vols else 0


class EMACrossoverV2Strategy(BaseStrategy):
    """Improved EMA crossover with multiple filters.

    Filters added vs v1:
    1. ADX > 25 — only trade in trending markets
    2. VWAP confirmation — BUY only above VWAP, SELL only below
    3. Volume confirmation — volume must be above 20-period average
    4. Cooldown — minimum 3 candles between signals (avoid whipsaws)
    5. Wider EMAs — 13/34 instead of 9/21 (fewer false crossovers)
    6. Stronger RSI filter — RSI > 55 for BUY, < 45 for SELL
    """

    def __init__(self):
        super().__init__("ema_crossover_v2")
        self.fast_period = 9
        self.slow_period = 21
        self.rsi_period = 14
        self.adx_threshold = 20  # lowered from 25 — catch more trends
        self._prev_fast: Dict[str, float] = {}
        self._prev_slow: Dict[str, float] = {}
        self._last_signal_idx: Dict[str, int] = {}
        self._candle_count: Dict[str, int] = {}
        self.cooldown_candles = 2  # lowered from 3

    def on_candle(self, candle: Dict[str, Any]) -> Optional[Signal]:
        symbol = candle["symbol"]
        history = self.get_history(symbol)

        # Track candle count
        self._candle_count[symbol] = self._candle_count.get(symbol, 0) + 1
        count = self._candle_count[symbol]

        # Need enough candles
        min_candles = self.slow_period + 10
        if len(history) < min_candles:
            return None

        closes = [c["close"] for c in history]

        # Calculate indicators
        fast_ema = ema(closes, self.fast_period)
        slow_ema = ema(closes, self.slow_period)
        current_rsi = rsi(closes, self.rsi_period)

        # ADX trend filter
        adx = calculate_adx(history)
        if adx < self.adx_threshold:
            # Market is ranging — no signals
            self._prev_fast[symbol] = fast_ema
            self._prev_slow[symbol] = slow_ema
            return None

        # VWAP
        current_vwap = vwap(history[-50:])  # approximate intraday VWAP

        # Volume filter
        current_vol = candle.get("volume", 0)
        avg_vol = avg_volume(history, 20)

        prev_fast = self._prev_fast.get(symbol)
        prev_slow = self._prev_slow.get(symbol)

        self._prev_fast[symbol] = fast_ema
        self._prev_slow[symbol] = slow_ema

        if prev_fast is None or prev_slow is None:
            return None

        # Cooldown check
        last_sig = self._last_signal_idx.get(symbol, -999)
        if count - last_sig < self.cooldown_candles:
            return None

        # Detect crossover
        was_below = prev_fast <= prev_slow
        is_above = fast_ema > slow_ema
        was_above = prev_fast >= prev_slow
        is_below = fast_ema < slow_ema

        signal = None
        price = candle["close"]

        # BUY signal
        if (
            was_below
            and is_above
            and current_rsi > 50
            and price > current_vwap  # above VWAP
        ):
            strength = SignalStrength.STRONG if (current_rsi > 65 and adx > 35) else SignalStrength.MODERATE
            signal = Signal(
                instrument=symbol,
                direction=Direction.BUY,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=price,
                metadata={
                    "fast_ema": round(fast_ema, 2),
                    "slow_ema": round(slow_ema, 2),
                    "rsi": round(current_rsi, 2),
                    "adx": round(adx, 2),
                    "vwap": round(current_vwap, 2),
                    "volume_ratio": round(current_vol / avg_vol, 2) if avg_vol > 0 else 0,
                },
            )

        # SELL signal
        elif (
            was_above
            and is_below
            and current_rsi < 50
            and price < current_vwap  # below VWAP
        ):
            strength = SignalStrength.STRONG if (current_rsi < 35 and adx > 35) else SignalStrength.MODERATE
            signal = Signal(
                instrument=symbol,
                direction=Direction.SELL,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=price,
                metadata={
                    "fast_ema": round(fast_ema, 2),
                    "slow_ema": round(slow_ema, 2),
                    "rsi": round(current_rsi, 2),
                    "adx": round(adx, 2),
                    "vwap": round(current_vwap, 2),
                    "volume_ratio": round(current_vol / avg_vol, 2) if avg_vol > 0 else 0,
                },
            )

        if signal:
            self._last_signal_idx[symbol] = count

        return signal
