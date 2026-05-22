"""Directional options buyer — converts BUY/SELL signals into CE/PE option purchases.

BUY signal  → buy ATM Nifty Call (CE): profit if market rises
SELL signal → buy ATM Nifty Put  (PE): profit if market falls

P&L is estimated using a delta model against the underlying spot price
(so no need to subscribe to separate option tick feeds in paper mode).
In live mode, actual orders are placed via OptionsExecutor.

Exit conditions (whichever hits first):
  - Stop-loss : premium falls to (1 - OPTIONS_STOP_PCT/100) × entry_premium
  - Target    : premium reaches OPTIONS_TARGET_MULT × entry_premium
  - EOD       : scheduler force-close at 15:30 IST (via close_all_positions)
"""

import math
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from core.config import settings
from core.event_bus import EventType, event_bus
from core.logger import get_logger, get_trade_logger

log = get_logger("options_buyer")
trade_log = get_trade_logger()

_IST = timezone(timedelta(hours=5, minutes=30))
_STRIKE_INTERVAL = 50
_MARKET_HOURS_PER_DAY = 6.5  # 9:15 to 15:45

# Nifty weekly options expire on Tuesday (NSE change effective 2025)
_EXPIRY_WEEKDAY = 1  # Monday=0, Tuesday=1

# Patterns that identify a Nifty underlying (futures or index)
_NIFTY_PATTERNS = ("NIFTY",)


def _round_strike(price: float) -> int:
    """Round spot to nearest Nifty strike (50-point intervals)."""
    return int(round(price / _STRIKE_INTERVAL) * _STRIKE_INTERVAL)


