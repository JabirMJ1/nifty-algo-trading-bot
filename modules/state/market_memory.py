"""Market regime detection — trend vs range using ADX."""

from typing import Any, Dict, List
from core.event_bus import event_bus, EventType
from core.logger import get_logger

log = get_logger("market_memory")

ADX_TREND_THRESHOLD = 25


def calculate_adx(candles: List[Dict[str, Any]], period: int = 14) -> float:
    """Calculate ADX (Average Directional Index).

    ADX > 25 = trending market
    ADX < 25 = ranging market
    """
    if len(candles) < period * 2:
        return 0.0

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    # Calculate +DM, -DM, TR
    plus_dm = []
    minus_dm = []
    tr_list = []

    for i in range(1, len(candles)):
        high_diff = highs[i] - highs[i - 1]
        low_diff = lows[i - 1] - lows[i]

        pdm = high_diff if high_diff > low_diff and high_diff > 0 else 0
        mdm = low_diff if low_diff > high_diff and low_diff > 0 else 0
        plus_dm.append(pdm)
        minus_dm.append(mdm)

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

    if len(tr_list) < period:
        return 0.0

    # Smoothed averages (Wilder's smoothing)
    def wilder_smooth(values: List[float], p: int) -> List[float]:
        smoothed = [sum(values[:p])]
        for v in values[p:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / p) + v)
        return smoothed

    smooth_tr = wilder_smooth(tr_list, period)
    smooth_pdm = wilder_smooth(plus_dm, period)
    smooth_mdm = wilder_smooth(minus_dm, period)

    min_len = min(len(smooth_tr), len(smooth_pdm), len(smooth_mdm))
    if min_len == 0:
        return 0.0

    # Calculate DI+ and DI-
    dx_values = []
    for i in range(min_len):
        if smooth_tr[i] == 0:
            continue
        pdi = (smooth_pdm[i] / smooth_tr[i]) * 100
        mdi = (smooth_mdm[i] / smooth_tr[i]) * 100
        di_sum = pdi + mdi
        if di_sum == 0:
            continue
        dx = abs(pdi - mdi) / di_sum * 100
        dx_values.append(dx)

    if len(dx_values) < period:
        return sum(dx_values) / len(dx_values) if dx_values else 0.0

    # ADX = smoothed average of DX
    adx = sum(dx_values[-period:]) / period
    return adx


class MarketMemory:
    """Tracks market regime (trending vs ranging) per symbol."""

    def __init__(self):
        self._candle_history: Dict[str, List[Dict[str, Any]]] = {}
        self._regimes: Dict[str, str] = {}
        self._adx_values: Dict[str, float] = {}

    async def on_candle(self, candle_data: Dict[str, Any]):
        """Update regime on new candle."""
        symbol = candle_data["symbol"]

        if symbol not in self._candle_history:
            self._candle_history[symbol] = []
        self._candle_history[symbol].append(candle_data)
        # Keep last 100 candles
        self._candle_history[symbol] = self._candle_history[symbol][-100:]

        adx = calculate_adx(self._candle_history[symbol])
        self._adx_values[symbol] = adx

        prev_regime = self._regimes.get(symbol)
        new_regime = "TRENDING" if adx >= ADX_TREND_THRESHOLD else "RANGING"
        self._regimes[symbol] = new_regime

        if prev_regime and prev_regime != new_regime:
            log.info(f"Regime change {symbol}: {prev_regime} -> {new_regime} (ADX={adx:.1f})")

    def current_regime(self, symbol: str) -> str:
        """Get current market regime for a symbol."""
        return self._regimes.get(symbol, "UNKNOWN")

    def get_adx(self, symbol: str) -> float:
        return self._adx_values.get(symbol, 0.0)

    def is_trending(self, symbol: str) -> bool:
        return self.current_regime(symbol) == "TRENDING"

    def start(self):
        event_bus.subscribe(EventType.CANDLE, self.on_candle)
        log.info("Market memory started")
