"""Extensive tests for OrderManager — live execution engine.

Kite Connect is fully mocked — no real API calls.
Perspective: ₹1,00,000 capital live trader.

Key behaviours tested:
  - Signal gating (circuit breaker, duplicate direction)
  - Position creation and SL-M order placement
  - Capital tracking (balance + sizer.update_capital) after close
  - deployed_margin reduces available capital for subsequent positions
  - Stop/target triggers via on_tick
  - Retry logic and graceful failure handling
  - close_all_positions (EOD / shutdown)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from datetime import datetime
from zoneinfo import ZoneInfo

from modules.execution.order_manager import OrderManager
from modules.risk.position_sizer import PositionSizer
from modules.risk.stop_manager import StopManager
from modules.risk.circuit_breaker import CircuitBreaker
from core.config import settings

IST     = ZoneInfo("Asia/Kolkata")
CAPITAL = 100_000
SYMBOL  = "NSE:NIFTY26MARFUT"
ATR     = 100.0
ENTRY   = 22_000.0


# ─── Builders ────────────────────────────────────────────────────────────────

def make_kite(order_id="ORD001") -> MagicMock:
    """Return a mock KiteConnect that always succeeds."""
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY  = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"
    kite.ORDER_TYPE_MARKET = "MARKET"
    kite.ORDER_TYPE_SL_M   = "SL-M"
    kite.VARIETY_REGULAR   = "regular"
    kite.PRODUCT_MIS       = "MIS"
    kite.place_order.return_value = order_id
    kite.cancel_order.return_value = True
    return kite


def make_sizer(capital=CAPITAL) -> PositionSizer:
    s = PositionSizer()
    s.capital = capital
    s.risk_pct = 0.01
    s.max_positions = 3
    return s


def make_manager(capital=CAPITAL, order_id="ORD001") -> tuple:
    kite    = make_kite(order_id)
    sizer   = make_sizer(capital)
    stops   = StopManager()
    breaker = CircuitBreaker()
    breaker._max_loss = capital * 0.02
    mgr = OrderManager(kite=kite, position_sizer=sizer,
                       stop_manager=stops, circuit_breaker=breaker)
    return mgr, kite


def make_signal(direction="BUY", price=ENTRY, symbol=SYMBOL) -> dict:
    return {
        "instrument":    symbol,
        "direction":     direction,
        "price":         price,
        "strategy_name": "test",
        "timestamp":     datetime(2026, 3, 5, 10, 30, tzinfo=IST),
    }


def make_candle(symbol=SYMBOL, close=ENTRY, high=None, low=None) -> dict:
    return {
        "symbol":    symbol,
        "open":      close - 20,
        "high":      high  if high  is not None else close + ATR,
        "low":       low   if low   is not None else close - ATR,
        "close":     close,
        "volume":    1_000,
        "timestamp": datetime(2026, 3, 5, 10, 45, tzinfo=IST),
    }


def seed_candles(mgr: OrderManager, symbol=SYMBOL, n=20, base=ENTRY) -> None:
    candles = [
        {
            "symbol": symbol, "open": base + i,
            "high":   base + i + ATR, "low": base + i - ATR,
            "close":  base + i + ATR * 0.5, "volume": 1_000,
            "timestamp": datetime(2026, 3, 5, 9, 20 + i, tzinfo=IST),
        }
        for i in range(n)
    ]
    mgr._candle_history[symbol] = candles


@pytest.fixture
def mock_bus():
    with patch("modules.execution.order_manager.event_bus") as m:
        m.publish = AsyncMock()
        yield m


# ═══════════════════════════════════════════════════════════════════════════════
# Signal Gating
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalGating:
    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_signal(self, mock_bus):
        mgr, kite = make_manager()
        mgr.breaker._kill_switch = True
        await mgr.on_signal(make_signal("BUY"))
        kite.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_atr_no_order_placed(self, mock_bus):
        """No candle history → ATR=0 → no order."""
        mgr, kite = make_manager()
        await mgr.on_signal(make_signal("BUY"))
        kite.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_direction_duplicate_ignored(self, mock_bus):
        """Already long → another BUY signal → no new order."""
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))
        assert SYMBOL in mgr.positions

        call_count = kite.place_order.call_count
        await mgr.on_signal(make_signal("BUY"))
        assert kite.place_order.call_count == call_count  # no new call

    @pytest.mark.asyncio
    async def test_opposite_signal_closes_existing(self, mock_bus):
        """Long open + SELL signal → close long, open short."""
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))
        assert SYMBOL in mgr.positions
        assert mgr.positions[SYMBOL]["direction"] == "BUY"

        await mgr.on_signal(make_signal("SELL"))
        assert SYMBOL in mgr.positions
        assert mgr.positions[SYMBOL]["direction"] == "SELL"


# ═══════════════════════════════════════════════════════════════════════════════
# Order Placement
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrderPlacement:
    @pytest.mark.asyncio
    async def test_buy_signal_calls_place_order(self, mock_bus):
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))
        kite.place_order.assert_called()

    @pytest.mark.asyncio
    async def test_sell_signal_calls_place_order(self, mock_bus):
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("SELL"))
        kite.place_order.assert_called()

    @pytest.mark.asyncio
    async def test_entry_transaction_type_correct_for_buy(self, mock_bus):
        """BUY signal → transaction_type=BUY."""
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))

        args = kite.place_order.call_args_list[0]
        assert args.kwargs.get("transaction_type") == "BUY"

    @pytest.mark.asyncio
    async def test_entry_transaction_type_correct_for_sell(self, mock_bus):
        """SELL signal → transaction_type=SELL."""
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("SELL"))

        args = kite.place_order.call_args_list[0]
        assert args.kwargs.get("transaction_type") == "SELL"

    @pytest.mark.asyncio
    async def test_position_recorded_after_order(self, mock_bus):
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))

        assert SYMBOL in mgr.positions
        assert mgr.positions[SYMBOL]["order_id"] == "ORD001"

    @pytest.mark.asyncio
    async def test_sl_order_placed_after_entry(self, mock_bus):
        """Exchange-level SL-M order must be placed to protect position."""
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))

        # At least 2 place_order calls: entry + SL-M
        assert kite.place_order.call_count >= 2

    @pytest.mark.asyncio
    async def test_sl_order_id_stored_in_position(self, mock_bus):
        mgr, kite = make_manager()
        kite.place_order.side_effect = ["ORD001", "SL001"]  # entry, SL
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))

        assert mgr.positions[SYMBOL]["sl_order_id"] == "SL001"

    @pytest.mark.asyncio
    async def test_trade_event_published_after_entry(self, mock_bus):
        from core.event_bus import EventType
        mgr, kite = make_manager()
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))

        types = [c.args[0] for c in mock_bus.publish.call_args_list]
        assert EventType.TRADE in types


# ═══════════════════════════════════════════════════════════════════════════════
# Retry Logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestRetryLogic:
    @pytest.mark.asyncio
    async def test_order_retried_on_transient_failure(self, mock_bus):
        """First call fails, second succeeds → position still created."""
        mgr, kite = make_manager()
        kite.place_order.side_effect = [Exception("timeout"), "ORD001", "SL001"]
        seed_candles(mgr)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.on_signal(make_signal("BUY"))

        assert SYMBOL in mgr.positions

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_no_position(self, mock_bus):
        """All 3 retries fail → no position created."""
        mgr, kite = make_manager()
        kite.place_order.side_effect = Exception("exchange down")
        seed_candles(mgr)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.on_signal(make_signal("BUY"))

        assert SYMBOL not in mgr.positions

    @pytest.mark.asyncio
    async def test_failed_order_publishes_alert(self, mock_bus):
        from core.event_bus import EventType
        mgr, kite = make_manager()
        kite.place_order.side_effect = Exception("down")
        seed_candles(mgr)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.on_signal(make_signal("BUY"))

        types = [c.args[0] for c in mock_bus.publish.call_args_list]
        assert EventType.ALERT in types


# ═══════════════════════════════════════════════════════════════════════════════
# Stop / Tick Monitoring
# ═══════════════════════════════════════════════════════════════════════════════

class TestTickMonitoring:
    @pytest.mark.asyncio
    async def test_stop_hit_closes_position_on_tick(self, mock_bus):
        mgr, kite = make_manager()
        kite.place_order.side_effect = ["ORD001", "SL001", "CLOSE001"]
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))
        assert SYMBOL in mgr.positions

        stop = mgr.stops.get_stop_level(SYMBOL).stop_loss
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.on_tick({"symbol": SYMBOL, "last_price": stop - 1})

        assert SYMBOL not in mgr.positions

    @pytest.mark.asyncio
    async def test_target_hit_closes_position_on_tick(self, mock_bus):
        mgr, kite = make_manager()
        kite.place_order.side_effect = ["ORD001", "SL001", "CLOSE001"]
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))

        target = mgr.stops.get_stop_level(SYMBOL).target
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.on_tick({"symbol": SYMBOL, "last_price": target + 1})

        assert SYMBOL not in mgr.positions

    @pytest.mark.asyncio
    async def test_sl_order_cancelled_before_market_close(self, mock_bus):
        """When closing via on_tick, standing SL-M must be cancelled first."""
        mgr, kite = make_manager()
        kite.place_order.side_effect = ["ORD001", "SL001", "CLOSE001"]
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))

        stop = mgr.stops.get_stop_level(SYMBOL).stop_loss
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.on_tick({"symbol": SYMBOL, "last_price": stop - 1})

        kite.cancel_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_for_symbol_without_position_ignored(self, mock_bus):
        """No position open → tick should be a no-op."""
        mgr, kite = make_manager()
        await mgr.on_tick({"symbol": SYMBOL, "last_price": ENTRY})
        kite.place_order.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Capital Tracking (Bug Fix: 2026-03-05)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCapitalTracking:
    @pytest.mark.asyncio
    async def test_balance_initialised_to_capital(self, mock_bus):
        mgr, _ = make_manager(capital=CAPITAL)
        assert mgr.balance == float(CAPITAL)

    @pytest.mark.asyncio
    async def test_sizer_capital_updated_after_stop_loss(self, mock_bus):
        """After a losing trade, sizer.capital must decrease."""
        mgr, kite = make_manager()
        kite.place_order.side_effect = ["ORD001", "SL001", "CLOSE001"]
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))

        stop = mgr.stops.get_stop_level(SYMBOL).stop_loss
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.on_tick({"symbol": SYMBOL, "last_price": stop - 1})

        assert mgr.sizer.capital == pytest.approx(mgr.balance, rel=0.01)
        assert mgr.balance != float(CAPITAL)  # changed after trade

    @pytest.mark.asyncio
    async def test_deployed_margin_passed_to_sizer(self, mock_bus):
        """
        With one position open, the second on_signal call must compute
        deployed_margin > 0 and pass it to calculate_size.
        We verify by patching calculate_size to capture kwargs.
        """
        mgr, kite = make_manager(capital=CAPITAL)
        kite.place_order.side_effect = ["ORD1", "SL1"]
        seed_candles(mgr)

        # Track calculate_size calls
        original_calc = mgr.sizer.calculate_size
        calls_deployed = []

        def tracking_calc(*args, **kwargs):
            calls_deployed.append(kwargs.get("deployed_margin", 0.0))
            return original_calc(*args, **kwargs)

        mgr.sizer.calculate_size = tracking_calc

        # First position
        await mgr.on_signal(make_signal("BUY", symbol=SYMBOL))
        assert SYMBOL in mgr.positions
        assert calls_deployed[0] == 0.0  # no prior positions

        # Inject a second position manually so deployed_margin is non-zero on next call
        symbol2 = "MCX:CRUDEOILFUT"
        seed_candles(mgr, symbol=symbol2, base=5_000)
        kite.place_order.side_effect = ["ORD2", "SL2"]
        await mgr.on_signal(make_signal("BUY", symbol=symbol2, price=5_000))

        # Second call must have positive deployed_margin (from SYMBOL position)
        assert len(calls_deployed) >= 2
        assert calls_deployed[1] > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Candle History
# ═══════════════════════════════════════════════════════════════════════════════

class TestCandleHistory:
    @pytest.mark.asyncio
    async def test_on_candle_stores_history(self, mock_bus):
        mgr, _ = make_manager()
        candle = make_candle()
        await mgr.on_candle(candle)
        assert SYMBOL in mgr._candle_history
        assert len(mgr._candle_history[SYMBOL]) == 1

    @pytest.mark.asyncio
    async def test_candle_history_capped_at_100(self, mock_bus):
        """Memory guard: only the last 100 candles are retained."""
        mgr, _ = make_manager()
        for i in range(120):
            await mgr.on_candle(make_candle(close=ENTRY + i))
        assert len(mgr._candle_history[SYMBOL]) == 100


# ═══════════════════════════════════════════════════════════════════════════════
# EOD / Shutdown
# ═══════════════════════════════════════════════════════════════════════════════

class TestEODClose:
    @pytest.mark.asyncio
    async def test_close_all_positions_sends_market_order(self, mock_bus):
        mgr, kite = make_manager()
        kite.place_order.side_effect = ["ORD001", "SL001", "CLOSE001"]
        seed_candles(mgr)
        await mgr.on_signal(make_signal("BUY"))
        assert SYMBOL in mgr.positions

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.close_all_positions(reason="EOD_CLOSE")

        assert SYMBOL not in mgr.positions

    @pytest.mark.asyncio
    async def test_close_all_empty_positions_does_not_crash(self, mock_bus):
        mgr, _ = make_manager()
        await mgr.close_all_positions(reason="SHUTDOWN")

    @pytest.mark.asyncio
    async def test_close_nse_only_leaves_mcx(self, mock_bus):
        """Exchange filter: close_all_positions(exchange='NSE') must skip MCX."""
        mgr, kite = make_manager()
        # Manually inject positions to avoid ATR/sizing dependencies
        mgr.positions[SYMBOL]       = {"order_id": "A", "sl_order_id": None,
                                        "direction": "BUY", "entry_price": ENTRY,
                                        "quantity": 1, "entry_time": datetime.now(IST),
                                        "strategy": "test"}
        mgr.positions["MCX:X"]      = {"order_id": "B", "sl_order_id": None,
                                        "direction": "BUY", "entry_price": 5_000,
                                        "quantity": 1, "entry_time": datetime.now(IST),
                                        "strategy": "test"}
        mgr.stops.set_stops(SYMBOL,    ENTRY, "BUY", ATR)
        mgr.stops.set_stops("MCX:X",   5_000, "BUY", 30)
        kite.place_order.side_effect = ["CLOSE_NSE"]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.close_all_positions(reason="NSE_EOD", exchange="NSE")

        assert SYMBOL    not in mgr.positions  # NSE closed
        assert "MCX:X"   in  mgr.positions     # MCX untouched