def _parse_timestamp(ts) -> Optional[datetime]:
    """Parse a timestamp that may be a datetime or an ISO string."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None
    return None


def _dte_from_timestamp(ts: Optional[datetime]) -> float:
    """Days-to-expiry: calendar days until next Tuesday (Nifty weekly expiry)."""
    if ts is None:
        return 2.0
    days_to_expiry = (_EXPIRY_WEEKDAY - ts.weekday()) % 7
    if days_to_expiry == 0:
        return 0.1  # expiry day — very little time left
    return float(days_to_expiry)


def _estimate_premium(spot: float, dte: float) -> float:
    """Estimate ATM option premium: spot × 0.006 × √DTE."""
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
    """Estimate current premium using delta (0.5 ATM) + theta (30%/day) model."""
    spot_move = current_spot - entry_spot
    delta_pnl = spot_move * 0.5 if option_type == "CE" else -spot_move * 0.5

    day_fraction = hours_elapsed / (_MARKET_HOURS_PER_DAY * max(dte, 0.1))
    theta_decay = entry_premium * 0.3 * day_fraction

    return max(entry_premium * 0.05, entry_premium + delta_pnl - theta_decay)


def _hours_since(entry_time, current_time) -> float:
    """Hours elapsed between two timestamps; handles both datetime and ISO string."""
    entry_dt = _parse_timestamp(entry_time) if not isinstance(entry_time, datetime) else entry_time
    current_dt = _parse_timestamp(current_time) if not isinstance(current_time, datetime) else current_time
    if entry_dt is None or current_dt is None:
        return 1.0
    try:
        return max(0.0, (current_dt.timestamp() - entry_dt.timestamp()) / 3600)
    except Exception:
        return 1.0


class OptionsDirectionalBuyer:
    """Paper/live directional options execution module.

    In paper mode  (PAPER_TRADE=True)  : simulates fills, tracks P&L via delta model.
    In live mode   (PAPER_TRADE=False) : places real Kite orders via OptionsExecutor.
    """

    def __init__(self, kite=None, circuit_breaker=None):
        if not settings.PAPER_TRADE and kite is None:
            raise ValueError(
                "Live options trading requires a valid Kite session. "
                "kite=None with PAPER_TRADE=False — check access token."
            )

        self._kite = kite
        self._breaker = circuit_breaker
        self.balance = float(settings.CAPITAL)
        self.lot_size = settings.OPTIONS_LOT_SIZE
        self.stop_pct = settings.OPTIONS_STOP_PCT / 100.0
        self.target_mult = settings.OPTIONS_TARGET_MULT

        self._position: Optional[Dict[str, Any]] = None
        self.trade_history: List[Dict[str, Any]] = []
        self._executor = None  # OptionsExecutor (live mode only)

        if not settings.PAPER_TRADE:
            from modules.execution.options_executor import OptionsExecutor
            self._executor = OptionsExecutor(kite=kite)

        self._validate_risk_parameters()

    def _validate_risk_parameters(self):
        """Warn if a single worst-case stop-out exceeds the daily loss limit."""
        # Typical Nifty ATM premium at DTE=2
        typical_premium = 24000 * 0.006 * math.sqrt(2)
        max_single_loss = typical_premium * self.stop_pct * self.lot_size
        daily_limit = settings.CAPITAL * (settings.MAX_DAILY_LOSS_PCT / 100)
        if max_single_loss > daily_limit:
            # Compute the highest stop % that keeps worst-case loss within 90% of daily limit
            safe_stop_pct = math.floor(
                (daily_limit * 0.9) / (typical_premium * self.lot_size) * 100
            )
            log.warning(
                f"RISK CONFIG MISMATCH: worst-case options loss ~Rs.{max_single_loss:.0f} "
                f"exceeds daily limit Rs.{daily_limit:.0f} "
                f"(CAPITAL={settings.CAPITAL}, MAX_DAILY_LOSS_PCT={settings.MAX_DAILY_LOSS_PCT}%). "
                f"Circuit breaker will trip on the first stop-out. "
                f"Set OPTIONS_STOP_PCT={safe_stop_pct} (or lower) in .env to fix this."
            )

    def start(self):
        event_bus.subscribe(EventType.SIGNAL, self.on_signal)
        event_bus.subscribe(EventType.CANDLE, self.on_candle)
        event_bus.subscribe(EventType.TICK, self.on_tick)
        event_bus.subscribe(EventType.KILL_SWITCH, self.on_kill_switch)
        log.info(
            f"Options directional buyer started | "
            f"lot={self.lot_size} stop={self.stop_pct:.0%} target={self.target_mult}x | "
            f"mode={'PAPER' if settings.PAPER_TRADE else 'LIVE'}"
        )

    def _is_nifty_instrument(self, symbol: str) -> bool:
        upper = symbol.upper()
        return any(p in upper for p in _NIFTY_PATTERNS)

    async def on_signal(self, signal_data: Dict[str, Any]):
        """React to a BUY or SELL signal from AdaptiveStrategy."""
        if self._breaker and not self._breaker.can_trade():
            log.warning("Circuit breaker active — options signal rejected")
            return

        direction = signal_data.get("direction", "")
        if direction not in ("BUY", "SELL"):
            return

        # Only trade Nifty options
        instrument = signal_data.get("instrument", "")
        if not self._is_nifty_instrument(instrument):
            log.debug(f"Options buyer skipping non-Nifty signal: {instrument}")
            return

        # Existing position handling
        if self._position:
            existing_type = self._position["option_type"]
            new_type = "CE" if direction == "BUY" else "PE"
            if existing_type == new_type:
                log.info(f"Already holding {existing_type} — skipping duplicate {direction} signal")
                return
            await self._close_position("SIGNAL_REVERSAL")

        spot = signal_data.get("price", 0.0)
        if spot <= 0:
            log.warning("Signal missing price — cannot enter options position")
            return

        option_type = "CE" if direction == "BUY" else "PE"
        strike = _round_strike(spot)

        # Signal timestamps are serialised as ISO strings by Signal.to_event_data()
        ts = _parse_timestamp(signal_data.get("timestamp")) or datetime.now(_IST)
        dte = _dte_from_timestamp(ts)
        entry_premium = _estimate_premium(spot, dte)

        # Reject if cost exceeds available balance
        cost = entry_premium * self.lot_size
        if cost > self.balance:
            log.warning(
                f"Insufficient balance Rs.{self.balance:.0f} for {option_type} "
                f"(cost=Rs.{cost:.0f}) — skipping"
            )
            return

        position_data: Dict[str, Any] = {
            "id": str(uuid.uuid4())[:8],
            "option_type": option_type,
            "strike": strike,
            "entry_spot": spot,
            "entry_premium": entry_premium,
            "entry_time": ts,
            "dte": dte,
            "lot_size": self.lot_size,
            "stop_premium": entry_premium * (1 - self.stop_pct),
            "target_premium": entry_premium * self.target_mult,
            "sl_order_id": None,
        }

        # Live mode: place entry order first; abort if it fails
        if self._executor:
            success = await self._executor.enter_directional(strike, option_type, self.lot_size)
            if not success:
                log.error(f"Entry order failed for {option_type} strike={strike} — position not opened")
                return
            sl_order_id = await self._executor.place_sl_order(
                option_type, position_data["stop_premium"]
            )
            position_data["sl_order_id"] = sl_order_id

        # Commit position only after successful entry (or in paper mode)
        self._position = position_data
        position_id = position_data["id"]

        log.info(
            f"OPTIONS {option_type} ENTRY | strike={strike} | "
            f"premium={entry_premium:.1f} | lot={self.lot_size} | "
            f"cost=Rs.{cost:.0f} | DTE={dte:.1f} | id={position_id}"
        )
        trade_log.info(
            f"OPEN | {option_type} NIFTY{strike}{option_type} | "
            f"premium={entry_premium:.1f} qty={self.lot_size} | spot={spot:.0f}"
        )

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
                "stop_loss": position_data["stop_premium"],
                "target": position_data["target_premium"],
            },
        )

    async def on_tick(self, tick_data: Dict[str, Any]):
        """Check stop/target on every tick for intraday protection."""
        if not self._position:
            return
        if tick_data.get("warmup"):
            return
        # Only react to ticks from our Nifty underlying
        if not self._is_nifty_instrument(tick_data.get("symbol", "")):
            return

        spot = tick_data.get("last_price", 0.0)
        if spot <= 0:
            return

        ts = _parse_timestamp(tick_data.get("timestamp"))
        hours_elapsed = _hours_since(self._position["entry_time"], ts) if ts else 1.0

        current_prem = _current_premium(
            entry_premium=self._position["entry_premium"],
            entry_spot=self._position["entry_spot"],
            current_spot=spot,
            option_type=self._position["option_type"],
            hours_elapsed=hours_elapsed,
            dte=self._position["dte"],
        )

        if current_prem <= self._position["stop_premium"]:
            await self._close_position("STOP_HIT", exit_premium=current_prem, spot=spot, ts=ts)
        elif current_prem >= self._position["target_premium"]:
            await self._close_position("TARGET_HIT", exit_premium=current_prem, spot=spot, ts=ts)

    async def on_candle(self, candle_data: Dict[str, Any]):
        """Check stop/target on candle close (fallback to tick-based checks)."""
        if candle_data.get("warmup"):
            return
        if not self._position:
            return

        spot = candle_data.get("close", 0.0)
        if spot <= 0:
            return

        ts = _parse_timestamp(candle_data.get("timestamp"))
        hours_elapsed = _hours_since(self._position["entry_time"], ts)

        current_prem = _current_premium(
            entry_premium=self._position["entry_premium"],
            entry_spot=self._position["entry_spot"],
            current_spot=spot,
            option_type=self._position["option_type"],
            hours_elapsed=hours_elapsed,
            dte=self._position["dte"],
        )

        if current_prem <= self._position["stop_premium"]:
            await self._close_position("STOP_HIT", exit_premium=current_prem, spot=spot, ts=ts)
        elif current_prem >= self._position["target_premium"]:
            await self._close_position("TARGET_HIT", exit_premium=current_prem, spot=spot, ts=ts)

    async def _close_position(
        self,
        reason: str,
        exit_premium: Optional[float] = None,
        spot: Optional[float] = None,
        ts=None,
    ):
        """Close the open option position and record P&L."""
        pos = self._position
        if not pos:
            return
        self._position = None

        if exit_premium is None:
            if spot and spot > 0:
                hours = _hours_since(pos["entry_time"], ts)
                exit_premium = _current_premium(
                    pos["entry_premium"], pos["entry_spot"], spot,
                    pos["option_type"], hours, pos["dte"]
                )
            else:
                exit_premium = pos["entry_premium"]

        pnl = (exit_premium - pos["entry_premium"]) * pos["lot_size"]
        pnl -= 40.0  # flat brokerage round-trip

        self.balance += pnl
        if self._breaker:
            await self._breaker.update_pnl(pnl)

        self.trade_history.append({
            "instrument": f"NIFTY{pos['strike']}{pos['option_type']}",
            "direction": "BUY",
            "entry_price": pos["entry_premium"],
            "exit_price": exit_premium,
            "quantity": pos["lot_size"],
            "pnl": pnl,
            "costs": 40.0,
            "exit_reason": reason,
            "strategy": "options_directional",
        })

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

        # Live mode: skip cancel+close if the exchange SL-M already squared off the position
        if self._executor:
            sl_already_filled = await self._executor.check_sl_filled(pos.get("sl_order_id"))
            if sl_already_filled:
                log.info("SL-M already filled by exchange — skipping manual close order")
            else:
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

    async def on_kill_switch(self, data: Dict[str, Any]):
        """Force-close open position when circuit breaker fires."""
        log.warning(
            f"KILL_SWITCH received — force-closing options position | "
            f"reason={data.get('reason', 'unknown')}"
        )
        await self.close_all_positions("KILL_SWITCH")

    async def close_all_positions(self, reason: str = "SHUTDOWN"):
        """Force-close any open position (called by scheduler or on shutdown)."""
        if self._position:
            await self._close_position(reason)

    def get_status(self) -> Dict[str, Any]:
        return {
            "balance": self.balance,
            "open_position": self._position,
            "mode": "PAPER" if settings.PAPER_TRADE else "LIVE",
        }
