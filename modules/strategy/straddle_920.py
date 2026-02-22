"""9:20 AM Short Straddle Strategy.

Sell ATM Call + ATM Put at 9:20 AM on Nifty weekly options.
- Stop-loss: 25% on each leg individually
- Exit: 3:15 PM or when stop hits
- If one leg stops out, keep the other running

Premium and P&L estimation uses a direct delta-based model
since we don't have historical options data.
"""

import math
from datetime import time
from typing import Any, Dict, Optional, Tuple
from core.logger import get_logger

log = get_logger("straddle_920")

NIFTY_LOT_SIZE = 25
STRIKE_INTERVAL = 50


def round_to_strike(price: float) -> int:
    return int(round(price / STRIKE_INTERVAL) * STRIKE_INTERVAL)


class Straddle920Strategy:
    """9:20 AM Short Straddle — direct P&L model from spot movement.

    Instead of trying to estimate option premiums precisely, we use:
    - Entry premium: estimated from vol and DTE
    - Stop trigger: based on spot movement (how far spot needs to move to cause 25% premium spike)
    - EOD P&L: credit received minus intrinsic value of ITM leg
    """

    def __init__(self, stop_loss_pct: float = 25.0, lot_size: int = NIFTY_LOT_SIZE):
        self.name = "straddle_920"
        self.stop_loss_pct = stop_loss_pct
        self.lot_size = lot_size

        self._current_day: str = ""
        self._position: Optional[Dict[str, Any]] = None
        self._traded_today: bool = False
        self._entry_time_window = (time(9, 15), time(9, 30))
        self._exit_time = time(15, 15)

    def _get_day(self, candle: Dict[str, Any]) -> str:
        ts = candle["timestamp"]
        if hasattr(ts, "strftime"):
            return ts.strftime("%Y-%m-%d")
        return str(ts)[:10]

    def _get_time(self, candle: Dict[str, Any]) -> Optional[time]:
        ts = candle["timestamp"]
        if hasattr(ts, "time"):
            return ts.time()
        return None

    def on_candle(self, candle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        day = self._get_day(candle)
        candle_time = self._get_time(candle)
        if candle_time is None:
            return None

        if self._current_day != day:
            self._current_day = day
            self._position = None
            self._traded_today = False

        spot = candle["close"]

        # Entry
        if (not self._traded_today
                and self._entry_time_window[0] <= candle_time <= self._entry_time_window[1]):
            return self._enter(candle, spot)

        # Manage position
        if self._position:
            if candle_time >= self._exit_time:
                return self._exit(candle, "EOD_EXIT")
            return self._check_stops(candle)

        return None

    def _estimate_premium_per_leg(self, spot: float, dte_days: float) -> float:
        """Estimate ATM option premium per leg.

        Real Nifty weekly ATM premiums (approx):
        - 5 DTE: ~180-220 points
        - 3 DTE: ~130-160 points
        - 1 DTE: ~70-90 points
        - 0 DTE: ~40-60 points
        """
        # Simple model: premium ≈ spot * 0.006 * sqrt(DTE)
        # Calibrated to match real Nifty weekly premiums
        if dte_days <= 0:
            dte_days = 0.1
        premium = spot * 0.006 * math.sqrt(dte_days)
        return round(premium, 2)

    def _enter(self, candle: Dict[str, Any], spot: float) -> Dict[str, Any]:
        strike = round_to_strike(spot)

        # Calculate DTE (days to Thursday expiry)
        ts = candle["timestamp"]
        if hasattr(ts, "weekday"):
            dte = (3 - ts.weekday()) % 7
            if dte == 0:
                dte = 0.15  # Expiry day
        else:
            dte = 2

        premium_per_leg = self._estimate_premium_per_leg(spot, dte)
        total_credit = premium_per_leg * 2 * self.lot_size

        # Stop trigger: spot movement that causes 25% premium increase
        # For ATM option with delta ~0.5:
        # Premium increase = spot_move * 0.55 (delta + gamma effect)
        # Stop when: spot_move * 0.55 >= premium * 0.25
        # So: spot_move >= premium * 0.25 / 0.55 = premium * 0.45
        stop_points = premium_per_leg * (self.stop_loss_pct / 100) / 0.55
        stop_loss_premium = premium_per_leg * (1 + self.stop_loss_pct / 100)

        self._traded_today = True
        self._position = {
            "strike": strike,
            "premium_per_leg": premium_per_leg,
            "entry_spot": spot,
            "entry_time": candle["timestamp"],
            "total_credit": total_credit,
            "dte": dte,
            "ce_active": True,
            "pe_active": True,
            "stop_points": stop_points,  # spot movement that triggers stop
            "stop_loss_premium": stop_loss_premium,
            "day_high": candle["high"],
            "day_low": candle["low"],
        }

        log.info(
            f"STRADDLE ENTRY | Strike={strike} | Premium/leg={premium_per_leg:.1f} | "
            f"Credit={total_credit:.0f} | Stop={stop_points:.0f}pts | DTE={dte:.1f}"
        )

        return {
            "action": "ENTRY",
            "strike": strike,
            "ce_premium": premium_per_leg,
            "pe_premium": premium_per_leg,
            "total_credit": total_credit,
            "spot": spot,
        }

    def _check_stops(self, candle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pos = self._position
        if not pos:
            return None

        strike = pos["strike"]
        stop_pts = pos["stop_points"]

        # Update day high/low
        pos["day_high"] = max(pos["day_high"], candle["high"])
        pos["day_low"] = min(pos["day_low"], candle["low"])

        result = None

        # CE stop: triggers if spot goes too far above strike
        if pos["ce_active"] and (pos["day_high"] - strike) >= stop_pts:
            pos["ce_active"] = False
            # Loss = premium increase = (move - stop_threshold) + stop_threshold premium
            ce_loss = pos["stop_loss_premium"] - pos["premium_per_leg"]
            ce_loss_rs = ce_loss * self.lot_size
            log.info(f"CE STOP HIT | Spot high={pos['day_high']:.0f} Strike={strike} | Loss={ce_loss_rs:.0f}")
            result = {"action": "CE_STOP", "ce_loss": ce_loss_rs, "spot": candle["close"]}

        # PE stop: triggers if spot goes too far below strike
        if pos["pe_active"] and (strike - pos["day_low"]) >= stop_pts:
            pos["pe_active"] = False
            pe_loss = pos["stop_loss_premium"] - pos["premium_per_leg"]
            pe_loss_rs = pe_loss * self.lot_size
            log.info(f"PE STOP HIT | Spot low={pos['day_low']:.0f} Strike={strike} | Loss={pe_loss_rs:.0f}")
            if result:
                result["action"] = "BOTH_STOP"
                result["pe_loss"] = pe_loss_rs
            else:
                result = {"action": "PE_STOP", "pe_loss": pe_loss_rs, "spot": candle["close"]}

        if not pos["ce_active"] and not pos["pe_active"]:
            self._position = None

        return result

    def _exit(self, candle: Dict[str, Any], reason: str) -> Dict[str, Any]:
        pos = self._position
        if not pos:
            return None

        strike = pos["strike"]
        spot = candle["close"]
        premium = pos["premium_per_leg"]
        dte = pos["dte"]

        # EOD P&L calculation:
        # Credit received - value of remaining legs at close
        # ITM leg value ≈ intrinsic (time value near zero at EOD)
        # OTM leg value ≈ 0
        pnl = 0

        # Time decay benefit: options lost ~40-70% of time value during the day
        # (more decay on lower DTE days)
        eod_tv_factor = max(0.1, 0.5 - 0.15 / max(dte, 0.1))  # remaining time value at EOD

        if pos["ce_active"]:
            ce_intrinsic = max(0, spot - strike)
            ce_eod = ce_intrinsic + premium * eod_tv_factor * (1 if ce_intrinsic == 0 else 0.3)
            pnl += (premium - ce_eod) * self.lot_size

        if pos["pe_active"]:
            pe_intrinsic = max(0, strike - spot)
            pe_eod = pe_intrinsic + premium * eod_tv_factor * (1 if pe_intrinsic == 0 else 0.3)
            pnl += (premium - pe_eod) * self.lot_size

        log.info(f"STRADDLE EXIT ({reason}) | PnL={pnl:.0f} | Spot={spot:.0f} Strike={strike}")

        result = {
            "action": "EXIT",
            "reason": reason,
            "pnl": pnl,
            "ce_entry": premium,
            "pe_entry": premium,
            "ce_exit": 0,
            "pe_exit": 0,
            "spot": spot,
        }

        self._position = None
        return result

    def _exit_all(self, candle: Dict[str, Any], spot: float, reason: str) -> Optional[Dict[str, Any]]:
        """Force exit all positions — used by backtest for day-end cleanup."""
        if not self._position:
            return None
        return self._exit(candle, reason)

    @property
    def has_position(self) -> bool:
        return self._position is not None
