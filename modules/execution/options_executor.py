"""Options executor — handles multi-leg options orders via Kite Connect.

Specifically designed for the 9:20 Short Straddle but generic enough
for other options strategies.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional

from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger, get_trade_logger

log = get_logger("options_executor")
trade_log = get_trade_logger()

NIFTY_LOT_SIZE = 25
STRIKE_INTERVAL = 50


def round_to_strike(price: float) -> int:
    """Round to nearest Nifty strike."""
    return int(round(price / STRIKE_INTERVAL) * STRIKE_INTERVAL)


def get_weekly_expiry_symbol(strike: int, option_type: str, expiry_str: str) -> str:
    """Build Kite option trading symbol.

    Example: NIFTY2522027600CE
    Format: NIFTY + YY + M + DD (or MMM for monthly) + strike + CE/PE

    Args:
        strike: ATM strike price
        option_type: 'CE' or 'PE'
        expiry_str: Date string like '2026-02-27' (next Thursday)
    """
    # Kite uses a specific format — we'll resolve via instruments API instead
    # This is a fallback format builder
    return f"NIFTY{expiry_str}{strike}{option_type}"


class OptionsExecutor:
    """Manages options order placement and position tracking for straddles."""

    def __init__(self, kite=None):
        self.kite = kite
        self.positions: Dict[str, Dict[str, Any]] = {}  # keyed by "CE" / "PE"
        self.daily_pnl: float = 0.0

    def _resolve_option_symbols(self, strike: int) -> Dict[str, str]:
        """Resolve CE and PE trading symbols from Kite instruments.

        Returns dict like {"CE": "NFO:NIFTY26FEB27600CE", "PE": "NFO:NIFTY26FEB27600PE"}
        """
        if not self.kite:
            return {}

        try:
            instruments = self.kite.instruments("NFO")
            ce_symbol = None
            pe_symbol = None

            for inst in instruments:
                ts = inst["tradingsymbol"]
                if (inst["name"] == "NIFTY"
                        and inst["instrument_type"] in ("CE", "PE")
                        and inst["strike"] == strike):
                    # Pick nearest weekly expiry
                    if inst["instrument_type"] == "CE" and not ce_symbol:
                        ce_symbol = f"NFO:{ts}"
                    elif inst["instrument_type"] == "PE" and not pe_symbol:
                        pe_symbol = f"NFO:{ts}"

                    if ce_symbol and pe_symbol:
                        break

            return {"CE": ce_symbol, "PE": pe_symbol}
        except Exception as e:
            log.error(f"Failed to resolve option symbols: {e}")
            return {}

    async def enter_straddle(self, spot_price: float, lot_size: int = NIFTY_LOT_SIZE):
        """Place short straddle: sell ATM CE + sell ATM PE."""
        strike = round_to_strike(spot_price)
        symbols = self._resolve_option_symbols(strike)

        if not symbols.get("CE") or not symbols.get("PE"):
            log.error(f"Could not resolve option symbols for strike {strike}")
            return False

        log.info(f"Entering straddle: Strike={strike} | CE={symbols['CE']} PE={symbols['PE']}")

        # Place both sell orders
        for opt_type in ("CE", "PE"):
            symbol = symbols[opt_type]
            exchange, tradingsymbol = symbol.split(":")

            try:
                order_id = self.kite.place_order(
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
                # If CE placed but PE fails, close CE immediately
                if opt_type == "PE" and "CE" in self.positions:
                    await self.close_leg("CE", "PE_ORDER_FAILED")

        return len(self.positions) == 2

    async def close_leg(self, leg: str, reason: str):
        """Close a single leg (CE or PE)."""
        pos = self.positions.get(leg)
        if not pos:
            return

        symbol = pos["symbol"]
        exchange, tradingsymbol = symbol.split(":")

        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=self.kite.TRANSACTION_TYPE_BUY,  # Buy back to close
                quantity=pos["quantity"],
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET,
            )
            log.info(f"CLOSED {leg} | {symbol} | reason={reason} | order_id={order_id}")
            trade_log.info(f"STRADDLE CLOSE {leg} | {symbol} | reason={reason}")
        except Exception as e:
            # Position may already be closed by exchange SL-M — clear internal state anyway
            log.warning(f"Close order rejected for {leg} (may be already filled by SL-M): {e}")
        finally:
            self.positions.pop(leg, None)

    async def close_all(self, reason: str = "EOD_EXIT"):
        """Close all open option legs."""
        for leg in list(self.positions.keys()):
            await self.close_leg(leg, reason)

    async def enter_directional(self, strike: int, option_type: str, lot_size: int = NIFTY_LOT_SIZE):
        """Place a single BUY order for a CE or PE option (directional buying).

        Args:
            strike: ATM strike price
            option_type: 'CE' or 'PE'
            lot_size: number of units (default 75 for Nifty)
        """
        symbols = self._resolve_option_symbols(strike)
        symbol = symbols.get(option_type)

        if not symbol:
            log.error(f"Could not resolve {option_type} symbol for strike {strike}")
            return False

        exchange, tradingsymbol = symbol.split(":")
        try:
            order_id = self.kite.place_order(
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
        """Close (sell) a single directional CE or PE position."""
        await self.close_leg(option_type, reason)

    async def place_sl_order(
        self, option_type: str, stop_premium: float
    ) -> Optional[str]:
        """Place an exchange-level SL-M sell order on the option at stop_premium.

        Fires at the exchange even if the bot crashes — hard protection.
        """
        pos = self.positions.get(option_type)
        if not pos or not self.kite:
            return None

        symbol = pos["symbol"]
        exchange, tradingsymbol = symbol.split(":")

        try:
            sl_order_id = self.kite.place_order(
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
        """Cancel a pending SL-M order (call before normal close to avoid double-fill)."""
        if not sl_order_id or not self.kite:
            return
        try:
            self.kite.cancel_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=sl_order_id,
            )
            log.info(f"OPTIONS SL-M cancelled | sl_order_id={sl_order_id}")
        except Exception as e:
            log.warning(f"Failed to cancel options SL-M {sl_order_id}: {e}")

    @property
    def has_position(self) -> bool:
        return len(self.positions) > 0
