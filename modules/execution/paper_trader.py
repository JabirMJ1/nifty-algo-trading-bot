"""Simulated execution engine for paper trading."""

import random
import uuid
from typing import Any, Dict, List, Optional

from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger, get_trade_logger
from modules.execution.order_models import Order, OrderSide, OrderStatus, OrderType, Trade
from modules.risk.circuit_breaker import CircuitBreaker
from modules.risk.position_sizer import PositionSizer, calculate_atr
from modules.risk.stop_manager import StopManager
from modules.sensory.market_hours import now_ist

log = get_logger("paper_trader")
trade_log = get_trade_logger()


class PaperTrader:
    """Simulated execution engine — fills at next candle open with slippage."""

    def __init__(
        self,
        position_sizer: PositionSizer,
        stop_manager: StopManager,
        circuit_breaker: CircuitBreaker,
    ):
        self.sizer = position_sizer
        self.stops = stop_manager
        self.breaker = circuit_breaker

        self.balance = float(settings.CAPITAL)
        self.positions: Dict[str, Dict[str, Any]] = {}  # symbol -> position
        self.pending_signals: List[Dict[str, Any]] = []
        self.trade_history: List[Trade] = []
        self._candle_history: Dict[str, List[Dict[str, Any]]] = {}

    async def on_signal(self, signal_data: Dict[str, Any]):
        """Queue a signal for execution on next candle."""
        if not self.breaker.can_trade():
            log.warning("Circuit breaker active. Signal rejected.")
            return

        symbol = signal_data["instrument"]

        # Don't open a position in the same direction if already in one
        if symbol in self.positions:
            existing = self.positions[symbol]
            if existing["direction"] == signal_data["direction"]:
                log.info(f"Already in {signal_data['direction']} for {symbol}. Skipping.")
                return

            # Opposite signal — close existing position first
            log.info(f"Opposite signal for {symbol}. Closing existing position.")
            await self._close_position(symbol, signal_data["price"], "SIGNAL_REVERSAL")

        self.pending_signals.append(signal_data)
        log.info(f"Signal queued: {signal_data['direction']} {symbol}")

    async def on_candle(self, candle_data: Dict[str, Any]):
        """Execute pending signals at candle open + check stops."""
        symbol = candle_data["symbol"]

        # Store candle history for ATR calculation
        if symbol not in self._candle_history:
            self._candle_history[symbol] = []
        self._candle_history[symbol].append(candle_data)
        # Keep last 100 candles
        self._candle_history[symbol] = self._candle_history[symbol][-100:]

        # Execute pending signals for this symbol
        for signal in list(self.pending_signals):
            if signal["instrument"] == symbol:
                await self._execute_signal(signal, candle_data)
                self.pending_signals.remove(signal)

        # Check stops for open positions
        if symbol in self.positions:
            current_price = candle_data["close"]
            exit_reason = self.stops.check_stops(symbol, current_price)
            if exit_reason:
                await self._close_position(symbol, current_price, exit_reason)

    async def on_tick(self, tick_data: Dict[str, Any]):
        """Check stops on every tick for tighter risk management."""
        symbol = tick_data["symbol"]
        if symbol in self.positions:
            current_price = tick_data["last_price"]
            exit_reason = self.stops.check_stops(symbol, current_price)
            if exit_reason:
                await self._close_position(symbol, current_price, exit_reason)

    async def _execute_signal(self, signal: Dict[str, Any], candle: Dict[str, Any]):
        """Execute a signal — simulate fill at candle open with slippage."""
        symbol = signal["instrument"]
        direction = signal["direction"]
        base_price = candle["open"]

        # Simulate slippage
        tick_size = 0.05  # Nifty tick size
        slippage = random.randint(-settings.SLIPPAGE_TICKS, settings.SLIPPAGE_TICKS)
        fill_price = base_price + (slippage * tick_size)

        # Calculate ATR for sizing and stops
        candles = self._candle_history.get(symbol, [])
        atr = calculate_atr(candles)

        if atr <= 0:
            log.warning(f"ATR is zero for {symbol}. Cannot execute.")
            return

        # Margin already locked in existing open positions
        deployed_margin = sum(
            pos["entry_price"] * pos["quantity"] * (settings.MARGIN_PCT / 100)
            for pos in self.positions.values()
        )

        # Position sizing
        quantity = self.sizer.calculate_size(
            atr=atr,
            current_open_positions=len(self.positions),
            price=signal.get("price", base_price),
            deployed_margin=deployed_margin,
        )
        if quantity <= 0:
            log.warning(f"Position sizer returned 0 for {symbol}. Skipping.")
            return

        # Create position
        order_id = str(uuid.uuid4())[:8]
        self.positions[symbol] = {
            "order_id": order_id,
            "direction": direction,
            "entry_price": fill_price,
            "quantity": quantity,
            "entry_time": now_ist(),
            "strategy": signal.get("strategy_name", "unknown"),
        }

        # Set stop-loss and target
        self.stops.set_stops(symbol, fill_price, direction, atr)

        stop_level = self.stops.get_stop_level(symbol)
        log.info(
            f"PAPER FILL | {direction} {quantity}x {symbol} @ {fill_price:.2f} | "
            f"SL={stop_level.stop_loss:.2f} TGT={stop_level.target:.2f} | "
            f"order_id={order_id}"
        )
        trade_log.info(
            f"OPEN | {direction} {quantity}x {symbol} @ {fill_price:.2f} | "
            f"SL={stop_level.stop_loss:.2f} TGT={stop_level.target:.2f}"
        )

        # Publish trade event
        await event_bus.publish(
            EventType.TRADE,
            {
                "type": "ENTRY",
                "order_id": order_id,
                "instrument": symbol,
                "direction": direction,
                "price": fill_price,
                "quantity": quantity,
                "strategy": signal.get("strategy_name"),
                "stop_loss": stop_level.stop_loss,
                "target": stop_level.target,
            },
        )

    async def _close_position(self, symbol: str, exit_price: float, reason: str):
        """Close an open position."""
        pos = self.positions.pop(symbol, None)
        if not pos:
            return

        # Calculate PnL
        if pos["direction"] == "BUY":
            gross_pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
        else:
            gross_pnl = (pos["entry_price"] - exit_price) * pos["quantity"]

        # Calculate costs
        costs = self._calculate_costs(pos["entry_price"], exit_price, pos["quantity"])
        net_pnl = gross_pnl - costs

        # Record trade
        trade = Trade(
            trade_id=pos["order_id"],
            instrument=symbol,
            direction=pos["direction"],
            entry_price=pos["entry_price"],
            exit_price=exit_price,
            quantity=pos["quantity"],
            pnl=net_pnl,
            entry_time=pos["entry_time"],
            exit_time=now_ist(),
            strategy=pos["strategy"],
            exit_reason=reason,
            costs=costs,
        )
        self.trade_history.append(trade)

        # Update balance and circuit breaker
        self.balance += net_pnl
        self.sizer.update_capital(self.balance)
        await self.breaker.update_pnl(net_pnl)

        # Remove stops
        self.stops.remove_stops(symbol)

        log.info(
            f"PAPER CLOSE | {pos['direction']} {pos['quantity']}x {symbol} | "
            f"entry={pos['entry_price']:.2f} exit={exit_price:.2f} | "
            f"PnL={net_pnl:.2f} ({reason}) | balance={self.balance:.2f}"
        )
        trade_log.info(
            f"CLOSE | {pos['direction']} {pos['quantity']}x {symbol} | "
            f"PnL={net_pnl:.2f} | reason={reason}"
        )

        # Publish trade event
        await event_bus.publish(
            EventType.TRADE,
            {
                "type": "EXIT",
                "trade": trade.to_event_data(),
                "reason": reason,
                "balance": self.balance,
            },
        )

        # Alert
        emoji = "profit" if net_pnl > 0 else "loss"
        await event_bus.publish(
            EventType.ALERT,
            {
                "type": "TRADE_CLOSED",
                "message": (
                    f"Trade Closed ({emoji.upper()})\n"
                    f"{pos['direction']} {symbol}\n"
                    f"Entry: {pos['entry_price']:.2f}\n"
                    f"Exit: {exit_price:.2f}\n"
                    f"PnL: {net_pnl:.2f}\n"
                    f"Reason: {reason}\n"
                    f"Balance: {self.balance:.2f}"
                ),
            },
        )

    def _calculate_costs(
        self, entry_price: float, exit_price: float, quantity: int
    ) -> float:
        """Calculate transaction costs (brokerage + STT + exchange charges)."""
        turnover = (entry_price + exit_price) * quantity
        brokerage = settings.BROKERAGE_PER_ORDER * 2  # entry + exit
        stt = turnover * (settings.STT_PCT / 100)
        exchange = turnover * (settings.EXCHANGE_CHARGES_PCT / 100)
        return brokerage + stt + exchange

    async def close_all_positions(self, reason: str = "EOD_CLOSE", exchange: str = None):
        """Close all open positions, optionally filtered by exchange (e.g. 'NSE' or 'MCX')."""
        for symbol in list(self.positions.keys()):
            if exchange and not symbol.startswith(f"{exchange}:"):
                continue
            # Use last known price from candle history
            candles = self._candle_history.get(symbol, [])
            if candles:
                price = candles[-1]["close"]
            else:
                price = self.positions[symbol]["entry_price"]
            await self._close_position(symbol, price, reason)

    async def on_kill_switch(self, data: Dict[str, Any]):
        """Force-close all positions when circuit breaker fires."""
        log.warning(f"KILL_SWITCH received — force-closing all positions | reason={data}")
        await self.close_all_positions("KILL_SWITCH")

    def start(self):
        """Subscribe to relevant events."""
        event_bus.subscribe(EventType.SIGNAL, self.on_signal)
        event_bus.subscribe(EventType.CANDLE, self.on_candle)
        event_bus.subscribe(EventType.TICK, self.on_tick)
        event_bus.subscribe(EventType.KILL_SWITCH, self.on_kill_switch)
        log.info(f"Paper trader started | balance={self.balance:.2f}")

    def get_status(self) -> Dict[str, Any]:
        return {
            "balance": self.balance,
            "open_positions": len(self.positions),
            "positions": self.positions,
            "total_trades": len(self.trade_history),
            "circuit_breaker": self.breaker.get_status(),
        }
