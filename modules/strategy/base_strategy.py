"""Abstract base class for all trading strategies."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from core.event_bus import event_bus, EventType
from core.logger import get_logger
from modules.strategy.signal_models import Signal
from modules.sensory.market_hours import is_signal_allowed


class BaseStrategy(ABC):
    """Abstract strategy that listens to CANDLE events and emits SIGNALs."""

    def __init__(self, name: str):
        self.name = name
        self._candle_history: Dict[str, List[Dict[str, Any]]] = {}
        self.log = get_logger(f"strategy.{name}")

    @abstractmethod
    def on_candle(self, candle: Dict[str, Any]) -> Optional[Signal]:
        """Process a completed candle and optionally return a signal.

        Must be implemented by subclasses.
        """
        ...

    async def _handle_candle(self, candle_data: Dict[str, Any]):
        """Event bus handler — called on each CANDLE event."""
        symbol = candle_data["symbol"]

        # Maintain candle history per symbol
        if symbol not in self._candle_history:
            self._candle_history[symbol] = []
        self._candle_history[symbol].append(candle_data)

        # Warmup candles: populate history + internal state, never emit signals
        if candle_data.get("warmup"):
            self.on_candle(candle_data)
            return

        # Check if signals are allowed (skip first 30 min)
        if not is_signal_allowed():
            return

        signal = self.on_candle(candle_data)
        if signal:
            self.log.info(
                f"SIGNAL | {signal.direction.value} {signal.instrument} | "
                f"strength={signal.strength.value} price={signal.price:.2f} | "
                f"strategy={self.name}"
            )
            await event_bus.publish(EventType.SIGNAL, signal.to_event_data())

    def get_history(self, symbol: str) -> List[Dict[str, Any]]:
        """Get candle history for a symbol."""
        return self._candle_history.get(symbol, [])

    def start(self):
        """Subscribe to candle events."""
        event_bus.subscribe(EventType.CANDLE, self._handle_candle)
        self.log.info(f"Strategy '{self.name}' started")
