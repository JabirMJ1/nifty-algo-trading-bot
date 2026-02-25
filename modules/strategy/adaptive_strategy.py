"""Adaptive Smart Strategy — combines multiple edge sources with regime awareness.

Edge sources:
1. VIX Regime: Switch between mean-reversion and momentum based on volatility
2. Gap Fill: Trade gap fills in the first hour (65% fill rate historically)
3. First Hour Momentum: Direction after first hour predicts day ~58%
4. Time-of-Day Patterns: Avoid choppy lunch hours, trade morning/afternoon trends
5. Dynamic Position Sizing: Bet big on multi-signal alignment, small/skip otherwise

Signal scoring system (0-100):
- Each factor adds confidence points
- Trade only when score >= 65
- Score determines position size multiplier
"""

import math
from datetime import time
from typing import Any, Dict, List, Optional
from modules.strategy.base_strategy import BaseStrategy
from modules.strategy.signal_models import Direction, Signal, SignalStrength
from modules.strategy.gap_analysis import GapAnalysis
from modules.state.vix_regime import VIXRegime
from modules.state.market_memory import calculate_adx
from modules.sensory.market_hours import now_ist
from core.event_bus import EventType, event_bus
from core.logger import get_logger

log = get_logger("adaptive_strategy")


# ─── Helper functions ───────────────────────────────────────────────

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
    total_vp = 0.0
    total_vol = 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        vol = max(c.get("volume", 1), 1)
        total_vp += typical * vol
        total_vol += vol
    return total_vp / total_vol if total_vol > 0 else candles[-1]["close"]


def calculate_atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / min(period, len(trs))


# ─── Strategy ───────────────────────────────────────────────────────

