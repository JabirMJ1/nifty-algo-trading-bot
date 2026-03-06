"""ATR-based position sizing module."""

from typing import Any, Dict, List
from core.config import settings
from core.logger import get_logger

log = get_logger("position_sizer")


def calculate_atr(candles: List[Dict[str, Any]], period: int = None) -> float:
    """Calculate Average True Range from candle history.

    True Range = max(H-L, |H-prevC|, |L-prevC|)
    ATR = SMA of True Range over period
    """
    period = period or settings.ATR_PERIOD
    if len(candles) < period + 1:
        # Fallback: use high-low range of last candle
        if candles:
            last = candles[-1]
            return last["high"] - last["low"]
        return 0.0

    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    # Use last `period` true ranges
    recent_tr = true_ranges[-period:]
    return sum(recent_tr) / len(recent_tr)


class PositionSizer:
    """Calculates position size based on ATR and risk parameters."""

    def __init__(self):
        self.capital = settings.CAPITAL
        self.risk_pct = settings.RISK_PER_TRADE_PCT / 100
        self.max_positions = settings.MAX_OPEN_POSITIONS

    def calculate_size(
        self,
        atr: float,
        current_open_positions: int,
        lot_size: int = 1,
        price: float = 0.0,
        deployed_margin: float = 0.0,
    ) -> int:
        """Calculate position size (number of lots).

        Formula: size = (capital * risk_pct) / (ATR * lot_multiplier)
        Then capped so that margin required ≤ available capital.
        Margin per lot = price × lot_size × MARGIN_PCT%
        deployed_margin: margin already locked in open positions (subtracted from capital cap).
        """
        if current_open_positions >= self.max_positions:
            log.warning(
                f"Max positions ({self.max_positions}) reached. No new trades."
            )
            return 0

        if atr <= 0:
            log.warning("ATR is zero or negative. Cannot size position.")
            return 0

        risk_amount = self.capital * self.risk_pct
        raw_size = risk_amount / atr

        # Round down to nearest lot
        lots = max(1, int(raw_size / lot_size)) if raw_size >= lot_size else 0

        # Cap by available capital (total minus already-deployed margin)
        available_capital = max(0.0, self.capital - deployed_margin)
        if price > 0 and lot_size > 0:
            margin_per_lot = price * lot_size * (settings.MARGIN_PCT / 100)
            if margin_per_lot > 0:
                max_lots_by_margin = int(available_capital / margin_per_lot)
                if lots > max_lots_by_margin:
                    log.warning(
                        f"Capping lots {lots} → {max_lots_by_margin} "
                        f"(margin_per_lot={margin_per_lot:.0f}, "
                        f"available_capital={available_capital:.0f}, "
                        f"deployed_margin={deployed_margin:.0f})"
                    )
                    lots = max_lots_by_margin

        if lots == 0:
            log.warning("Position size is 0 after margin cap — insufficient capital for 1 lot.")

        log.info(
            f"Position sizing: capital={self.capital}, deployed_margin={deployed_margin:.0f}, "
            f"available={available_capital:.0f}, risk={risk_amount:.2f}, "
            f"ATR={atr:.2f}, raw_size={raw_size:.2f}, lots={lots}"
        )
        return lots

    def update_capital(self, new_capital: float):
        """Update available capital (after PnL changes)."""
        self.capital = new_capital
        log.info(f"Capital updated to {new_capital:.2f}")
