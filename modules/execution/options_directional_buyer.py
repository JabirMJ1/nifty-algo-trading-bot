"""Directional options buyer — converts BUY/SELL signals into CE/PE option purchases.

BUY signal  → buy ATM Nifty Call (CE): profit if market rises
SELL signal → buy ATM Nifty Put  (PE): profit if market falls

P&L is estimated using a delta model against the underlying spot price
(so no need to subscribe to separate option tick feeds in paper mode).
In live mode, actual orders are placed via OptionsExecutor.

Exit conditions (whichever hits first):
  - Stop-loss : premium falls to (1 - OPTIONS_STOP_PCT/100) × entry_premium
  - Target    : premium reaches OPTIONS_TARGET_MULT × entry_premium
  - EOD       : 15:15 IST force-close
"""

import math
import uuid
from datetime import datetime, time, timezone, timedelta
from typing import Any, Dict, Optional

from core.config import settings
from core.event_bus import EventType, event_bus
from core.logger import get_logger, get_trade_logger

log = get_logger("options_buyer")
trade_log = get_trade_logger()

_IST = timezone(timedelta(hours=5, minutes=30))
_EOD_EXIT = time(15, 15)
_STRIKE_INTERVAL = 50
_MARKET_HOURS_PER_DAY = 6.5  # 9:15 to 15:45


def _round_strike(price: float) -> int:
    """Round spot to nearest Nifty strike (50-point intervals)."""
    return int(round(price / _STRIKE_INTERVAL) * _STRIKE_INTERVAL)


def _dte_from_timestamp(ts) -> float:
    """Days-to-expiry: trading days until next Thursday (weekly expiry)."""
    if not hasattr(ts, "weekday"):
        return 2.0
    # weekday(): Mon=0 … Thu=3 … Sun=6
    days_to_thursday = (3 - ts.weekday()) % 7
    if days_to_thursday == 0:
        return 0.1  # expiry day — very little time left
    return float(days_to_thursday)


def _estimate_premium(spot: float, dte: float) -> float:
    """Estimate ATM option premium from spot and DTE.

    Calibrated formula from straddle_920.py:
        premium ≈ spot × 0.006 × √DTE

    Approximate real-world checks:
        DTE=5 → ~180-220 pts   (0.006 × √5 ≈ 0.0134 × 23000 ≈ 308, reduced by liquidity/IV)
        DTE=2 → ~130-160 pts
        DTE=0 → ~40-60 pts
    """
    if dte <= 0:
        dte = 0.1
    return round(spot * 0.006 * math.sqrt(dte), 2)


def _current_premium(
    entry_premium: float,
    entry_spot: float,
    current_spot: float,
    option_type: str,
    hours_elapsed: float,
    dte: float,
) -> float:
    """Estimate current option premium using a simple delta + theta model.

    Delta ≈ 0.5 for ATM options.
    Theta decay ≈ 30% of entry premium per full trading day.
    """
    # Delta P&L
    spot_move = current_spot - entry_spot
    if option_type == "CE":
        delta_pnl = spot_move * 0.5
    else:  # PE
        delta_pnl = -spot_move * 0.5

    # Theta decay (fraction of day elapsed)
    day_fraction = hours_elapsed / (_MARKET_HOURS_PER_DAY * max(dte, 0.1))
    theta_decay = entry_premium * 0.3 * day_fraction

    estimated = entry_premium + delta_pnl - theta_decay
    # Floor at 5% of entry (option won't go to exactly zero during the day)
    return max(entry_premium * 0.05, estimated)


