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


def _rsi_raw(closes: List[float], period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    return 100 - (100 / (1 + avg_gain / avg_loss))


def stoch_rsi(closes: List[float], rsi_period: int = 14, stoch_period: int = 14, smooth_k: int = 3) -> float:
    """Stochastic RSI %K (smoothed). Returns 0–100; faster and more sensitive than plain RSI."""
    n = stoch_period + smooth_k - 1
    if len(closes) < rsi_period + n:
        return 50.0

    rsi_series = []
    for i in range(n):
        end = len(closes) - n + i + 1
        rsi_series.append(_rsi_raw(closes[max(0, end - rsi_period - 1):end], rsi_period))

    k_values = []
    for i in range(smooth_k):
        window = rsi_series[i: i + stoch_period]
        lo, hi = min(window), max(window)
        if hi == lo:
            k_values.append(50.0)
        else:
            k_values.append(100.0 * (rsi_series[i + stoch_period - 1] - lo) / (hi - lo))

    return sum(k_values) / smooth_k


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9):
    """Returns (macd_line, signal_line, histogram). Standard 12/26/9 MACD."""
    min_len = slow + signal_period - 1
    if len(closes) < min_len:
        return 0.0, 0.0, 0.0
    # Build the last signal_period MACD values for the signal EMA
    macd_vals = []
    for i in range(signal_period):
        end = len(closes) - (signal_period - 1 - i)
        w = closes[:end]
        macd_vals.append(ema(w, fast) - ema(w, slow))
    signal_line = ema(macd_vals, signal_period)
    macd_line = macd_vals[-1]
    return macd_line, signal_line, macd_line - signal_line


