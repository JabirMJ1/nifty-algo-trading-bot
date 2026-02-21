"""Stop-loss management: hard stop + trailing stop + target."""

from typing import Any, Dict, Optional
from core.config import settings
from core.logger import get_logger

log = get_logger("stop_manager")


class StopLevel:
    """Tracks stop-loss and target levels for a single position."""

    def __init__(
        self,
        entry_price: float,
        direction: str,  # "BUY" or "SELL"
        atr: float,
    ):
        self.entry_price = entry_price
        self.direction = direction
        self.atr = atr

        # Initial hard stop: 1.5x ATR from entry
        stop_distance = atr * settings.STOP_ATR_MULTIPLIER
        # Target: 3x ATR
        target_distance = atr * settings.TARGET_ATR_MULTIPLIER
        # Trailing activation: 1x ATR profit
        trail_activation_distance = atr * settings.TRAIL_ATR_MULTIPLIER
        # Trail distance: 1x ATR
        self.trail_distance = atr * settings.TRAIL_ATR_MULTIPLIER

        if direction == "BUY":
            self.stop_loss = entry_price - stop_distance
            self.target = entry_price + target_distance
            self.trail_activation = entry_price + trail_activation_distance
        else:
            self.stop_loss = entry_price + stop_distance
            self.target = entry_price - target_distance
            self.trail_activation = entry_price - trail_activation_distance

        self.trailing_active = False
        self.highest_pnl_price = entry_price  # tracks best price for trailing

        log.info(
            f"Stops set: entry={entry_price:.2f} dir={direction} "
            f"SL={self.stop_loss:.2f} target={self.target:.2f} "
            f"trail_activate={self.trail_activation:.2f}"
        )

    def update(self, current_price: float) -> Optional[str]:
        """Update stops based on current price. Returns exit reason or None.

        Returns:
            "STOP_HIT" | "TARGET_HIT" | "TRAIL_STOP_HIT" | None
        """
        if self.direction == "BUY":
            return self._update_long(current_price)
        else:
            return self._update_short(current_price)

    def _update_long(self, price: float) -> Optional[str]:
        # Check target hit
        if price >= self.target:
            return "TARGET_HIT"

        # Check hard stop hit
        if price <= self.stop_loss:
            if self.trailing_active:
                return "TRAIL_STOP_HIT"
            return "STOP_HIT"

        # Trailing stop logic
        if not self.trailing_active and price >= self.trail_activation:
            self.trailing_active = True
            log.info(f"Trailing stop activated at price {price:.2f}")

        if self.trailing_active and price > self.highest_pnl_price:
            self.highest_pnl_price = price
            new_stop = price - self.trail_distance
            if new_stop > self.stop_loss:
                self.stop_loss = new_stop
                log.debug(f"Trailing stop moved to {self.stop_loss:.2f}")

        return None

    def _update_short(self, price: float) -> Optional[str]:
        # Check target hit
        if price <= self.target:
            return "TARGET_HIT"

        # Check hard stop hit
        if price >= self.stop_loss:
            if self.trailing_active:
                return "TRAIL_STOP_HIT"
            return "STOP_HIT"

        # Trailing stop logic
        if not self.trailing_active and price <= self.trail_activation:
            self.trailing_active = True
            log.info(f"Trailing stop activated at price {price:.2f}")

        if self.trailing_active and price < self.highest_pnl_price:
            self.highest_pnl_price = price
            new_stop = price + self.trail_distance
            if new_stop < self.stop_loss:
                self.stop_loss = new_stop
                log.debug(f"Trailing stop moved to {self.stop_loss:.2f}")

        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_price": self.entry_price,
            "direction": self.direction,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "trailing_active": self.trailing_active,
            "highest_pnl_price": self.highest_pnl_price,
        }


class StopManager:
    """Manages stop levels for all open positions."""

    def __init__(self):
        self._stops: Dict[str, StopLevel] = {}  # symbol -> StopLevel

    def set_stops(self, symbol: str, entry_price: float, direction: str, atr: float):
        """Set stops for a new position."""
        self._stops[symbol] = StopLevel(entry_price, direction, atr)

    def check_stops(self, symbol: str, current_price: float) -> Optional[str]:
        """Check if any stop/target hit for the symbol. Returns exit reason or None."""
        stop = self._stops.get(symbol)
        if not stop:
            return None
        return stop.update(current_price)

    def remove_stops(self, symbol: str):
        """Remove stops when position is closed."""
        if symbol in self._stops:
            del self._stops[symbol]
            log.info(f"Stops removed for {symbol}")

    def get_stop_level(self, symbol: str) -> Optional[StopLevel]:
        return self._stops.get(symbol)

    def get_all_stops(self) -> Dict[str, Dict[str, Any]]:
        return {sym: stop.to_dict() for sym, stop in self._stops.items()}
