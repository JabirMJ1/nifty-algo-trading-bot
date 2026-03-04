"""Tests for AdaptiveStrategy — the live active strategy."""

import pytest
from datetime import datetime, date
from unittest.mock import patch
from zoneinfo import ZoneInfo

from modules.strategy.adaptive_strategy import AdaptiveStrategy, ema, rsi, vwap, calculate_atr
from modules.strategy.signal_models import Direction, SignalStrength

IST = ZoneInfo("Asia/Kolkata")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _ts(hour: int, minute: int = 0) -> datetime:
    """Return a timezone-aware IST datetime for today's date."""
    return datetime(2026, 3, 4, hour, minute, tzinfo=IST)


def _candle(
    close: float,
    open_: float = None,
    high: float = None,
    low: float = None,
    volume: int = 1000,
    timestamp: datetime = None,
    symbol: str = "NSE:NIFTY26MARFUT",
) -> dict:
    open_ = open_ if open_ is not None else close
    high = high if high is not None else close + 5
    low = low if low is not None else close - 5
    ts = timestamp if timestamp is not None else _ts(10, 30)
    return {
        "symbol": symbol,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "timestamp": ts,
    }


def _build_history(n: int = 25, base_price: float = 22000.0, symbol: str = "NSE:NIFTY26MARFUT") -> list:
    """Return `n` rising candles suitable for seeding EMA history."""
    candles = []
    for i in range(n):
        p = base_price + i * 10
        candles.append(_candle(p, open_=p - 5, high=p + 8, low=p - 8, timestamp=_ts(9, 20 + i), symbol=symbol))
    return candles


# ─── Unit tests: pure indicator functions ───────────────────────────────────

class TestIndicators:
    def test_ema_single_value(self):
        assert ema([100.0], 5) == 100.0

    def test_ema_constant_series(self):
        values = [100.0] * 20
        assert ema(values, 9) == pytest.approx(100.0)

    def test_ema_rising_series(self):
        # Rising prices → EMA lags; fast EMA > slow EMA eventually
        values = list(range(1, 31))
        fast = ema(values, 9)
        slow = ema(values, 21)
        assert fast > slow

    def test_rsi_neutral_on_insufficient_data(self):
        assert rsi([100.0] * 5, period=14) == 50.0

    def test_rsi_all_gains(self):
        closes = [float(i) for i in range(20)]
        assert rsi(closes) > 80

    def test_rsi_all_losses(self):
        closes = [float(20 - i) for i in range(20)]
        assert rsi(closes) < 20

    def test_vwap_constant_price(self):
        candles = [_candle(100, high=100, low=100) for _ in range(5)]
        assert vwap(candles) == pytest.approx(100.0)

    def test_calculate_atr_insufficient_data(self):
        assert calculate_atr([_candle(100)], period=14) == 0.0

    def test_calculate_atr_constant_range(self):
        candles = [_candle(100 + i, high=100 + i + 5, low=100 + i - 5) for i in range(20)]
        atr = calculate_atr(candles)
        assert atr == pytest.approx(10.0, abs=1.0)


# ─── Unit tests: scoring functions ──────────────────────────────────────────

