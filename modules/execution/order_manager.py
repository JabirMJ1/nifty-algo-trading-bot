"""Real order execution via Kite Connect API."""

import asyncio
import uuid
from typing import Any, Dict, Optional

from kiteconnect import KiteConnect
from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger, get_trade_logger
from modules.execution.order_models import Order, OrderSide, OrderStatus, OrderType, Trade
from modules.risk.circuit_breaker import CircuitBreaker
from modules.risk.position_sizer import PositionSizer, calculate_atr
from modules.risk.stop_manager import StopManager
from modules.sensory.market_hours import now_ist

log = get_logger("order_manager")
trade_log = get_trade_logger()

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1  # seconds


class OrderManager:
    """Real execution engine using Kite Connect."""

    def __init__(
        self,
        kite: KiteConnect,
        position_sizer: PositionSizer,
        stop_manager: StopManager,
        circuit_breaker: CircuitBreaker,
    ):
        self.kite = kite
        self.sizer = position_sizer
        self.stops = stop_manager
        self.breaker = circuit_breaker

        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trade_history: list = []
        self._candle_history: Dict[str, list] = {}

    async def on_signal(self, signal_data: Dict[str, Any]):
        """Process a trading signal."""
        if not self.breaker.can_trade():
            log.warning("Circuit breaker active. Signal rejected.")
            return

        symbol = signal_data["instrument"]

        # Close opposite position if exists
        if symbol in self.positions:
            existing = self.positions[symbol]
            if existing["direction"] == signal_data["direction"]:
                log.info(f"Already in {signal_data['direction']} for {symbol}.")
                return
            await self._close_position_market(symbol, "SIGNAL_REVERSAL")

        # Calculate ATR and sizing
        candles = self._candle_history.get(symbol, [])
        atr = calculate_atr(candles)
        if atr <= 0:
            log.warning(f"ATR is zero for {symbol}. Skipping.")
            return

        quantity = self.sizer.calculate_size(
            atr=atr, current_open_positions=len(self.positions)
        )
        if quantity <= 0:
            return

        # Place order
        exchange, tradingsymbol = symbol.split(":")
        side = signal_data["direction"]
        transaction_type = (
            self.kite.TRANSACTION_TYPE_BUY
            if side == "BUY"
            else self.kite.TRANSACTION_TYPE_SELL
        )

        order_id = await self._place_order_with_retry(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=self.kite.ORDER_TYPE_MARKET,
        )

        if order_id:
            fill_price = signal_data["price"]  # approximate; update on fill
            self.stops.set_stops(symbol, fill_price, side, atr)
            stop_level = self.stops.get_stop_level(symbol)

            # Place exchange-level SL-M order as a hard stop (protects if bot crashes)
            sl_order_id = None
            if stop_level:
                sl_order_id = await self._place_sl_order(
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    direction=side,
                    quantity=quantity,
                    stop_price=stop_level.stop_loss,
                )

            self.positions[symbol] = {
                "order_id": order_id,
                "sl_order_id": sl_order_id,
                "direction": side,
                "entry_price": fill_price,
                "quantity": quantity,
                "entry_time": now_ist(),
                "strategy": signal_data.get("strategy_name", "unknown"),
            }

            log.info(
                f"ORDER PLACED | {side} {quantity}x {symbol} | "
                f"order_id={order_id} sl_order_id={sl_order_id}"
            )
            trade_log.info(f"OPEN | {side} {quantity}x {symbol} | order_id={order_id}")

            await event_bus.publish(
                EventType.TRADE,
                {
                    "type": "ENTRY",
                    "order_id": order_id,
                    "instrument": symbol,
                    "direction": side,
                    "price": fill_price,
                    "quantity": quantity,
                    "strategy": signal_data.get("strategy_name"),
                },
            )

    async def on_candle(self, candle_data: Dict[str, Any]):
        """Store candle history and check stops."""
        symbol = candle_data["symbol"]
        if symbol not in self._candle_history:
            self._candle_history[symbol] = []
        self._candle_history[symbol].append(candle_data)
        self._candle_history[symbol] = self._candle_history[symbol][-100:]

    async def on_tick(self, tick_data: Dict[str, Any]):
        """Check stops on every tick."""
        symbol = tick_data["symbol"]
        if symbol in self.positions:
            current_price = tick_data["last_price"]
            exit_reason = self.stops.check_stops(symbol, current_price)
            if exit_reason:
                await self._close_position_market(symbol, exit_reason)

    async def _place_order_with_retry(
        self,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        price: float = 0,
        trigger_price: float = 0,
    ) -> Optional[str]:
        """Place order with retry logic (3 retries, exponential backoff)."""
        for attempt in range(MAX_RETRIES):
            try:
                log.info(
                    f"Placing order (attempt {attempt + 1}): "
                    f"{transaction_type} {quantity}x {exchange}:{tradingsymbol}"
                )
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    product=self.kite.PRODUCT_MIS,
                    order_type=order_type,
                    price=price if price else None,
                    trigger_price=trigger_price if trigger_price else None,
                )
                log.info(f"Order placed successfully: {order_id}")
                return order_id
            except Exception as e:
                log.error(f"Order placement failed (attempt {attempt + 1}): {e}")
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF_BASE * (2**attempt)
                    await asyncio.sleep(wait)

        log.error("All order placement retries exhausted.")
        await event_bus.publish(
            EventType.ALERT,
            {"type": "ORDER_FAILED", "message": f"Failed to place order for {exchange}:{tradingsymbol}"},
        )
        return None

    async def _place_sl_order(
        self,
        exchange: str,
        tradingsymbol: str,
        direction: str,
        quantity: int,
        stop_price: float,
    ) -> Optional[str]:
        """Place an exchange-level SL-M closing order (protects if bot goes offline).

        This is a pending SELL (for BUY position) or BUY (for SELL position) order
        that fires at the exchange when `stop_price` is touched — even if the bot crashes.
        """
        close_side = (
            self.kite.TRANSACTION_TYPE_SELL
            if direction == "BUY"
            else self.kite.TRANSACTION_TYPE_BUY
        )
        try:
            sl_order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=close_side,
                quantity=quantity,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_SL_M,
                trigger_price=round(stop_price, 2),
            )
            log.info(
                f"SL-M order placed | {exchange}:{tradingsymbol} | "
                f"trigger={stop_price:.2f} | sl_order_id={sl_order_id}"
            )
            return sl_order_id
        except Exception as e:
            log.error(f"Failed to place SL-M order for {tradingsymbol}: {e}")
            return None

    async def _cancel_sl_order(self, sl_order_id: Optional[str]):
        """Cancel a pending SL-M order (called when closing position normally)."""
        if not sl_order_id:
            return
        try:
            self.kite.cancel_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=sl_order_id,
            )
            log.info(f"SL-M order cancelled | sl_order_id={sl_order_id}")
        except Exception as e:
            log.warning(f"Failed to cancel SL-M order {sl_order_id}: {e}")

    async def _close_position_market(self, symbol: str, reason: str):
        """Close position with a market order."""
        pos = self.positions.get(symbol)
        if not pos:
            return

        # Cancel the standing SL-M order before placing close (avoid double-fill)
        await self._cancel_sl_order(pos.get("sl_order_id"))

        exchange, tradingsymbol = symbol.split(":")
        close_side = (
            self.kite.TRANSACTION_TYPE_SELL
            if pos["direction"] == "BUY"
            else self.kite.TRANSACTION_TYPE_BUY
        )

        order_id = await self._place_order_with_retry(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=close_side,
            quantity=pos["quantity"],
            order_type=self.kite.ORDER_TYPE_MARKET,
        )

        # Always clean up internal state — position may already be closed by exchange SL-M
        candles = self._candle_history.get(symbol, [])
        exit_price = candles[-1]["close"] if candles else pos["entry_price"]

        if pos["direction"] == "BUY":
            pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
        else:
            pnl = (pos["entry_price"] - exit_price) * pos["quantity"]

        del self.positions[symbol]
        self.stops.remove_stops(symbol)
        await self.breaker.update_pnl(pnl)

        if order_id:
            log.info(f"POSITION CLOSED | {symbol} | PnL={pnl:.2f} | reason={reason}")
        else:
            log.warning(
                f"Close order failed for {symbol} — position may be closed by SL-M already | "
                f"estimated PnL={pnl:.2f} | reason={reason}"
            )
        trade_log.info(f"CLOSE | {symbol} | PnL={pnl:.2f} | reason={reason}")

        await event_bus.publish(
            EventType.TRADE,
            {"type": "EXIT", "instrument": symbol, "pnl": pnl, "reason": reason},
        )

    async def close_all_positions(self, reason: str = "EOD_CLOSE"):
        """Close all open positions."""
        for symbol in list(self.positions.keys()):
            await self._close_position_market(symbol, reason)

    def start(self):
        """Subscribe to events."""
        event_bus.subscribe(EventType.SIGNAL, self.on_signal)
        event_bus.subscribe(EventType.CANDLE, self.on_candle)
        event_bus.subscribe(EventType.TICK, self.on_tick)
        log.info("Order manager started (LIVE MODE)")
