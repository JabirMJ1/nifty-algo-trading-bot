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

        available_capital = max(0.0, self.capital - deployed_margin)

        # Primary constraint: how many lots can margin support?
        margin_lots = 0
        margin_per_lot = 0.0
        if price > 0 and lot_size > 0:
            margin_per_lot = price * lot_size * (settings.MARGIN_PCT / 100)
            if margin_per_lot > 0:
                margin_lots = int(available_capital / margin_per_lot)

        # Secondary constraint: ATR-based risk sizing
        risk_amount = self.capital * self.risk_pct
        atr_lots = max(1, int((risk_amount / atr) / lot_size)) if risk_amount >= atr * lot_size else 1

        # Use the more conservative of the two
        lots = min(margin_lots, atr_lots) if margin_lots > 0 else atr_lots

        if lots == 0:
            log.warning("Position size is 0 — insufficient capital for 1 lot.")

        log.info(
            f"Position sizing: capital={self.capital}, deployed={deployed_margin:.0f}, "
            f"available={available_capital:.0f}, margin_lots={margin_lots}, "
            f"atr_lots={atr_lots} (risk={risk_amount:.0f}/ATR={atr:.2f}), final_lots={lots}"
        )
        return lots

    def update_capital(self, new_capital: float):
        """Update available capital (after PnL changes)."""
        self.capital = new_capital
        log.info(f"Capital updated to {new_capital:.2f}")
