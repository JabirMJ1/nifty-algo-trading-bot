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
SL_VERIFY_WAIT_SECS = 2  # seconds to wait before verifying SL-M acceptance

# SL-M order is live and protecting the position
_SL_ACTIVE = {"OPEN", "TRIGGER PENDING"}
# SL-M already fired (market gapped through stop during placement window) — position closed by exchange
_SL_COMPLETE = {"COMPLETE"}
# Transient: exchange still processing — recheck rather than retry the order
_SL_TRANSIENT = {"PUT ORDER REQ RECEIVED", "VALIDATION PENDING"}
# Definitive failures — retry the order placement
_SL_FAILED = {"REJECTED", "CANCELLED"}


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
        self.balance: float = self._resolve_starting_capital()

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

        deployed_margin = sum(
            pos["entry_price"] * pos["quantity"] * (settings.MARGIN_PCT / 100)
            for pos in self.positions.values()
        )

        quantity = self.sizer.calculate_size(
            atr=atr,
            current_open_positions=len(self.positions),
            price=signal_data.get("price", 0.0),
            deployed_margin=deployed_margin,
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
            fill_price = await self._get_fill_price(order_id, signal_data["price"])
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
                    market_protection=1.0 if order_type == self.kite.ORDER_TYPE_MARKET else None,
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

    async def _get_order_status(self, order_id: str) -> Optional[str]:
        """Fetch the latest status of an order from Kite."""
        try:
            history = self.kite.order_history(order_id)
            if history:
                return history[-1]["status"]
        except Exception as e:
            log.warning(f"Could not fetch status for order {order_id}: {e}")
        return None

    async def _get_fill_price(self, order_id: str, fallback: float) -> float:
        """Fetch actual average fill price from Kite order history.

        Polls up to 3 times (1s apart) waiting for COMPLETE status.
        Falls back to the provided price if unavailable.
        """
        try:
            for _ in range(3):
                history = self.kite.order_history(order_id)
                for update in reversed(history):
                    if update.get("status") == "COMPLETE" and update.get("average_price", 0) > 0:
                        fill = float(update["average_price"])
                        log.info(f"Actual fill price for {order_id}: {fill:.2f} (fallback was {fallback:.2f})")
                        return fill
                await asyncio.sleep(1)
        except Exception as e:
            log.warning(f"Could not fetch fill price for order {order_id}: {e}")
        log.warning(f"Using fallback price {fallback:.2f} for order {order_id}")
        return fallback

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
        Verifies the order is accepted by the exchange after placement and retries once
        on definitive rejection, firing a Telegram alert if it ultimately fails.
        """
        close_side = (
            self.kite.TRANSACTION_TYPE_SELL
            if direction == "BUY"
            else self.kite.TRANSACTION_TYPE_BUY
        )
        symbol = f"{exchange}:{tradingsymbol}"
        prev_sl_order_id: Optional[str] = None

        for attempt in range(2):
            # Cancel any previous failed attempt before retrying (avoid orphaned orders)
            if prev_sl_order_id:
                await self._cancel_sl_order(prev_sl_order_id)

            try:
                sl_order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=close_side,
                    quantity=quantity,
                    product=self.kite.PRODUCT_MIS,
                    order_type=self.kite.ORDER_TYPE_SLM,
                    trigger_price=round(stop_price, 2),
                )
            except Exception as e:
                log.error(f"SL-M placement exception (attempt {attempt + 1}) for {symbol}: {e}")
                if attempt == 0:
                    await asyncio.sleep(SL_VERIFY_WAIT_SECS)
                    continue
                break

            # Give exchange time to process, then verify acceptance
            await asyncio.sleep(SL_VERIFY_WAIT_SECS)
            status = await self._get_order_status(sl_order_id)

            if status in _SL_ACTIVE:
                log.info(
                    f"SL-M order placed | {symbol} | "
                    f"trigger={stop_price:.2f} | sl_order_id={sl_order_id} | status={status}"
                )
                return sl_order_id

            if status in _SL_COMPLETE:
                # Market gapped through stop during placement window — SL already executed
                log.warning(
                    f"SL-M order {sl_order_id} already COMPLETE for {symbol} "
                    f"(stop hit during placement window) | trigger={stop_price:.2f}"
                )
                return sl_order_id

            if status in _SL_TRANSIENT:
                # Exchange still processing — recheck once more after a short wait
                await asyncio.sleep(SL_VERIFY_WAIT_SECS)
                status = await self._get_order_status(sl_order_id)
                if status in _SL_ACTIVE | _SL_COMPLETE:
                    log.info(
                        f"SL-M order placed | {symbol} | "
                        f"trigger={stop_price:.2f} | sl_order_id={sl_order_id} | status={status}"
                    )
                    return sl_order_id

            if status is None:
                # Status check failed (network issue) — assume order is live rather than risk a duplicate
                log.warning(
                    f"SL-M status check failed for {sl_order_id} ({symbol}) — "
                    f"assuming order is live. Monitor manually."
                )
                return sl_order_id

            # Definitive failure (REJECTED/CANCELLED) or unexpected status — retry
            log.error(
                f"SL-M order {sl_order_id} status='{status}' (attempt {attempt + 1}) for {symbol}"
            )
            prev_sl_order_id = sl_order_id
            if attempt == 0:
                await asyncio.sleep(SL_VERIFY_WAIT_SECS)

        await event_bus.publish(
            EventType.ALERT,
            {
                "type": "SL_ORDER_FAILED",
                "message": f"SL-M order rejected by exchange for {symbol} — position unprotected! Stop={stop_price:.2f}",
            },
        )
        log.critical(f"SL-M order failed after 2 attempts for {symbol} — position has NO exchange-level stop protection!")
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

        # Fetch actual fill price; fall back to last candle close if unavailable
        candles = self._candle_history.get(symbol, [])
        fallback_price = candles[-1]["close"] if candles else pos["entry_price"]
        exit_price = (
            await self._get_fill_price(order_id, fallback_price)
            if order_id
            else fallback_price
        )

        if pos["direction"] == "BUY":
            gross_pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
        else:
            gross_pnl = (pos["entry_price"] - exit_price) * pos["quantity"]

        turnover = (pos["entry_price"] + exit_price) * pos["quantity"]
        costs = round(
            settings.BROKERAGE_PER_ORDER * 2
            + turnover * (settings.STT_PCT / 100)
            + turnover * (settings.EXCHANGE_CHARGES_PCT / 100),
            2,
        )
        pnl = gross_pnl - costs

        del self.positions[symbol]
        self.stops.remove_stops(symbol)
        self.balance += pnl
        self.sizer.update_capital(self.balance)
        await self.breaker.update_pnl(pnl)

        if order_id:
            log.info(f"POSITION CLOSED | {symbol} | gross={gross_pnl:.2f} costs={costs:.2f} net={pnl:.2f} | reason={reason}")
        else:
            log.warning(
                f"Close order failed for {symbol} — position may be closed by SL-M already | "
                f"estimated gross={gross_pnl:.2f} costs={costs:.2f} net={pnl:.2f} | reason={reason}"
            )
        trade_log.info(f"CLOSE | {symbol} | gross={gross_pnl:.2f} costs={costs:.2f} net={pnl:.2f} | reason={reason}")

        await event_bus.publish(
            EventType.TRADE,
            {
                "type": "EXIT",
                "instrument": symbol,
                "pnl": pnl,
                "reason": reason,
                "trade": {
                    "trade_id": pos["order_id"],
                    "exit_price": exit_price,
                    "gross_pnl": gross_pnl,
                    "pnl": pnl,
                    "costs": costs,
                    "exit_reason": reason,
                },
            },
        )

    async def close_all_positions(self, reason: str = "EOD_CLOSE", exchange: str = None):
        """Close all open positions, optionally filtered by exchange (e.g. 'NSE' or 'MCX')."""
        for symbol in list(self.positions.keys()):
            if exchange and not symbol.startswith(f"{exchange}:"):
                continue
            await self._close_position_market(symbol, reason)

    async def on_kill_switch(self, data: Dict[str, Any]):
        """Force-close all positions when circuit breaker fires."""
        log.warning(f"KILL_SWITCH received — force-closing all positions | reason={data}")
        await self.close_all_positions("KILL_SWITCH")

    def _resolve_starting_capital(self) -> float:
        """Use the lower of env CAPITAL and actual Kite account balance.

        Retries up to 3 times with 2-second backoff to handle the window between
        token refresh completing and the first API call. Falls back to env CAPITAL
        only after all attempts fail.
        """
        import time
        env_capital = float(settings.CAPITAL)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                margins = self.kite.margins(segment="equity")
                kite_balance = float(margins.get("net", env_capital))
                capital = min(env_capital, kite_balance)
                if capital < env_capital:
                    log.info(
                        f"Starting capital set to Kite balance ₹{capital:.2f} "
                        f"(lower than env CAPITAL ₹{env_capital:.2f})"
                    )
                else:
                    log.info(f"Starting capital: ₹{capital:.2f} (env CAPITAL)")
                return capital
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    log.warning(
                        f"Could not fetch Kite margins (attempt {attempt + 1}/3): {e}. "
                        f"Retrying in 2s..."
                    )
                    time.sleep(2)
        log.warning(
            f"Could not fetch Kite margins after 3 attempts — "
            f"using env CAPITAL ₹{env_capital:.2f}: {last_exc}"
        )
        return env_capital

    def start(self):
        """Subscribe to events."""
        event_bus.subscribe(EventType.SIGNAL, self.on_signal)
        event_bus.subscribe(EventType.CANDLE, self.on_candle)
        event_bus.subscribe(EventType.TICK, self.on_tick)
        event_bus.subscribe(EventType.KILL_SWITCH, self.on_kill_switch)
        log.info("Order manager started (LIVE MODE)")
