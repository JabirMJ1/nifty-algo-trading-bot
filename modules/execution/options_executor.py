"""Options executor — handles multi-leg options orders via Kite Connect.

Specifically designed for the 9:20 Short Straddle but generic enough
for other options strategies.
"""

import asyncio
from datetime import date, datetime
from typing import Any, Dict, Optional

from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger, get_trade_logger

log = get_logger("options_executor")
trade_log = get_trade_logger()

STRIKE_INTERVAL = 50


def round_to_strike(price: float) -> int:
    """Round to nearest Nifty strike."""
    return int(round(price / STRIKE_INTERVAL) * STRIKE_INTERVAL)


class OptionsExecutor:
    """Manages options order placement and position tracking."""

    def __init__(self, kite=None):
        self.kite = kite
        self.positions: Dict[str, Dict[str, Any]] = {}  # keyed by "CE" / "PE"
        # Cache instruments list by date to avoid repeated 50k-record HTTP calls
        self._instr_cache: Dict[str, list] = {}  # date_str -> instruments list

    # ── Instrument resolution ──────────────────────────────────────────

    def _fetch_nfo_instruments(self) -> list:
        """Fetch NFO instruments from Kite, using a daily in-process cache."""
        today = date.today().isoformat()
        if today not in self._instr_cache:
            self._instr_cache.clear()  # drop previous day's cache
            self._instr_cache[today] = self.kite.instruments("NFO")
        return self._instr_cache[today]

    def _resolve_symbols_sync(self, strike: int) -> Dict[str, Optional[str]]:
        """Resolve CE and PE trading symbols from the cached instruments list.

        Picks the nearest expiry >= today for each leg (weekly before monthly).
        Returns dict like {"CE": "NFO:NIFTY26MAY27600CE", "PE": "NFO:NIFTY26MAY27600PE"}.
        """
        if not self.kite:
            return {"CE": None, "PE": None}

        try:
            instruments = self._fetch_nfo_instruments()
            today = date.today()
            ce_symbol: Optional[str] = None
            ce_expiry: Optional[date] = None
            pe_symbol: Optional[str] = None
            pe_expiry: Optional[date] = None

            for inst in instruments:
                if not (
                    inst["name"] == "NIFTY"
                    and inst["instrument_type"] in ("CE", "PE")
                    and inst["strike"] == strike
                ):
                    continue

                expiry = inst.get("expiry")
                if not expiry or expiry < today:
                    continue

                ts = inst["tradingsymbol"]
                if inst["instrument_type"] == "CE":
                    if ce_expiry is None or expiry < ce_expiry:
                        ce_symbol = f"NFO:{ts}"
                        ce_expiry = expiry
                else:
                    if pe_expiry is None or expiry < pe_expiry:
                        pe_symbol = f"NFO:{ts}"
                        pe_expiry = expiry

            return {"CE": ce_symbol, "PE": pe_symbol}
        except Exception as e:
            log.error(f"Failed to resolve option symbols: {e}")
            return {"CE": None, "PE": None}

    async def _resolve_option_symbols(self, strike: int) -> Dict[str, Optional[str]]:
        """Async wrapper — runs the blocking Kite HTTP call off the event loop."""
        return await asyncio.to_thread(self._resolve_symbols_sync, strike)

    # ── Straddle (short sell CE + PE) ─────────────────────────────────

    async def enter_straddle(self, spot_price: float, lot_size: int = None):
        """Place short straddle: sell ATM CE + sell ATM PE."""
        lot_size = lot_size or settings.OPTIONS_LOT_SIZE
        strike = round_to_strike(spot_price)
        symbols = await self._resolve_option_symbols(strike)

        if not symbols.get("CE") or not symbols.get("PE"):
            log.error(f"Could not resolve option symbols for strike {strike}")
            return False

        log.info(f"Entering straddle: Strike={strike} | CE={symbols['CE']} PE={symbols['PE']}")

        for opt_type in ("CE", "PE"):
            symbol = symbols[opt_type]
            exchange, tradingsymbol = symbol.split(":")
            try:
                order_id = await asyncio.to_thread(
                    self.kite.place_order,
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=lot_size,
                    product=self.kite.PRODUCT_MIS,
                    order_type=self.kite.ORDER_TYPE_MARKET,
                )
                self.positions[opt_type] = {
                    "symbol": symbol,
                    "order_id": order_id,
                    "strike": strike,
                    "type": opt_type,
                    "quantity": lot_size,
                    "entry_time": datetime.now(),
                }
                log.info(f"SOLD {opt_type} | {symbol} | order_id={order_id}")
                trade_log.info(f"STRADDLE SELL {opt_type} | {symbol} | qty={lot_size}")
            except Exception as e:
                log.error(f"Failed to place {opt_type} order: {e}")
                if opt_type == "PE" and "CE" in self.positions:
                    await self.close_leg("CE", "PE_ORDER_FAILED")

        return len(self.positions) == 2

    # ── Directional (buy single CE or PE) ─────────────────────────────

    async def enter_directional(self, strike: int, option_type: str, lot_size: int = None) -> bool:
        """Place a single BUY order for a CE or PE option."""
        lot_size = lot_size or settings.OPTIONS_LOT_SIZE
        symbols = await self._resolve_option_symbols(strike)
        symbol = symbols.get(option_type)

        if not symbol:
            log.error(f"Could not resolve {option_type} symbol for strike {strike}")
            return False

        exchange, tradingsymbol = symbol.split(":")
        try:
            order_id = await asyncio.to_thread(
                self.kite.place_order,
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                quantity=lot_size,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET,
            )
            self.positions[option_type] = {
                "symbol": symbol,
                "order_id": order_id,
                "strike": strike,
                "type": option_type,
                "quantity": lot_size,
                "entry_time": datetime.now(),
            }
            log.info(f"BOUGHT {option_type} | {symbol} | qty={lot_size} | order_id={order_id}")
            trade_log.info(f"DIRECTIONAL BUY {option_type} | {symbol} | qty={lot_size}")
            return True
        except Exception as e:
            log.error(f"Failed to place directional {option_type} order: {e}")
            return False

    async def close_directional(self, option_type: str, reason: str = "EXIT"):
        """Close (sell back) a single directional CE or PE position."""
        await self.close_leg(option_type, reason)

    # ── Stop-loss management ───────────────────────────────────────────

    async def place_sl_order(self, option_type: str, stop_premium: float) -> Optional[str]:
        """Place an exchange-level SL-M sell order at stop_premium.

        Hard protection: fires at the exchange even if the bot crashes.
        """
        pos = self.positions.get(option_type)
        if not pos or not self.kite:
            return None

        symbol = pos["symbol"]
        exchange, tradingsymbol = symbol.split(":")

        try:
            sl_order_id = await asyncio.to_thread(
                self.kite.place_order,
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=pos["quantity"],
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_SL_M,
                trigger_price=round(stop_premium, 2),
            )
            log.info(
                f"OPTIONS SL-M placed | {tradingsymbol} | "
                f"trigger={stop_premium:.2f} | sl_order_id={sl_order_id}"
            )
            return sl_order_id
        except Exception as e:
            log.error(f"Failed to place options SL-M for {tradingsymbol}: {e}")
            return None

    async def cancel_sl_order(self, sl_order_id: Optional[str]):
        """Cancel a pending SL-M order before issuing a normal close."""
        if not sl_order_id or not self.kite:
            return
        try:
            await asyncio.to_thread(
                self.kite.cancel_order,
                variety=self.kite.VARIETY_REGULAR,
                order_id=sl_order_id,
            )
            log.info(f"OPTIONS SL-M cancelled | sl_order_id={sl_order_id}")
        except Exception as e:
            log.warning(f"Failed to cancel options SL-M {sl_order_id}: {e}")

    async def check_sl_filled(self, sl_order_id: Optional[str]) -> bool:
        """Return True if the SL-M order has already been filled by the exchange.

        Prevents sending a redundant close order when the SL-M already squared off
        the position, which would otherwise open a new accidental short.
        """
        if not sl_order_id or not self.kite:
            return False
        try:
            history = await asyncio.to_thread(self.kite.order_history, sl_order_id)
            if history:
                last_status = history[-1].get("status", "")
                return last_status == "COMPLETE"
        except Exception as e:
            log.warning(f"Could not check SL-M status for {sl_order_id}: {e}")
        return False

    # ── Leg management ─────────────────────────────────────────────────

    async def close_leg(self, leg: str, reason: str):
        """Close a single leg (CE or PE)."""
        pos = self.positions.get(leg)
        if not pos:
            return

        symbol = pos["symbol"]
        exchange, tradingsymbol = symbol.split(":")

        try:
            order_id = await asyncio.to_thread(
                self.kite.place_order,
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                quantity=pos["quantity"],
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET,
            )
            log.info(f"CLOSED {leg} | {symbol} | reason={reason} | order_id={order_id}")
            trade_log.info(f"STRADDLE CLOSE {leg} | {symbol} | reason={reason}")
        except Exception as e:
            # Position may already be closed by exchange SL-M — clear state anyway
            log.warning(f"Close order rejected for {leg} (may be already filled by SL-M): {e}")
        finally:
            self.positions.pop(leg, None)

    async def close_all(self, reason: str = "EOD_EXIT"):
        """Close all open option legs."""
        for leg in list(self.positions.keys()):
            await self.close_leg(leg, reason)

    @property
    def has_position(self) -> bool:
        return len(self.positions) > 0