def find_support_resistance(
    candles: List[Dict[str, Any]],
    lookback: int = 1500,   # ~2 months of 15-min candles
    pivot_n: int = 8,       # bar must be extreme vs 8 bars each side → ~2h swing
    min_touches: int = 3,   # cluster must be tested ≥ 3 times to qualify
    cluster_pct: float = 0.003,  # levels within 0.3% treated as the same zone
    max_levels: int = 5,
):
    """Find significant monthly S&R levels by clustering multi-touch swing pivots.

    Only returns levels that price has respected at least min_touches times over
    the lookback window — these are the strong, well-tested zones.
    """
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if len(window) < pivot_n * 2 + 1:
        return [], []

    raw_res, raw_sup = [], []
    for i in range(pivot_n, len(window) - pivot_n):
        hood = window[i - pivot_n: i + pivot_n + 1]
        bar = window[i]
        if bar["high"] == max(c["high"] for c in hood):
            raw_res.append(bar["high"])
        if bar["low"] == min(c["low"] for c in hood):
            raw_sup.append(bar["low"])

    def cluster_and_filter(levels):
        if not levels:
            return []
        sorted_lvls = sorted(levels)
        clusters: List[List[float]] = []
        cur: List[float] = [sorted_lvls[0]]
        for lvl in sorted_lvls[1:]:
            if (lvl - cur[0]) / cur[0] <= cluster_pct:
                cur.append(lvl)
            else:
                clusters.append(cur)
                cur = [lvl]
        clusters.append(cur)

        # Keep only clusters with enough touches; rank by touch count
        qualified = [(sum(c) / len(c), len(c)) for c in clusters if len(c) >= min_touches]
        qualified.sort(key=lambda x: -x[1])
        return sorted(lvl for lvl, _ in qualified[:max_levels])

    return cluster_and_filter(raw_sup), cluster_and_filter(raw_res)


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
        self._prev_macd_line: Dict[str, float] = {}
        self._prev_signal_line: Dict[str, float] = {}
        self._prev_histogram: Dict[str, float] = {}
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
        if ts is None:
            return None
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            log.warning(f"Naive timestamp in candle {candle.get('symbol')}: {ts} — IST conversion missing")
        if hasattr(ts, "time"):
            return ts.time()
        return None

    def _get_sentiment_key(self, symbol: str) -> str:
        """Return 'COMMODITY' for MCX instruments, 'EQUITY' for all others."""
        exchange = symbol.split(":")[0].upper() if ":" in symbol else ""
        return "COMMODITY" if exchange == "MCX" else "EQUITY"

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

        # Wait until 12:00 — give the morning time to establish a clear bias
        if candle_time < time(12, 0):
            return None

        # ─── Strict day-quality filter: only trade confirmed trend days ──
        # Compute morning (9:15→12:00) metrics. Reject choppy/narrow-range
        # days AND require a clear directional bias from the morning.
        # Then only allow signals that AGREE with the morning bias.
        morning = [c for c in self._today_candles[symbol]
                   if self._get_time(c) and self._get_time(c) < time(12, 0)]
        morning_bullish = None
        if not morning:
            return None
        m_high = max(c["high"] for c in morning)
        m_low  = min(c["low"]  for c in morning)
        m_open = morning[0]["open"]
        m_close = morning[-1]["close"]
        spot = candle["close"]
        morning_range_pct = (m_high - m_low) / spot * 100
        morning_move_pct  = abs(m_close - m_open) / m_open * 100

        # Need both: wide range (vol expansion) and clear net direction
        if morning_range_pct < 0.6 or morning_move_pct < 0.3:
            return None
        morning_bullish = m_close > m_open

        # Stop new signals before force-close: MCX at 23:15 (force-close at 23:20), NSE at 14:30
        exchange = symbol.split(":")[0].upper() if ":" in symbol else "NSE"
        no_trade_cutoff = time(23, 15) if exchange == "MCX" else time(14, 30)
        if candle_time >= no_trade_cutoff:
            return None

        lunch_hour = False  # trading window starts at 12:00, no lunch penalty

        # Max trades per day
        if self._traded_today.get(symbol, 0) >= self.max_trades_per_day:
            return None

        # Need enough history for MACD 26/9 (slow EMA + signal period)
        history = self.get_history(symbol)
        if len(history) < 35:
            return None

        # ─── Phase 2: Calculate indicators ──────────────────────────
        closes = [c["close"] for c in history]
        price = candle["close"]

        macd_line, signal_line, histogram = macd(closes)
        current_srsi = stoch_rsi(closes)
        current_vwap = vwap(self._today_candles[symbol])
        adx = calculate_adx(history)
        atr = calculate_atr(history)
        supports, resistances = find_support_resistance(history)

        prev_macd = self._prev_macd_line.get(symbol)
        prev_signal = self._prev_signal_line.get(symbol)
        prev_hist = self._prev_histogram.get(symbol, 0.0)
        self._prev_macd_line[symbol] = macd_line
        self._prev_signal_line[symbol] = signal_line
        self._prev_histogram[symbol] = histogram

        if prev_macd is None or prev_signal is None:
            return None

        # ─── ADX gate: no signal in ranging/choppy markets ─────────────
        if adx < 20:
            return None

        # ─── 50-EMA trend alignment gate ────────────────────────────────
        # Only take BUY signals above the 50-EMA, SELL signals below it.
        ema50 = ema(closes, 50) if len(closes) >= 50 else None

        # ─── MACD histogram acceleration gate ───────────────────────────
        # Block BUY when histogram is falling (fading momentum).
        # Block SELL when histogram is rising. Crossover entries still pass
        # because a fresh cross takes histogram from negative→positive (growing).
        hist_bullish = histogram > 0 and histogram > prev_hist
        hist_bearish = histogram < 0 and histogram < prev_hist
        hist_fresh_bull_cross = prev_hist <= 0 < histogram
        hist_fresh_bear_cross = prev_hist >= 0 > histogram
        macd_ok_for_buy  = hist_bullish or hist_fresh_bull_cross
        macd_ok_for_sell = hist_bearish or hist_fresh_bear_cross

        # ─── 1-hour MACD alignment gate ────────────────────────────────
        # 15-min cross often fights the 1-hour trend. Block conflicting
        # signals. Take every 4th 15-min close as a rough hourly close.
        h1_ok_for_buy = True
        h1_ok_for_sell = True
        if len(closes) >= 35 * 4:
            hourly_closes = closes[::4]
            h1_macd, h1_signal, _ = macd(hourly_closes)
            h1_ok_for_buy  = h1_macd >= h1_signal
            h1_ok_for_sell = h1_macd <= h1_signal

        # ─── Candle body filter: skip indecisive candles (dojis) ────
        body = abs(candle["close"] - candle["open"])
        full_range = candle["high"] - candle["low"]
        if full_range > 0 and body / full_range < 0.3:
            return None

        # ─── Phase 3: Score BUY and SELL independently ──────────────
        sentiment_key = self._get_sentiment_key(symbol)
        sentiment = self._sentiment_cache.get(sentiment_key, {})

        # Apply EMA50 trend, histogram, 1h MACD, and morning-bias gates
        buy_blocked  = (not macd_ok_for_buy  or not h1_ok_for_buy
                        or (ema50 and price < ema50) or not morning_bullish)
        sell_blocked = (not macd_ok_for_sell or not h1_ok_for_sell
                        or (ema50 and price > ema50) or morning_bullish)

        buy_score  = 0 if buy_blocked else self._score_buy(
            symbol, candle, price, macd_line, signal_line, histogram,
            prev_macd, prev_signal, prev_hist, current_srsi, current_vwap,
            adx, atr, lunch_hour, sentiment, supports, resistances
        )
        sell_score = 0 if sell_blocked else self._score_sell(
            symbol, candle, price, macd_line, signal_line, histogram,
            prev_macd, prev_signal, prev_hist, current_srsi, current_vwap,
            adx, atr, lunch_hour, sentiment, supports, resistances
        )

        # ─── Phase 4: Emit signal if score threshold met ────────────
        nearest_sup = max((s for s in supports if s < price), default=None)
        nearest_res = min((r for r in resistances if r > price), default=None)
        log.debug(
            f"SCORE {symbol} | buy={buy_score} sell={sell_score} | "
            f"price={price:.1f} vwap={current_vwap:.1f} stoch_rsi={current_srsi:.1f} "
            f"macd={macd_line:.2f} sig={signal_line:.2f} hist={histogram:.2f} "
            f"sup={nearest_sup} res={nearest_res} "
            f"adx={adx:.1f} regime={self.vix.preferred_mode(symbol)} "
            f"first_hour={self._first_hour_direction.get(symbol)}"
        )

        min_score = 60
        signal = None

        if buy_score >= min_score and buy_score > sell_score:
            strength = SignalStrength.STRONG if buy_score >= 75 else SignalStrength.MODERATE
            if not candle.get("warmup"):
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
                    "stoch_rsi": round(current_srsi, 1),
                    "macd": round(macd_line, 2),
                    "macd_hist": round(histogram, 2),
                    "support": round(nearest_sup, 1) if nearest_sup else None,
                    "resistance": round(nearest_res, 1) if nearest_res else None,
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
            if not candle.get("warmup"):
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
                    "stoch_rsi": round(current_srsi, 1),
                    "macd": round(macd_line, 2),
                    "macd_hist": round(histogram, 2),
                    "support": round(nearest_sup, 1) if nearest_sup else None,
                    "resistance": round(nearest_res, 1) if nearest_res else None,
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
        self, symbol, candle, price, macd_line, signal_line, histogram,
        prev_macd, prev_signal, prev_hist, current_srsi, current_vwap,
        adx, atr, lunch_hour, sentiment: Dict = None,
        supports: list = None, resistances: list = None
    ) -> int:
        """Score a potential BUY signal (0-100).

        Requires a primary trigger (MACD crossover/momentum or mean-reversion setup)
        before adding confirmation factors.
        """
        score = 0
        regime = self.vix.preferred_mode(symbol)
        has_primary_trigger = False

        # ── Primary Trigger: MACD (required baseline, max +30) ──
        bullish_cross = prev_macd <= prev_signal and macd_line > signal_line
        hist_growing = histogram > 0 and histogram > prev_hist

        if bullish_cross:
            # Fresh bullish MACD crossover
            score += 30
            has_primary_trigger = True
        elif macd_line > signal_line and hist_growing and candle["close"] > candle["open"]:
            # Established uptrend with accelerating histogram — momentum continuation
            score += 25
            has_primary_trigger = True
        elif regime == "MEAN_REVERSION":
            # Mean reversion trigger: price far below VWAP + oversold StochRSI
            if (price < current_vwap * 0.997
                    and candle["close"] > candle["open"]
                    and current_srsi < 30):
                score += 30
                has_primary_trigger = True

        if not has_primary_trigger:
            return 0

        # ── Factor 2: StochRSI confirmation (max +15) ──
        if regime == "MEAN_REVERSION":
            if current_srsi < 20:
                score += 15
            elif current_srsi < 40:
                score += 10
        else:
            if 50 < current_srsi < 80:
                score += 15
            elif 40 < current_srsi <= 50:
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
            score -= 15

        # ── Factor 6: Monthly S&R zones (max +20, min -20) ──
        if atr > 0 and supports is not None and resistances is not None:
            nearest_sup = max((s for s in supports if s < price), default=None)
            nearest_res = min((r for r in resistances if r > price), default=None)
            if nearest_sup:
                dist = price - nearest_sup
                if dist <= 0.5 * atr:
                    score += 20  # sitting on a well-tested monthly support
                elif dist <= 1.5 * atr:
                    score += 10
            if nearest_res:
                dist = nearest_res - price
                if dist <= 0.5 * atr:
                    score -= 20  # running into a major resistance wall
                elif dist <= 1.5 * atr:
                    score -= 10

        # ── Factor 7: News Sentiment (max +20, min -20) ──
        if sentiment:
            s = sentiment.get("score", 0.0)
            mag = sentiment.get("magnitude", 0.0)
            if s > 0.7 and mag > 0.5:
                if price > current_vwap:
                    score += 20
                else:
                    score += 5
            elif s > 0.3 and mag > 0.3:
                score += 10
            elif s < -0.7 and mag > 0.5:
                score -= 20
            elif s < -0.3 and mag > 0.3:
                score -= 10

        # ── Penalties ──
        if lunch_hour:
            score -= 15
        if current_srsi > 85:
            score -= 20

        return max(0, score)

    def _score_sell(
        self, symbol, candle, price, macd_line, signal_line, histogram,
        prev_macd, prev_signal, prev_hist, current_srsi, current_vwap,
        adx, atr, lunch_hour, sentiment: Dict = None,
        supports: list = None, resistances: list = None
    ) -> int:
        """Score a potential SELL signal (0-100).

        Requires a primary trigger before adding confirmation factors.
        """
        score = 0
        regime = self.vix.preferred_mode(symbol)
        has_primary_trigger = False

        # ── Primary Trigger: MACD (required baseline, max +30) ──
        bearish_cross = prev_macd >= prev_signal and macd_line < signal_line
        hist_falling = histogram < 0 and histogram < prev_hist

        if bearish_cross:
            score += 30
            has_primary_trigger = True
        elif macd_line < signal_line and hist_falling and candle["close"] < candle["open"]:
            score += 25
            has_primary_trigger = True
        elif regime == "MEAN_REVERSION":
            # Mean reversion trigger: price far above VWAP + overbought StochRSI
            if (price > current_vwap * 1.003
                    and candle["close"] < candle["open"]
                    and current_srsi > 70):
                score += 30
                has_primary_trigger = True

        if not has_primary_trigger:
            return 0

        # ── Factor 2: StochRSI confirmation (max +15) ──
        if regime == "MEAN_REVERSION":
            if current_srsi > 80:
                score += 15
            elif current_srsi > 60:
                score += 10
        else:
            if 20 < current_srsi < 50:
                score += 15
            elif 50 <= current_srsi < 60:
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

        # ── Factor 6: Monthly S&R zones (max +20, min -20) ──
        if atr > 0 and supports is not None and resistances is not None:
            nearest_sup = max((s for s in supports if s < price), default=None)
            nearest_res = min((r for r in resistances if r > price), default=None)
            if nearest_res:
                dist = nearest_res - price
                if dist <= 0.5 * atr:
                    score += 20  # selling right at a major tested resistance
                elif dist <= 1.5 * atr:
                    score += 10
            if nearest_sup:
                dist = price - nearest_sup
                if dist <= 0.5 * atr:
                    score -= 20  # selling into a well-tested support floor
                elif dist <= 1.5 * atr:
                    score -= 10

        # ── Factor 7: News Sentiment (max +20, min -20) ──
        if sentiment:
            s = sentiment.get("score", 0.0)
            mag = sentiment.get("magnitude", 0.0)
            if s < -0.7 and mag > 0.5:
                if price < current_vwap:
                    score += 20
                else:
                    score += 5
            elif s < -0.3 and mag > 0.3:
                score += 10
            elif s > 0.7 and mag > 0.5:
                score -= 20
            elif s > 0.3 and mag > 0.3:
                score -= 10

        # ── Penalties ──
        if lunch_hour:
            score -= 15
        if current_srsi < 15:
            score -= 20

        return max(0, score)