class OptionsDirectionalBuyer:
    """Paper/live directional options execution module.

    In paper mode  (PAPER_TRADE=True)  : simulates fills, tracks P&L via delta model.
    In live mode   (PAPER_TRADE=False) : places real Kite orders via OptionsExecutor.
    """

    def __init__(self, kite=None, circuit_breaker=None):
        self._kite = kite
        self._breaker = circuit_breaker
        self.balance = float(settings.CAPITAL)
        self.lot_size = settings.OPTIONS_LOT_SIZE
        self.stop_pct = settings.OPTIONS_STOP_PCT / 100.0
        self.target_mult = settings.OPTIONS_TARGET_MULT

        self._position: Optional[Dict[str, Any]] = None
        self._executor = None  # OptionsExecutor (live mode only)

        if not settings.PAPER_TRADE and kite:
            from modules.execution.options_executor import OptionsExecutor
            self._executor = OptionsExecutor(kite=kite)

    def start(self):
        event_bus.subscribe(EventType.SIGNAL, self.on_signal)
        event_bus.subscribe(EventType.CANDLE, self.on_candle)
        log.info(
            f"Options directional buyer started | "
            f"lot={self.lot_size} stop={self.stop_pct:.0%} target={self.target_mult}x | "
            f"mode={'PAPER' if settings.PAPER_TRADE else 'LIVE'}"
        )

    async def on_signal(self, signal_data: Dict[str, Any]):
        """React to a BUY or SELL signal from AdaptiveStrategy."""
        if self._breaker and not self._breaker.can_trade():
            log.warning("Circuit breaker active — options signal rejected")
            return

        direction = signal_data.get("direction", "")
        if direction not in ("BUY", "SELL"):
            return

        # If already in a position, check for reversal
        if self._position:
            existing_type = self._position["option_type"]
            new_type = "CE" if direction == "BUY" else "PE"
            if existing_type == new_type:
                log.info(f"Already holding {existing_type} — skipping duplicate signal")
                return
            # Opposite direction — close first
            await self._close_position("SIGNAL_REVERSAL")

        spot = signal_data.get("price", 0.0)
        if spot <= 0:
            log.warning("Signal missing price — cannot enter options position")
            return

        option_type = "CE" if direction == "BUY" else "PE"
        strike = _round_strike(spot)
        ts = signal_data.get("timestamp", datetime.now(_IST))
        dte = _dte_from_timestamp(ts)
        entry_premium = _estimate_premium(spot, dte)

        position_id = str(uuid.uuid4())[:8]
        self._position = {
            "id": position_id,
            "option_type": option_type,
            "strike": strike,
            "entry_spot": spot,
            "entry_premium": entry_premium,
            "entry_time": ts,
            "dte": dte,
            "lot_size": self.lot_size,
            "stop_premium": entry_premium * (1 - self.stop_pct),
            "target_premium": entry_premium * self.target_mult,
        }

        cost = entry_premium * self.lot_size
        log.info(
            f"OPTIONS {option_type} ENTRY | strike={strike} | "
            f"premium={entry_premium:.1f} | lot={self.lot_size} | "
            f"cost=Rs.{cost:.0f} | DTE={dte:.1f} | id={position_id}"
        )
        trade_log.info(
            f"OPEN | {option_type} NIFTY{strike}{option_type} | "
            f"premium={entry_premium:.1f} qty={self.lot_size} | spot={spot:.0f}"
        )

        # Live mode: place actual order + exchange-level SL-M stop
        if self._executor:
            await self._executor.enter_directional(strike, option_type, self.lot_size)
            sl_order_id = await self._executor.place_sl_order(
                option_type, self._position["stop_premium"]
            )
            self._position["sl_order_id"] = sl_order_id

        await event_bus.publish(
            EventType.TRADE,
            {
                "type": "ENTRY",
                "order_id": position_id,
                "instrument": f"NIFTY{strike}{option_type}",
                "direction": "BUY",
                "price": entry_premium,
                "quantity": self.lot_size,
                "strategy": signal_data.get("strategy_name", "adaptive"),
                "stop_loss": self._position["stop_premium"],
                "target": self._position["target_premium"],
            },
        )

    async def on_candle(self, candle_data: Dict[str, Any]):
        """Check stop/target/EOD on every 15-min candle."""
        if not self._position:
            return

        ts = candle_data.get("timestamp")
        candle_time = ts.time() if hasattr(ts, "time") else None

        # EOD force-close
        if candle_time and candle_time >= _EOD_EXIT:
            await self._close_position("EOD_EXIT", candle_data)
            return

        spot = candle_data.get("close", 0.0)
        if spot <= 0:
            return

        # Estimate current option value
        entry_time = self._position["entry_time"]
        if hasattr(ts, "timestamp") and hasattr(entry_time, "timestamp"):
            hours_elapsed = (ts.timestamp() - entry_time.timestamp()) / 3600
        else:
            hours_elapsed = 1.0

        current_prem = _current_premium(
            entry_premium=self._position["entry_premium"],
            entry_spot=self._position["entry_spot"],
            current_spot=spot,
            option_type=self._position["option_type"],
            hours_elapsed=max(0.0, hours_elapsed),
            dte=self._position["dte"],
        )

        # Check stop
        if current_prem <= self._position["stop_premium"]:
            await self._close_position("STOP_HIT", candle_data, current_prem)
            return

        # Check target
        if current_prem >= self._position["target_premium"]:
            await self._close_position("TARGET_HIT", candle_data, current_prem)
            return

    async def _close_position(
        self,
        reason: str,
        candle: Optional[Dict[str, Any]] = None,
        exit_premium: Optional[float] = None,
    ):
        """Close the open option position and record P&L."""
        pos = self._position
        if not pos:
            return
        self._position = None

        if exit_premium is None:
            # Estimate from candle close if available
            if candle:
                spot = candle.get("close", pos["entry_spot"])
                ts = candle.get("timestamp")
                entry_time = pos["entry_time"]
                if hasattr(ts, "timestamp") and hasattr(entry_time, "timestamp"):
                    hours = max(0.0, (ts.timestamp() - entry_time.timestamp()) / 3600)
                else:
                    hours = 1.0
                exit_premium = _current_premium(
                    pos["entry_premium"], pos["entry_spot"], spot,
                    pos["option_type"], hours, pos["dte"]
                )
            else:
                exit_premium = pos["entry_premium"]  # flat fallback

        pnl = (exit_premium - pos["entry_premium"]) * pos["lot_size"]
        # Deduct brokerage (flat Rs.40 round-trip for options)
        pnl -= 40.0

        self.balance += pnl
        if self._breaker:
            await self._breaker.update_pnl(pnl)

        outcome = "PROFIT" if pnl >= 0 else "LOSS"
        log.info(
            f"OPTIONS {pos['option_type']} CLOSE ({reason}) | "
            f"entry={pos['entry_premium']:.1f} exit={exit_premium:.1f} | "
            f"PnL=Rs.{pnl:.0f} | balance=Rs.{self.balance:.0f}"
        )
        trade_log.info(
            f"CLOSE | {pos['option_type']} NIFTY{pos['strike']}{pos['option_type']} | "
            f"PnL={pnl:.0f} | reason={reason}"
        )

        # Live mode: cancel standing SL-M then close via executor
        if self._executor:
            await self._executor.cancel_sl_order(pos.get("sl_order_id"))
            await self._executor.close_directional(pos["option_type"], reason)

        await event_bus.publish(
            EventType.TRADE,
            {
                "type": "EXIT",
                "trade": {
                    "instrument": f"NIFTY{pos['strike']}{pos['option_type']}",
                    "direction": "BUY",
                    "entry_price": pos["entry_premium"],
                    "exit_price": exit_premium,
                    "quantity": pos["lot_size"],
                    "pnl": pnl,
                    "exit_reason": reason,
                    "strategy": "options_directional",
                },
                "reason": reason,
                "balance": self.balance,
            },
        )

        await event_bus.publish(
            EventType.ALERT,
            {
                "type": "TRADE_CLOSED",
                "message": (
                    f"Options Trade {outcome}\n"
                    f"NIFTY {pos['strike']} {pos['option_type']}\n"
                    f"Entry premium: {pos['entry_premium']:.1f}\n"
                    f"Exit premium : {exit_premium:.1f}\n"
                    f"PnL          : Rs.{pnl:.0f}\n"
                    f"Reason       : {reason}\n"
                    f"Balance      : Rs.{self.balance:.0f}"
                ),
            },
        )

    async def close_all_positions(self, reason: str = "SHUTDOWN"):
        """Force-close any open position (called on graceful shutdown)."""
        if self._position:
            await self._close_position(reason)

    def get_status(self) -> Dict[str, Any]:
        return {
            "balance": self.balance,
            "open_position": self._position,
            "mode": "PAPER" if settings.PAPER_TRADE else "LIVE",
        }
