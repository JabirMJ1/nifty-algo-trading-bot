"""Tests for execution engine."""

import pytest
from modules.execution.order_models import Order, Trade, OrderSide, OrderType, OrderStatus


class TestOrderModel:
    def test_create_order(self):
        order = Order(
            instrument="NSE:NIFTY25FEBFUT",
            side=OrderSide.BUY,
            quantity=50,
            price=22000,
        )
        assert order.instrument == "NSE:NIFTY25FEBFUT"
        assert order.side == OrderSide.BUY
        assert order.status == OrderStatus.PENDING

    def test_order_to_event_data(self):
        order = Order(
            instrument="NSE:NIFTY25FEBFUT",
            side=OrderSide.SELL,
            quantity=25,
        )
        data = order.to_event_data()
        assert data["instrument"] == "NSE:NIFTY25FEBFUT"
        assert data["side"] == "SELL"


class TestTradeModel:
    def test_calculate_pnl_buy(self):
        trade = Trade(
            instrument="NSE:NIFTY25FEBFUT",
            direction="BUY",
            entry_price=22000,
            exit_price=22100,
            quantity=50,
            entry_time="2024-01-01T10:00:00",
            costs=100,
        )
        pnl = trade.calculate_pnl()
        # (22100 - 22000) * 50 - 100 = 4900
        assert pnl == pytest.approx(4900, abs=1)

    def test_calculate_pnl_sell(self):
        trade = Trade(
            instrument="NSE:NIFTY25FEBFUT",
            direction="SELL",
            entry_price=22100,
            exit_price=22000,
            quantity=50,
            entry_time="2024-01-01T10:00:00",
            costs=100,
        )
        pnl = trade.calculate_pnl()
        assert pnl == pytest.approx(4900, abs=1)

    def test_calculate_pnl_loss(self):
        trade = Trade(
            instrument="NSE:NIFTY25FEBFUT",
            direction="BUY",
            entry_price=22000,
            exit_price=21900,
            quantity=50,
            entry_time="2024-01-01T10:00:00",
            costs=100,
        )
        pnl = trade.calculate_pnl()
        # (21900 - 22000) * 50 - 100 = -5100
        assert pnl == pytest.approx(-5100, abs=1)
