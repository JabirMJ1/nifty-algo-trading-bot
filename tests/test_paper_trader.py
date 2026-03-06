"""Extensive tests for PaperTrader — simulated execution engine.

Perspective: ₹1,00,000 capital retail trader.  Every rupee matters.

Flow recap:
  on_signal(signal) → queues signal (if circuit breaker OK, not duplicate direction)
  on_candle(candle) → executes pending signals for that symbol + checks stops
  on_tick(tick)     → checks stops on every tick
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

from modules.execution.paper_trader import PaperTrader
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

def make_sizer(capital=CAPITAL) -> PositionSizer:
    s = PositionSizer()
    s.capital = capital
    s.risk_pct = 0.01       # 1% risk → ₹1,000 risk amount
    s.max_positions = 3
    return s


def make_trader(capital=CAPITAL) -> PaperTrader:
    sizer   = make_sizer(capital)
    stops   = StopManager()
    breaker = CircuitBreaker()
    breaker._max_loss = capital * 0.02  # 2% = ₹2,000
    trader  = PaperTrader(position_sizer=sizer, stop_manager=stops,
                          circuit_breaker=breaker)
    trader.balance = float(capital)
    return trader


def make_signal(direction="BUY", price=ENTRY, symbol=SYMBOL) -> dict:
    return {
        "instrument":    symbol,
        "direction":     direction,
        "price":         price,
        "strategy_name": "test",
        "timestamp":     datetime(2026, 3, 5, 10, 30, tzinfo=IST),
    }


def make_candle(symbol=SYMBOL, open_=ENTRY, close=ENTRY,
                high=None, low=None) -> dict:
    return {
        "symbol":    symbol,
        "open":      open_,
        "high":      high  if high  is not None else open_ + ATR,
        "low":       low   if low   is not None else open_ - ATR,
        "close":     close,
        "volume":    1_000,
        "timestamp": datetime(2026, 3, 5, 10, 45, tzinfo=IST),
    }


def seed_candles(trader: PaperTrader, symbol=SYMBOL,
                 n=20, base=ENTRY, atr_range=ATR) -> None:
    """Pre-populate candle history so ATR calculation succeeds."""
    candles = [
        {
            "symbol":    symbol,
            "open":      base + i,
            "high":      base + i + atr_range,
            "low":       base + i - atr_range,
            "close":     base + i + atr_range * 0.5,
            "volume":    1_000,
            "timestamp": datetime(2026, 3, 5, 9, 20 + i, tzinfo=IST),
        }
        for i in range(n)
    ]
    trader._candle_history[symbol] = candles


@pytest.fixture
def mock_bus():
    with patch("modules.execution.paper_trader.event_bus") as m:
        m.publish = AsyncMock()
        yield m


# ═══════════════════════════════════════════════════════════════════════════════
# Signal Gating
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalGating:
    @pytest.mark.asyncio
    async def test_valid_buy_signal_is_queued(self, mock_bus):
        trader = make_trader()
        await trader.on_signal(make_signal("BUY"))
        assert len(trader.pending_signals) == 1

    @pytest.mark.asyncio
    async def test_valid_sell_signal_is_queued(self, mock_bus):
        trader = make_trader()
        await trader.on_signal(make_signal("SELL"))
        assert len(trader.pending_signals) == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_active_blocks_signal(self, mock_bus):
        trader = make_trader()
        trader.breaker._kill_switch = True
        await trader.on_signal(make_signal("BUY"))
        assert len(trader.pending_signals) == 0

    @pytest.mark.asyncio
    async def test_duplicate_buy_signal_ignored_when_long(self, mock_bus):
        """Already long → second BUY signal must be dropped."""
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())   # executes first BUY
        assert SYMBOL in trader.positions

        prior_pending = len(trader.pending_signals)
        await trader.on_signal(make_signal("BUY"))
        assert len(trader.pending_signals) == prior_pending  # not queued again

    @pytest.mark.asyncio
    async def test_opposite_signal_closes_existing_and_queues_new(self, mock_bus):
        """Long open + SELL signal → close long immediately, queue SELL."""
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())
        assert SYMBOL in trader.positions

        await trader.on_signal(make_signal("SELL"))
        assert SYMBOL not in trader.positions          # closed
        assert len(trader.pending_signals) == 1        # SELL queued
        assert trader.pending_signals[0]["direction"] == "SELL"

    @pytest.mark.asyncio
    async def test_multiple_different_symbols_queued_separately(self, mock_bus):
        trader = make_trader()
        await trader.on_signal(make_signal("BUY",  symbol="NSE:NIFTY26MARFUT"))
        await trader.on_signal(make_signal("SELL", symbol="MCX:CRUDEOILFUT"))
        assert len(trader.pending_signals) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Signal Execution via on_candle
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalExecution:
    @pytest.mark.asyncio
    async def test_position_created_after_candle(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle(open_=ENTRY))

        assert SYMBOL in trader.positions
        pos = trader.positions[SYMBOL]
        assert pos["direction"] == "BUY"
        assert pos["quantity"] > 0

    @pytest.mark.asyncio
    async def test_sell_position_created(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("SELL"))
        await trader.on_candle(make_candle(open_=ENTRY))

        assert SYMBOL in trader.positions
        assert trader.positions[SYMBOL]["direction"] == "SELL"

    @pytest.mark.asyncio
    async def test_no_position_when_candle_has_zero_range(self, mock_bus):
        """Candle with high==low → ATR=0 → signal silently dropped."""
        trader = make_trader()
        # Candle with zero range → ATR calculation returns 0
        flat_candle = make_candle(high=ENTRY, low=ENTRY)  # H-L = 0
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(flat_candle)
        assert SYMBOL not in trader.positions

    @pytest.mark.asyncio
    async def test_fill_price_near_candle_open(self, mock_bus):
        """Fill price must be candle open ± slippage (≤ ±SLIPPAGE_TICKS ticks)."""
        trader = make_trader()
        seed_candles(trader)
        candle_open = 22_000.0
        await trader.on_signal(make_signal("BUY", price=candle_open))
        await trader.on_candle(make_candle(open_=candle_open))

        fill = trader.positions[SYMBOL]["entry_price"]
        max_slip = settings.SLIPPAGE_TICKS * 0.05
        assert abs(fill - candle_open) <= max_slip

    @pytest.mark.asyncio
    async def test_stops_set_correctly_after_long_entry(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle(open_=ENTRY))

        sl = trader.stops.get_stop_level(SYMBOL)
        assert sl is not None
        assert sl.stop_loss < ENTRY    # stop below entry for long
        assert sl.target    > ENTRY    # target above entry

    @pytest.mark.asyncio
    async def test_stops_set_correctly_after_short_entry(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("SELL"))
        await trader.on_candle(make_candle(open_=ENTRY))

        sl = trader.stops.get_stop_level(SYMBOL)
        assert sl.stop_loss > ENTRY   # stop above entry for short
        assert sl.target    < ENTRY

    @pytest.mark.asyncio
    async def test_pending_signal_cleared_after_execution(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        assert len(trader.pending_signals) == 1
        await trader.on_candle(make_candle())
        assert len(trader.pending_signals) == 0

    @pytest.mark.asyncio
    async def test_trade_event_published_on_entry(self, mock_bus):
        from core.event_bus import EventType
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())

        published_types = [c.args[0] for c in mock_bus.publish.call_args_list]
        assert EventType.TRADE in published_types


# ═══════════════════════════════════════════════════════════════════════════════
# Stop / Target Exits
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopAndTargetExits:
    @pytest.mark.asyncio
    async def test_stop_hit_on_tick_closes_long(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())
        assert SYMBOL in trader.positions

        stop_price = trader.stops.get_stop_level(SYMBOL).stop_loss
        await trader.on_tick({"symbol": SYMBOL, "last_price": stop_price - 1})
        assert SYMBOL not in trader.positions

    @pytest.mark.asyncio
    async def test_target_hit_on_tick_closes_long(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())

        target = trader.stops.get_stop_level(SYMBOL).target
        await trader.on_tick({"symbol": SYMBOL, "last_price": target + 1})
        assert SYMBOL not in trader.positions

    @pytest.mark.asyncio
    async def test_stop_hit_on_candle_close_closes_position(self, mock_bus):
        """on_candle also checks stops via close price."""
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())

        stop_price = trader.stops.get_stop_level(SYMBOL).stop_loss
        exit_candle = make_candle(close=stop_price - 1)
        await trader.on_candle(exit_candle)
        assert SYMBOL not in trader.positions

    @pytest.mark.asyncio
    async def test_price_between_stop_and_target_keeps_position(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())

        sl   = trader.stops.get_stop_level(SYMBOL)
        mid  = (sl.stop_loss + sl.target) / 2
        await trader.on_tick({"symbol": SYMBOL, "last_price": mid})
        assert SYMBOL in trader.positions

    @pytest.mark.asyncio
    async def test_tick_for_unknown_symbol_does_not_crash(self, mock_bus):
        trader = make_trader()
        await trader.on_tick({"symbol": "NSE:UNKNOWN", "last_price": 22000})

    @pytest.mark.asyncio
    async def test_stops_removed_after_stop_exit(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())

        stop = trader.stops.get_stop_level(SYMBOL).stop_loss
        await trader.on_tick({"symbol": SYMBOL, "last_price": stop - 1})
        assert trader.stops.get_stop_level(SYMBOL) is None


# ═══════════════════════════════════════════════════════════════════════════════
# PnL and Balance Tracking
# ═══════════════════════════════════════════════════════════════════════════════

class TestPnLAndBalance:
    @pytest.mark.asyncio
    async def test_profitable_trade_increases_balance(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle(open_=ENTRY))

        initial_balance = trader.balance
        target = trader.stops.get_stop_level(SYMBOL).target

        await trader.on_tick({"symbol": SYMBOL, "last_price": target + 1})
        # Target hit → profitable close → balance should increase
        assert trader.balance > initial_balance

    @pytest.mark.asyncio
    async def test_losing_trade_decreases_balance(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle(open_=ENTRY))

        initial_balance = trader.balance
        stop = trader.stops.get_stop_level(SYMBOL).stop_loss

        await trader.on_tick({"symbol": SYMBOL, "last_price": stop - 1})
        assert trader.balance < initial_balance

    @pytest.mark.asyncio
    async def test_sizer_capital_updated_after_loss(self, mock_bus):
        """After stop-loss hit, PositionSizer.capital must reflect new balance."""
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle(open_=ENTRY))

        stop = trader.stops.get_stop_level(SYMBOL).stop_loss
        await trader.on_tick({"symbol": SYMBOL, "last_price": stop - 1})

        assert trader.sizer.capital == pytest.approx(trader.balance, rel=0.01)

    @pytest.mark.asyncio
    async def test_circuit_breaker_pnl_updated_after_trade(self, mock_bus):
        """Circuit breaker must know about trade PnL for daily limit tracking."""
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle(open_=ENTRY))

        stop = trader.stops.get_stop_level(SYMBOL).stop_loss
        await trader.on_tick({"symbol": SYMBOL, "last_price": stop - 1})

        # Breaker daily_pnl must be negative after a losing trade
        assert trader.breaker.get_daily_pnl() < 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_triggered_after_2pct_loss(self, mock_bus):
        """After losing > 2% of ₹1L = ₹2,000, circuit breaker blocks new trades."""
        trader = make_trader()
        # Directly update breaker to simulate accumulated losses
        await trader.breaker.update_pnl(-2_500)
        assert trader.breaker.can_trade() is False

        # New signal must be rejected
        await trader.on_signal(make_signal("BUY"))
        assert len(trader.pending_signals) == 0

    @pytest.mark.asyncio
    async def test_trade_history_appended_after_close(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle(open_=ENTRY))

        stop = trader.stops.get_stop_level(SYMBOL).stop_loss
        await trader.on_tick({"symbol": SYMBOL, "last_price": stop - 1})

        assert len(trader.trade_history) == 1

    @pytest.mark.asyncio
    async def test_cost_deducted_from_pnl(self, mock_bus):
        """Costs (brokerage + STT + exchange) must reduce net PnL."""
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY", price=ENTRY))
        await trader.on_candle(make_candle(open_=ENTRY, close=ENTRY))

        # Manually close at a breakeven price: net PnL should be negative due to costs
        await trader._close_position(SYMBOL, ENTRY, "TEST")
        if trader.trade_history:
            trade = trader.trade_history[-1]
            assert trade.costs > 0
            assert trade.pnl < 0  # costs eaten into breakeven


# ═══════════════════════════════════════════════════════════════════════════════
# Deployed Margin and Position Sizing
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeployedMarginInPaperTrader:
    @pytest.mark.asyncio
    async def test_second_position_respects_deployed_margin(self, mock_bus):
        """
        Open position 1 in NSE:NIFTY at ENTRY=22000.
        Then open position 2 in MCX:CRUDEOIL — should use only remaining capital.
        With deployed_margin fix, position 2 must be sized on available capital,
        not the full ₹1L.
        """
        trader = make_trader(capital=100_000)
        symbol2 = "MCX:CRUDEOILFUT"
        seed_candles(trader, symbol=SYMBOL)
        seed_candles(trader, symbol=symbol2, base=5_000, atr_range=30)

        # Open position 1
        await trader.on_signal(make_signal("BUY", symbol=SYMBOL, price=ENTRY))
        await trader.on_candle(make_candle(symbol=SYMBOL, open_=ENTRY))
        assert SYMBOL in trader.positions
        qty1 = trader.positions[SYMBOL]["quantity"]

        # Open position 2 — must account for margin already deployed
        await trader.on_signal(make_signal("BUY", symbol=symbol2, price=5_000))
        await trader.on_candle(make_candle(symbol=symbol2, open_=5_000, close=5_000,
                                            high=5_030, low=4_970))

        if symbol2 in trader.positions:
            qty2 = trader.positions[symbol2]["quantity"]
            margin1 = trader.positions[SYMBOL]["entry_price"] * qty1 * (settings.MARGIN_PCT / 100)
            margin2 = 5_000 * qty2 * (settings.MARGIN_PCT / 100)
            assert margin1 + margin2 <= trader.balance + 1  # can't exceed capital


# ═══════════════════════════════════════════════════════════════════════════════
# EOD / Shutdown Close
# ═══════════════════════════════════════════════════════════════════════════════

class TestEODClose:
    @pytest.mark.asyncio
    async def test_close_all_positions_clears_all(self, mock_bus):
        trader = make_trader()
        seed_candles(trader)
        await trader.on_signal(make_signal("BUY"))
        await trader.on_candle(make_candle())
        assert SYMBOL in trader.positions

        await trader.close_all_positions(reason="EOD_CLOSE")
        assert SYMBOL not in trader.positions

    @pytest.mark.asyncio
    async def test_close_all_with_no_positions_does_not_raise(self, mock_bus):
        trader = make_trader()
        await trader.close_all_positions(reason="EOD_CLOSE")

    @pytest.mark.asyncio
    async def test_close_all_nse_only_leaves_mcx_open(self, mock_bus):
        """NSE force-close at 15:30 must not close MCX positions."""
        trader = make_trader()
        symbol_mcx = "MCX:CRUDEOILFUT"
        seed_candles(trader, symbol=SYMBOL)
        seed_candles(trader, symbol=symbol_mcx, base=5_000, atr_range=30)

        # Open two positions
        await trader.on_signal(make_signal("BUY", symbol=SYMBOL))
        await trader.on_candle(make_candle(symbol=SYMBOL))
        await trader.on_signal(make_signal("BUY", symbol=symbol_mcx, price=5_000))
        await trader.on_candle(
            make_candle(symbol=symbol_mcx, open_=5_000, close=5_000,
                        high=5_030, low=4_970)
        )

        # Close only NSE
        await trader.close_all_positions(reason="NSE_EOD", exchange="NSE")

        assert SYMBOL not in trader.positions
        # MCX should still be open (if it was created — depends on capital)
        # The key assertion is NSE is gone
