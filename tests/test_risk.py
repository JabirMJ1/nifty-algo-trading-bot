"""Tests for risk management modules."""

import pytest
from modules.risk.position_sizer import PositionSizer, calculate_atr
from modules.risk.stop_manager import StopManager, StopLevel
from modules.risk.circuit_breaker import CircuitBreaker


class TestATR:
    def _make_candles(self, data):
        return [
            {"open": d[0], "high": d[1], "low": d[2], "close": d[3]}
            for d in data
        ]

    def test_atr_basic(self):
        # 15 candles with known ranges
        candles = self._make_candles(
            [(100, 105, 95, 102)] * 16
        )
        atr = calculate_atr(candles, period=14)
        assert atr == pytest.approx(10.0, abs=0.5)

    def test_atr_insufficient_data(self):
        candles = self._make_candles([(100, 110, 90, 105)])
        atr = calculate_atr(candles, period=14)
        assert atr == 20  # high - low of last candle


class TestPositionSizer:
    def test_basic_sizing(self):
        sizer = PositionSizer()
        sizer.capital = 100000
        sizer.risk_pct = 0.01  # 1%

        size = sizer.calculate_size(atr=50, current_open_positions=0)
        # risk_amount = 1000, size = 1000/50 = 20
        assert size == 20

    def test_max_positions_reached(self):
        sizer = PositionSizer()
        size = sizer.calculate_size(atr=50, current_open_positions=3)
        assert size == 0

    def test_zero_atr(self):
        sizer = PositionSizer()
        size = sizer.calculate_size(atr=0, current_open_positions=0)
        assert size == 0


class TestStopManager:
    def test_buy_stop_hit(self):
        stop = StopLevel(entry_price=100, direction="BUY", atr=10)
        # Stop should be at 100 - 15 = 85
        assert stop.stop_loss == pytest.approx(85, abs=0.1)
        result = stop.update(84)
        assert result == "STOP_HIT"

    def test_buy_target_hit(self):
        stop = StopLevel(entry_price=100, direction="BUY", atr=10)
        # Target should be at 100 + 30 = 130
        assert stop.target == pytest.approx(130, abs=0.1)
        result = stop.update(131)
        assert result == "TARGET_HIT"

    def test_trailing_stop(self):
        stop = StopLevel(entry_price=100, direction="BUY", atr=10)
        # Move price up to activate trailing (1x ATR = 110)
        result = stop.update(112)
        assert result is None
        assert stop.trailing_active is True

        # Trailing stop should move up
        result = stop.update(120)
        assert result is None
        assert stop.stop_loss > 85  # moved up from initial

    def test_sell_stop_hit(self):
        stop = StopLevel(entry_price=100, direction="SELL", atr=10)
        result = stop.update(116)  # stop at 115
        assert result == "STOP_HIT"


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_triggers_on_loss_limit(self):
        breaker = CircuitBreaker()
        breaker._max_loss = 2000  # 2% of 100k

        await breaker.update_pnl(-1500)
        assert breaker.can_trade() is True

        await breaker.update_pnl(-600)
        assert breaker.can_trade() is False
        assert breaker.is_active() is True

    @pytest.mark.asyncio
    async def test_reset(self):
        breaker = CircuitBreaker()
        breaker._kill_switch = True
        await breaker.reset()
        assert breaker.can_trade() is True
        assert breaker.get_daily_pnl() == 0
