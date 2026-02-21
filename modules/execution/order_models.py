"""Pydantic models for orders and trades."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PLACED = "PLACED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Order(BaseModel):
    """Represents a trading order."""

    order_id: str = ""
    instrument: str
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    quantity: int
    price: float = 0.0
    trigger_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    strategy_name: str = ""
    timestamp: datetime = datetime.utcnow()
    metadata: Dict[str, Any] = {}

    def to_event_data(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")


class Trade(BaseModel):
    """Represents a completed trade (entry + exit)."""

    trade_id: str = ""
    instrument: str
    direction: str  # BUY or SELL
    entry_price: float
    exit_price: float = 0.0
    quantity: int
    pnl: float = 0.0
    entry_time: datetime
    exit_time: Optional[datetime] = None
    strategy: str = ""
    stop_loss: float = 0.0
    target: float = 0.0
    exit_reason: str = ""
    costs: float = 0.0

    def to_event_data(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")

    def calculate_pnl(self) -> float:
        """Calculate PnL for the trade."""
        if self.direction == "BUY":
            gross = (self.exit_price - self.entry_price) * self.quantity
        else:
            gross = (self.entry_price - self.exit_price) * self.quantity
        self.pnl = gross - self.costs
        return self.pnl
