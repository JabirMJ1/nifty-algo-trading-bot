"""Multi-Strategy — combines ORB, EMA Crossover, and VWAP Reversion.

Each strategy votes independently. A signal is emitted when:
- Any STRONG signal from any strategy, OR
- Two or more strategies agree on direction (consensus)
"""

from typing import Any, Dict, List, Optional
from modules.strategy.base_strategy import BaseStrategy
from modules.strategy.signal_models import Direction, Signal, SignalStrength
from modules.strategy.orb_strategy import ORBStrategy
from modules.strategy.ema_crossover import EMACrossoverStrategy
from modules.strategy.vwap_reversion import VWAPReversionStrategy
from modules.sensory.market_hours import now_ist
from core.logger import get_logger

log = get_logger("multi_strategy")


class MultiStrategy(BaseStrategy):
    """Combines multiple strategies with voting/consensus logic."""

    def __init__(self):
        super().__init__("multi_strategy")
        self.orb = ORBStrategy()
        self.ema = EMACrossoverStrategy()
        self.vwap = VWAPReversionStrategy()
        self._strategies = [self.orb, self.ema, self.vwap]

    def on_candle(self, candle: Dict[str, Any]) -> Optional[Signal]:
        symbol = candle["symbol"]

        # Feed candle to all sub-strategies
        for strat in self._strategies:
            strat._candle_history.setdefault(symbol, []).append(candle)

        # Collect signals from each
        signals = []
        for strat in self._strategies:
            sig = strat.on_candle(candle)
            if sig:
                signals.append(sig)

        if not signals:
            return None

        # Rule 1: Any STRONG signal passes through directly
        for sig in signals:
            if sig.strength == SignalStrength.STRONG:
                log.info(
                    f"STRONG signal from {sig.strategy_name}: "
                    f"{sig.direction.value} {symbol}"
                )
                return Signal(
                    instrument=symbol,
                    direction=sig.direction,
                    strength=SignalStrength.STRONG,
                    timestamp=now_ist(),
                    strategy_name=f"multi({sig.strategy_name})",
                    price=sig.price,
                    metadata={
                        "source": sig.strategy_name,
                        "source_meta": sig.metadata,
                        "consensus": False,
                    },
                )

        # Rule 2: Two or more strategies agree on direction
        buy_signals = [s for s in signals if s.direction == Direction.BUY]
        sell_signals = [s for s in signals if s.direction == Direction.SELL]

        if len(buy_signals) >= 2:
            sources = [s.strategy_name for s in buy_signals]
            log.info(f"CONSENSUS BUY from {sources} on {symbol}")
            return Signal(
                instrument=symbol,
                direction=Direction.BUY,
                strength=SignalStrength.STRONG,
                timestamp=now_ist(),
                strategy_name=f"multi({'+'.join(sources)})",
                price=buy_signals[0].price,
                metadata={
                    "sources": sources,
                    "consensus": True,
                },
            )

        if len(sell_signals) >= 2:
            sources = [s.strategy_name for s in sell_signals]
            log.info(f"CONSENSUS SELL from {sources} on {symbol}")
            return Signal(
                instrument=symbol,
                direction=Direction.SELL,
                strength=SignalStrength.STRONG,
                timestamp=now_ist(),
                strategy_name=f"multi({'+'.join(sources)})",
                price=sell_signals[0].price,
                metadata={
                    "sources": sources,
                    "consensus": True,
                },
            )

        # Single MODERATE signal — still trade it but as moderate
        sig = signals[0]
        return Signal(
            instrument=symbol,
            direction=sig.direction,
            strength=SignalStrength.MODERATE,
            timestamp=now_ist(),
            strategy_name=f"multi({sig.strategy_name})",
            price=sig.price,
            metadata={
                "source": sig.strategy_name,
                "source_meta": sig.metadata,
                "consensus": False,
            },
        )
