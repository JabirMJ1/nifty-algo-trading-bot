"""Pydantic models for trading signals."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalStrength(str, Enum):
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


class Signal(BaseModel):
    """Trading signal emitted by a strategy."""

    instrument: str
    direction: Direction
    strength: SignalStrength
    timestamp: datetime
    strategy_name: str
    price: float
    metadata: Dict[str, Any] = {}

    def to_event_data(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")