class AdaptiveStrategy(BaseStrategy):
    """Adaptive strategy that scores signals using multiple edge factors."""

    def __init__(self):
        super().__init__("adaptive")
        self.vix = VIXRegime()
        self.gap = GapAnalysis(min_gap_pct=0.3, max_gap_pct=1.5)

        # Per-symbol state
        self._today_candles: Dict[str, List[Dict]] = {}
        self._current_day: Dict[str, str] = {}
        self._first_hour_direction: Dict[str, Optional[str]] = {}
        self._first_hour_set: Dict[str, bool] = {}
        self._traded_today: Dict[str, int] = {}
        self._prev_fast_ema: Dict[str, float] = {}
        self._prev_slow_ema: Dict[str, float] = {}
        self.max_trades_per_day = 2

        # Sentiment cache — keyed by "MARKET" or symbol
        self._sentiment_cache: Dict[str, Dict] = {}

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

    def start(self):
        """Subscribe to CANDLE and SENTIMENT events."""
        super().start()
        event_bus.subscribe(EventType.SENTIMENT, self._on_sentiment)

    async def _on_sentiment(self, data: Dict[str, Any]):
        """Cache the latest sentiment reading."""
        symbol = data.get("symbol", "MARKET")
        self._sentiment_cache[symbol] = data
        log.debug(
            f"Sentiment cached: {symbol} score={data.get('score', 0):+.2f} "
            f"magnitude={data.get('magnitude', 0):.2f} | {data.get('summary', '')}"
        )

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
            self._first_hour_direction[symbol] = None
            self._first_hour_set[symbol] = False
            self._traded_today[symbol] = 0

        self._today_candles[symbol].append(candle)

        # Update sub-modules
        self.vix.update(candle)
        self.gap.update(candle)

        # ─── Phase 1: Build opening range data (9:15 - 9:30) ───────
        if candle_time < time(9, 30):
            return None

        # Set opening range direction once
        if not self._first_hour_set.get(symbol):
            self._first_hour_set[symbol] = True
            first_candles = [
                c for c in self._today_candles[symbol]
                if self._get_time(c) and self._get_time(c) < time(9, 30)
            ]
            if first_candles:
                first_open = first_candles[0]["open"]
                last_close = first_candles[-1]["close"]
                if last_close > first_open * 1.001:
                    self._first_hour_direction[symbol] = "BULLISH"
                elif last_close < first_open * 0.999:
                    self._first_hour_direction[symbol] = "BEARISH"
                else:
                    self._first_hour_direction[symbol] = "NEUTRAL"
                log.info(
                    f"Opening range {symbol}: {self._first_hour_direction[symbol]} "
                    f"(open={first_open:.2f} close={last_close:.2f})"
                )

        # Don't trade in the last hour or during lunch chop
        if candle_time >= time(14, 30):
            return None

        # Lunch hour penalty (12:00-13:00) — reduce signals, don't block entirely
        lunch_hour = time(12, 0) <= candle_time < time(13, 0)

        # Max trades per day
        if self._traded_today.get(symbol, 0) >= self.max_trades_per_day:
            return None

        # Need enough history for EMA-21 (the slowest indicator)
        history = self.get_history(symbol)
        if len(history) < 21:
            return None

        # ─── Phase 2: Calculate indicators ──────────────────────────
        closes = [c["close"] for c in history]
        price = candle["close"]

        fast_ema = ema(closes, 9)
        slow_ema = ema(closes, 21)
        current_rsi = rsi(closes, 14)
        current_vwap = vwap(self._today_candles[symbol])
        adx = calculate_adx(history)
        atr = calculate_atr(history)

        prev_fast = self._prev_fast_ema.get(symbol)
        prev_slow = self._prev_slow_ema.get(symbol)
        self._prev_fast_ema[symbol] = fast_ema
        self._prev_slow_ema[symbol] = slow_ema

        if prev_fast is None or prev_slow is None:
            return None

        # ─── Candle body filter: skip indecisive candles (dojis) ────
        body = abs(candle["close"] - candle["open"])
        full_range = candle["high"] - candle["low"]
        if full_range > 0 and body / full_range < 0.3:
            return None

        # ─── Phase 3: Score BUY and SELL independently ──────────────
        sentiment = self._sentiment_cache.get("MARKET", {})
        buy_score = self._score_buy(
            symbol, candle, price, fast_ema, slow_ema,
            prev_fast, prev_slow, current_rsi, current_vwap,
            adx, atr, lunch_hour, sentiment
        )
        sell_score = self._score_sell(
            symbol, candle, price, fast_ema, slow_ema,
            prev_fast, prev_slow, current_rsi, current_vwap,
            adx, atr, lunch_hour, sentiment
        )

        # ─── Phase 4: Emit signal if score threshold met ────────────
        min_score = 65
        signal = None

        if buy_score >= min_score and buy_score > sell_score:
            strength = SignalStrength.STRONG if buy_score >= 75 else SignalStrength.MODERATE
            self._traded_today[symbol] = self._traded_today.get(symbol, 0) + 1
            signal = Signal(
                instrument=symbol,
                direction=Direction.BUY,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=price,
                metadata={
                    "score": buy_score,
                    "regime": self.vix.current_regime(symbol),
                    "vol": round(self.vix.volatility(symbol), 1),
                    "adx": round(adx, 1),
                    "rsi": round(current_rsi, 1),
                    "first_hour": self._first_hour_direction.get(symbol),
                    "gap": round(self.gap.gap_pct(symbol), 2),
                    "vwap": round(current_vwap, 2),
                    "stop_multiplier": 1.5,
                    "target_multiplier": 1.5,
                    "sentiment_score": round(sentiment.get("score", 0.0), 2),
                    "sentiment_summary": sentiment.get("summary", ""),
                },
            )
            log.info(f"BUY {symbol} | score={buy_score} | {signal.metadata}")

        elif sell_score >= min_score and sell_score > buy_score:
            strength = SignalStrength.STRONG if sell_score >= 75 else SignalStrength.MODERATE
            self._traded_today[symbol] = self._traded_today.get(symbol, 0) + 1
            signal = Signal(
                instrument=symbol,
                direction=Direction.SELL,
                strength=strength,
                timestamp=now_ist(),
                strategy_name=self.name,
                price=price,
                metadata={
                    "score": sell_score,
                    "regime": self.vix.current_regime(symbol),
                    "vol": round(self.vix.volatility(symbol), 1),
                    "adx": round(adx, 1),
                    "rsi": round(current_rsi, 1),
                    "first_hour": self._first_hour_direction.get(symbol),
                    "gap": round(self.gap.gap_pct(symbol), 2),
                    "vwap": round(current_vwap, 2),
                    "stop_multiplier": 1.5,
                    "target_multiplier": 1.5,
                    "sentiment_score": round(sentiment.get("score", 0.0), 2),
                    "sentiment_summary": sentiment.get("summary", ""),
                },
            )
            log.info(f"SELL {symbol} | score={sell_score} | {signal.metadata}")

        return signal

    def _score_buy(
        self, symbol, candle, price, fast_ema, slow_ema,
        prev_fast, prev_slow, current_rsi, current_vwap,
        adx, atr, lunch_hour, sentiment: Dict = None
    ) -> int:
        """Score a potential BUY signal (0-100).

        Requires a primary trigger (EMA crossover or strong mean-reversion setup)
        before adding confirmation factors.
        """
        score = 0
        regime = self.vix.preferred_mode(symbol)
        has_primary_trigger = False

        # ── Primary Trigger: EMA crossover (required baseline, max +30) ──
        was_below = prev_fast <= prev_slow
        is_above = fast_ema > slow_ema

        if was_below and is_above:
            # Fresh bullish crossover
            score += 30
            has_primary_trigger = True
        elif is_above and (fast_ema - slow_ema) / slow_ema > 0.002:
            # Strong established uptrend — momentum trigger
            if candle["close"] > candle["open"]:  # Confirm with bullish candle
                score += 25
                has_primary_trigger = True
        elif regime == "MEAN_REVERSION":
            # Mean reversion trigger: price far below VWAP + bullish candle
            if (price < current_vwap * 0.997
                    and candle["close"] > candle["open"]
                    and current_rsi < 40):
                score += 30
                has_primary_trigger = True

        # No primary trigger = no trade
        if not has_primary_trigger:
            return 0

        # ── Factor 2: RSI confirmation (max +15) ──
        if regime == "MEAN_REVERSION":
            if current_rsi < 35:
                score += 15  # Oversold — good for mean reversion buy
            elif current_rsi < 45:
                score += 10
        else:
            if 50 < current_rsi < 70:
                score += 15
            elif 45 < current_rsi <= 50:
                score += 5

        # ── Factor 3: VWAP position (max +15) ──
        if regime == "MOMENTUM":
            if price > current_vwap:
                score += 15
        elif regime == "MEAN_REVERSION":
            if price < current_vwap:
                score += 15
        else:
            if price > current_vwap:
                score += 10

        # ── Factor 4: ADX trend strength (max +10) ──
        if regime == "MOMENTUM":
            if adx > 25:
                score += 10
        elif regime == "MEAN_REVERSION":
            if adx < 20:
                score += 10
        else:
            if adx > 20:
                score += 8

        # ── Factor 5: First hour alignment (max +15) ──
        first_hour = self._first_hour_direction.get(symbol)
        if first_hour == "BULLISH":
            score += 15
        elif first_hour == "NEUTRAL":
            score += 3
        elif first_hour == "BEARISH" and regime != "MEAN_REVERSION":
            score -= 15  # Strong penalty for counter-trend in momentum

        # ── Factor 6: Gap fill (max +10) ──
        gap_dir = self.gap.gap_direction(symbol)
        gap_filled = self.gap.is_filled(symbol)
        candle_time = self._get_time(candle)

        if gap_dir == "DOWN" and not gap_filled:
            if candle_time and candle_time < time(11, 30):
                score += 10

        # ── Factor 7: News Sentiment (max +20, min -20) ──
        if sentiment:
            s = sentiment.get("score", 0.0)
            mag = sentiment.get("magnitude", 0.0)
            if s > 0.7 and mag > 0.5:       # Very bullish, high confidence
                if price > current_vwap:     # Chart confirms → amplify
                    score += 20
                else:                        # News not yet in chart → cautious boost
                    score += 5
            elif s > 0.3:                    # Moderately bullish
                score += 10
            elif s < -0.7 and mag > 0.5:    # Very bearish → penalise buy
                score -= 20
            elif s < -0.3:                   # Moderately bearish
                score -= 10

        # ── Penalties ──
        if lunch_hour:
            score -= 15
        if current_rsi > 75:
            score -= 20

        return max(0, score)

    def _score_sell(
        self, symbol, candle, price, fast_ema, slow_ema,
        prev_fast, prev_slow, current_rsi, current_vwap,
        adx, atr, lunch_hour, sentiment: Dict = None
    ) -> int:
        """Score a potential SELL signal (0-100).

        Requires a primary trigger before adding confirmation factors.
        """
        score = 0
        regime = self.vix.preferred_mode(symbol)
        has_primary_trigger = False

        # ── Primary Trigger: EMA crossover (required baseline, max +30) ──
        was_above = prev_fast >= prev_slow
        is_below = fast_ema < slow_ema

        if was_above and is_below:
            score += 30
            has_primary_trigger = True
        elif is_below and (slow_ema - fast_ema) / slow_ema > 0.002:
            # Strong established downtrend — momentum trigger
            if candle["close"] < candle["open"]:  # Confirm with bearish candle
                score += 25
                has_primary_trigger = True
        elif regime == "MEAN_REVERSION":
            # Mean reversion trigger: price far above VWAP + bearish candle
            if (price > current_vwap * 1.003
                    and candle["close"] < candle["open"]
                    and current_rsi > 60):
                score += 30
                has_primary_trigger = True

        if not has_primary_trigger:
            return 0

        # ── Factor 2: RSI confirmation (max +15) ──
        if regime == "MEAN_REVERSION":
            if current_rsi > 65:
                score += 15
            elif current_rsi > 55:
                score += 10
        else:
            if 30 < current_rsi < 50:
                score += 15
            elif 50 <= current_rsi < 55:
                score += 5

        # ── Factor 3: VWAP position (max +15) ──
        if regime == "MOMENTUM":
            if price < current_vwap:
                score += 15
        elif regime == "MEAN_REVERSION":
            if price > current_vwap:
                score += 15
        else:
            if price < current_vwap:
                score += 10

        # ── Factor 4: ADX trend strength (max +10) ──
        if regime == "MOMENTUM":
            if adx > 25:
                score += 10
        elif regime == "MEAN_REVERSION":
            if adx < 20:
                score += 10
        else:
            if adx > 20:
                score += 8

        # ── Factor 5: First hour alignment (max +15) ──
        first_hour = self._first_hour_direction.get(symbol)
        if first_hour == "BEARISH":
            score += 15
        elif first_hour == "NEUTRAL":
            score += 3
        elif first_hour == "BULLISH" and regime != "MEAN_REVERSION":
            score -= 15

        # ── Factor 6: Gap fill (max +10) ──
        gap_dir = self.gap.gap_direction(symbol)
        gap_filled = self.gap.is_filled(symbol)
        candle_time = self._get_time(candle)

        if gap_dir == "UP" and not gap_filled:
            if candle_time and candle_time < time(11, 30):
                score += 10

        # ── Factor 7: News Sentiment (max +20, min -20) ──
        if sentiment:
            s = sentiment.get("score", 0.0)
            mag = sentiment.get("magnitude", 0.0)
            if s < -0.7 and mag > 0.5:      # Very bearish, high confidence
                if price < current_vwap:     # Chart confirms → amplify
                    score += 20
                else:                        # News not yet in chart → cautious boost
                    score += 5
            elif s < -0.3:                   # Moderately bearish
                score += 10
            elif s > 0.7 and mag > 0.5:     # Very bullish → penalise sell
                score -= 20
            elif s > 0.3:                    # Moderately bullish
                score -= 10

        # ── Penalties ──
        if lunch_hour:
            score -= 15
        if current_rsi < 25:
            score -= 20

        return max(0, score)