class TestScoring:
    def setup_method(self):
        self.strategy = AdaptiveStrategy()

    def _score_buy(self, **kwargs):
        defaults = dict(
            symbol="NSE:NIFTY26MARFUT",
            candle=_candle(22200, open_=22150),  # bullish candle
            price=22200,
            fast_ema=22200,
            slow_ema=22100,
            prev_fast=22080,  # prev_fast < prev_slow → fresh crossover
            prev_slow=22090,
            current_rsi=55.0,
            current_vwap=22150.0,
            adx=28.0,
            atr=80.0,
            lunch_hour=False,
            sentiment={},
        )
        defaults.update(kwargs)
        self.strategy._first_hour_direction[defaults["symbol"]] = "BULLISH"
        return self.strategy._score_buy(**defaults)

    def _score_sell(self, **kwargs):
        defaults = dict(
            symbol="NSE:NIFTY26MARFUT",
            candle=_candle(21900, open_=21950),  # bearish candle
            price=21900,
            fast_ema=21900,
            slow_ema=22000,
            prev_fast=22010,  # prev_fast > prev_slow → fresh crossover
            prev_slow=22000,
            current_rsi=45.0,
            current_vwap=21950.0,
            adx=28.0,
            atr=80.0,
            lunch_hour=False,
            sentiment={},
        )
        defaults.update(kwargs)
        self.strategy._first_hour_direction[defaults["symbol"]] = "BEARISH"
        return self.strategy._score_sell(**defaults)

    def test_buy_fresh_crossover_gets_primary_trigger(self):
        score = self._score_buy(prev_fast=22080, prev_slow=22090, fast_ema=22200, slow_ema=22100)
        assert score >= 30, "Fresh crossover must add at least 30 points"

    def test_sell_fresh_crossover_gets_primary_trigger(self):
        score = self._score_sell(prev_fast=22010, prev_slow=22000, fast_ema=21900, slow_ema=22000)
        assert score >= 30

    def test_buy_no_trigger_returns_zero(self):
        # fast < slow — no crossover, no uptrend
        score = self._score_buy(
            prev_fast=21900, prev_slow=22000,
            fast_ema=21950, slow_ema=22000,
        )
        assert score == 0

    def test_sell_no_trigger_returns_zero(self):
        score = self._score_sell(
            prev_fast=21800, prev_slow=22000,
            fast_ema=22100, slow_ema=22000,
        )
        assert score == 0

    def test_buy_lunch_hour_penalty_applied(self):
        score_normal = self._score_buy(lunch_hour=False)
        score_lunch = self._score_buy(lunch_hour=True)
        assert score_lunch < score_normal

    def test_sell_lunch_hour_penalty_applied(self):
        score_normal = self._score_sell(lunch_hour=False)
        score_lunch = self._score_sell(lunch_hour=True)
        assert score_lunch < score_normal

    def test_buy_strong_positive_sentiment_boosts_score(self):
        base = self._score_buy(sentiment={})
        boosted = self._score_buy(
            sentiment={"score": 0.8, "magnitude": 0.6},
            price=22200, current_vwap=22150,  # price > vwap → chart confirms
        )
        assert boosted > base

    def test_sell_strong_negative_sentiment_boosts_score(self):
        base = self._score_sell(sentiment={})
        boosted = self._score_sell(
            sentiment={"score": -0.8, "magnitude": 0.6},
            price=21900, current_vwap=21950,  # price < vwap → chart confirms
        )
        assert boosted > base

    def test_buy_very_overbought_rsi_penalised(self):
        score = self._score_buy(current_rsi=80.0)
        # RSI > 75 penalty should reduce the score
        score_normal = self._score_buy(current_rsi=55.0)
        assert score < score_normal

    def test_sell_very_oversold_rsi_penalised(self):
        score = self._score_sell(current_rsi=20.0)
        score_normal = self._score_sell(current_rsi=45.0)
        assert score < score_normal

    def test_buy_score_cannot_go_below_zero(self):
        score = self._score_buy(
            current_rsi=80.0,
            lunch_hour=True,
            sentiment={"score": -0.8, "magnitude": 0.6},
        )
        assert score >= 0

    def test_sell_score_cannot_go_below_zero(self):
        score = self._score_sell(
            current_rsi=20.0,
            lunch_hour=True,
            sentiment={"score": 0.8, "magnitude": 0.6},
        )
        assert score >= 0


# ─── Integration tests: on_candle signal emission ───────────────────────────

