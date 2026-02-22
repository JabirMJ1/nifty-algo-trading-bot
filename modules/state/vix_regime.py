"""India VIX regime detector.

Uses VIX levels to classify market regime:
- HIGH_VOL (VIX > 18): Mean reversion strategies work best
- NORMAL (13 <= VIX <= 18): Mixed — use consensus
- LOW_VOL (VIX < 13): Momentum/trend strategies work best

Since we can't get live VIX from Kite historical API easily,
we estimate realized volatility from price data as a proxy.
"""

from typing import Any, Dict, List
from core.logger import get_logger

log = get_logger("vix_regime")


class VIXRegime:
    """Market volatility regime detection using realized volatility as VIX proxy."""

    # Regime thresholds (annualized volatility %)
    HIGH_VOL_THRESHOLD = 18.0
    LOW_VOL_THRESHOLD = 13.0

    def __init__(self):
        self._daily_closes: Dict[str, List[float]] = {}
        self._current_day: Dict[str, str] = {}
        self._regime: Dict[str, str] = {}
        self._vol_estimate: Dict[str, float] = {}

    def _get_day(self, candle: Dict[str, Any]) -> str:
        ts = candle["timestamp"]
        if hasattr(ts, "strftime"):
            return ts.strftime("%Y-%m-%d")
        return str(ts)[:10]

    def update(self, candle: Dict[str, Any]):
        """Update regime estimate from latest candle."""
        symbol = candle["symbol"]
        day = self._get_day(candle)

        if symbol not in self._daily_closes:
            self._daily_closes[symbol] = []
            self._current_day[symbol] = ""

        # Track one close per day (last candle of day)
        if self._current_day.get(symbol) != day:
            # New day — save previous day's close
            if self._current_day.get(symbol):
                self._daily_closes[symbol].append(candle["close"])
            self._current_day[symbol] = day
        else:
            # Update today's close (overwrite with latest)
            if self._daily_closes[symbol]:
                self._daily_closes[symbol][-1] = candle["close"]
            else:
                self._daily_closes[symbol].append(candle["close"])

        # Keep last 30 daily closes
        self._daily_closes[symbol] = self._daily_closes[symbol][-30:]

        # Need at least 5 days for regime detection
        if len(self._daily_closes[symbol]) < 5:
            self._regime[symbol] = "UNKNOWN"
            self._vol_estimate[symbol] = 0.0
            return

        self._calculate_regime(symbol)

    def _calculate_regime(self, symbol: str):
        """Calculate annualized realized volatility and classify regime."""
        closes = self._daily_closes[symbol]

        # Daily log returns
        returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                import math
                returns.append(math.log(closes[i] / closes[i - 1]))

        if len(returns) < 4:
            return

        # Realized volatility (annualized)
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        daily_vol = variance ** 0.5
        annualized_vol = daily_vol * (252 ** 0.5) * 100  # as percentage

        self._vol_estimate[symbol] = annualized_vol
        prev_regime = self._regime.get(symbol)

        if annualized_vol > self.HIGH_VOL_THRESHOLD:
            self._regime[symbol] = "HIGH_VOL"
        elif annualized_vol < self.LOW_VOL_THRESHOLD:
            self._regime[symbol] = "LOW_VOL"
        else:
            self._regime[symbol] = "NORMAL"

        if prev_regime and prev_regime != self._regime[symbol]:
            log.info(
                f"VIX regime change {symbol}: {prev_regime} -> {self._regime[symbol]} "
                f"(vol={annualized_vol:.1f}%)"
            )

    def current_regime(self, symbol: str) -> str:
        return self._regime.get(symbol, "UNKNOWN")

    def volatility(self, symbol: str) -> float:
        return self._vol_estimate.get(symbol, 0.0)

    def is_high_vol(self, symbol: str) -> bool:
        return self.current_regime(symbol) == "HIGH_VOL"

    def is_low_vol(self, symbol: str) -> bool:
        return self.current_regime(symbol) == "LOW_VOL"

    def preferred_mode(self, symbol: str) -> str:
        """Return which strategy mode to prefer based on regime."""
        regime = self.current_regime(symbol)
        if regime == "HIGH_VOL":
            return "MEAN_REVERSION"
        elif regime == "LOW_VOL":
            return "MOMENTUM"
        else:
            return "MIXED"