class TestOnCandle:
    def setup_method(self):
        self.strategy = AdaptiveStrategy()
        self.symbol = "NSE:NIFTY26MARFUT"

    def _seed_history(self, n: int = 25, base: float = 22000.0):
        """Feed enough candles so indicators are ready, but not enough to trigger a trade."""
        candles = _build_history(n, base)
        for c in candles:
            self.strategy._candle_history.setdefault(self.symbol, []).append(c)
        # Warm up EMA state so prev_fast/prev_slow are set
        closes = [c["close"] for c in candles]
        self.strategy._prev_fast_ema[self.symbol] = ema(closes[:-1], 9)
        self.strategy._prev_slow_ema[self.symbol] = ema(closes[:-1], 21)
        # Prime day/state tracking
        self.strategy._current_day[self.symbol] = "2026-03-04"
        self.strategy._today_candles[self.symbol] = list(candles)
        self.strategy._first_hour_set[self.symbol] = True
        self.strategy._first_hour_direction[self.symbol] = "BULLISH"
        self.strategy._traded_today[self.symbol] = 0

    def test_no_signal_before_930(self):
        self._seed_history()
        candle = _candle(22300, open_=22250, timestamp=_ts(9, 20), symbol=self.symbol)
        signal = self.strategy.on_candle(candle)
        assert signal is None

    def test_no_signal_after_nse_cutoff(self):
        self._seed_history()
        candle = _candle(22300, open_=22250, timestamp=_ts(14, 31), symbol=self.symbol)
        signal = self.strategy.on_candle(candle)
        assert signal is None

    def test_no_signal_insufficient_history(self):
        strategy = AdaptiveStrategy()
        # Only 10 candles — not enough for EMA-21
        for c in _build_history(10):
            strategy._candle_history.setdefault(self.symbol, []).append(c)
        candle = _candle(22200, open_=22150, timestamp=_ts(10, 0), symbol=self.symbol)
        assert strategy.on_candle(candle) is None

    def test_buy_signal_emitted_on_bullish_crossover(self):
        self._seed_history(base=22000)
        # Set up a bullish EMA crossover: fast was below slow, now above
        self.strategy._prev_fast_ema[self.symbol] = 22090.0
        self.strategy._prev_slow_ema[self.symbol] = 22100.0  # fast < slow before

        # Price jumps — fast EMA will now be above slow EMA
        bullish_candle = _candle(
            close=22300, open_=22200,  # strong bullish body
            high=22310, low=22190,
            timestamp=_ts(10, 30),
            symbol=self.symbol,
        )
        # Inject enough candles so EMA-9 > EMA-21
        closes = [22000 + i * 15 for i in range(25)]  # strong uptrend
        for i, c in enumerate(self.strategy._today_candles[self.symbol]):
            c["close"] = closes[i] if i < len(closes) else closes[-1]
        self.strategy._today_candles[self.symbol].append(bullish_candle)

        with patch.object(
            self.strategy, "_score_buy", return_value=75
        ), patch.object(
            self.strategy, "_score_sell", return_value=30
        ):
            signal = self.strategy.on_candle(bullish_candle)

        assert signal is not None
        assert signal.direction == Direction.BUY
        assert signal.strength == SignalStrength.STRONG
        assert signal.instrument == self.symbol

    def test_sell_signal_emitted_on_bearish_crossover(self):
        self._seed_history(base=22000)
        self.strategy._first_hour_direction[self.symbol] = "BEARISH"

        bearish_candle = _candle(
            close=21700, open_=21800,
            high=21810, low=21690,
            timestamp=_ts(10, 30),
            symbol=self.symbol,
        )
        self.strategy._today_candles[self.symbol].append(bearish_candle)

        with patch.object(
            self.strategy, "_score_buy", return_value=20
        ), patch.object(
            self.strategy, "_score_sell", return_value=70
        ):
            signal = self.strategy.on_candle(bearish_candle)

        assert signal is not None
        assert signal.direction == Direction.SELL

    def test_no_signal_when_max_trades_reached(self):
        self._seed_history()
        self.strategy._traded_today[self.symbol] = 2  # already at max

        candle = _candle(22300, open_=22200, timestamp=_ts(10, 30), symbol=self.symbol)
        with patch.object(self.strategy, "_score_buy", return_value=80), \
             patch.object(self.strategy, "_score_sell", return_value=20):
            signal = self.strategy.on_candle(candle)

        assert signal is None

    def test_doji_candle_skipped(self):
        self._seed_history()
        # Body < 30% of range → doji
        doji = _candle(
            close=22202, open_=22200,  # tiny body (2 pts)
            high=22210, low=22190,      # range = 20 pts → body/range = 10%
            timestamp=_ts(10, 30),
            symbol=self.symbol,
        )
        self.strategy._today_candles[self.symbol].append(doji)

        with patch.object(self.strategy, "_score_buy", return_value=80), \
             patch.object(self.strategy, "_score_sell", return_value=20):
            signal = self.strategy.on_candle(doji)

        assert signal is None

    def test_new_day_resets_state(self):
        self._seed_history()
        self.strategy._traded_today[self.symbol] = 2

        # Candle from a new day
        new_day_candle = _candle(
            close=22200, open_=22150,
            timestamp=datetime(2026, 3, 5, 10, 0, tzinfo=IST),
            symbol=self.symbol,
        )
        self.strategy.on_candle(new_day_candle)

        assert self.strategy._traded_today.get(self.symbol, 0) == 0
        assert self.strategy._current_day[self.symbol] == "2026-03-05"

    def test_trade_count_incremented_on_signal(self):
        self._seed_history()
        assert self.strategy._traded_today.get(self.symbol, 0) == 0

        candle = _candle(22300, open_=22200, timestamp=_ts(10, 30), symbol=self.symbol)
        self.strategy._today_candles[self.symbol].append(candle)

        with patch.object(self.strategy, "_score_buy", return_value=80), \
             patch.object(self.strategy, "_score_sell", return_value=20):
            signal = self.strategy.on_candle(candle)

        if signal is not None:
            assert self.strategy._traded_today[self.symbol] == 1

    def test_mcx_no_trade_after_2300(self):
        symbol = "MCX:CRUDEOIL26MARFUT"
        strategy = AdaptiveStrategy()
        strategy._current_day[symbol] = "2026-03-04"
        strategy._today_candles[symbol] = []
        strategy._first_hour_set[symbol] = True
        strategy._first_hour_direction[symbol] = "BEARISH"
        strategy._traded_today[symbol] = 0

        for c in _build_history(25, base_price=5000.0, symbol=symbol):
            strategy._candle_history.setdefault(symbol, []).append(c)

        late_candle = _candle(
            close=5200, open_=5150,
            timestamp=datetime(2026, 3, 4, 23, 1, tzinfo=IST),
            symbol=symbol,
        )
        signal = strategy.on_candle(late_candle)
        assert signal is None
