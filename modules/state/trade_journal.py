"""Trade journal — logs all trades to PostgreSQL."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger

log = get_logger("trade_journal")


class Base(DeclarativeBase):
    pass


class TradeRecord(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String, unique=True, index=True)
    instrument = Column(String, nullable=False, index=True)
    direction = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float)
    quantity = Column(Integer, nullable=False)
    pnl = Column(Float, default=0)
    costs = Column(Float, default=0)
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime)
    strategy = Column(String)
    stop_loss = Column(Float)
    target = Column(Float)
    exit_reason = Column(String)


class TradeJournal:
    """Persists all trades to PostgreSQL."""

    def __init__(self):
        self.engine = None
        self.SessionLocal = None
        self._initialized = False

    def initialize(self):
        """Create engine and tables."""
        try:
            self.engine = create_engine(settings.DB_URL, echo=False)
            Base.metadata.create_all(self.engine)
            self.SessionLocal = sessionmaker(bind=self.engine)
            self._initialized = True
            log.info("Trade journal initialized (PostgreSQL connected)")
        except Exception as e:
            log.warning(f"Could not connect to PostgreSQL: {e}. Journal disabled.")
            self._initialized = False

    async def on_trade(self, trade_data: Dict[str, Any]):
        """Record trade events."""
        if not self._initialized:
            return

        trade_type = trade_data.get("type")

        if trade_type == "ENTRY":
            self._record_entry(trade_data)
        elif trade_type == "EXIT":
            trade = trade_data.get("trade", {})
            if trade:
                self._record_exit(trade)

    def _record_entry(self, data: Dict[str, Any]):
        try:
            with self.SessionLocal() as session:
                record = TradeRecord(
                    trade_id=data.get("order_id", ""),
                    instrument=data["instrument"],
                    direction=data["direction"],
                    entry_price=data["price"],
                    quantity=data["quantity"],
                    entry_time=datetime.utcnow(),
                    strategy=data.get("strategy", ""),
                    stop_loss=data.get("stop_loss", 0),
                    target=data.get("target", 0),
                )
                session.add(record)
                session.commit()
            log.debug(f"Trade entry recorded: {data.get('order_id')}")
        except Exception as e:
            log.error(f"Failed to record trade entry: {e}")

    def _record_exit(self, trade: Dict[str, Any]):
        try:
            with self.SessionLocal() as session:
                record = (
                    session.query(TradeRecord)
                    .filter_by(trade_id=trade.get("trade_id", ""))
                    .first()
                )
                if record:
                    record.exit_price = trade.get("exit_price", 0)
                    record.exit_time = datetime.utcnow()
                    record.pnl = trade.get("pnl", 0)
                    record.costs = trade.get("costs", 0)
                    record.exit_reason = trade.get("exit_reason", "")
                    session.commit()
                else:
                    # No entry record found — create a complete record
                    record = TradeRecord(
                        trade_id=trade.get("trade_id", ""),
                        instrument=trade.get("instrument", ""),
                        direction=trade.get("direction", ""),
                        entry_price=trade.get("entry_price", 0),
                        exit_price=trade.get("exit_price", 0),
                        quantity=trade.get("quantity", 0),
                        pnl=trade.get("pnl", 0),
                        costs=trade.get("costs", 0),
                        entry_time=datetime.fromisoformat(trade["entry_time"])
                        if trade.get("entry_time")
                        else datetime.utcnow(),
                        exit_time=datetime.utcnow(),
                        strategy=trade.get("strategy", ""),
                        exit_reason=trade.get("exit_reason", ""),
                    )
                    session.add(record)
                    session.commit()
            log.debug(f"Trade exit recorded: {trade.get('trade_id')}")
        except Exception as e:
            log.error(f"Failed to record trade exit: {e}")

    def get_trades_today(self) -> List[Dict[str, Any]]:
        """Get all trades from today."""
        if not self._initialized:
            return []
        try:
            with self.SessionLocal() as session:
                today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                records = (
                    session.query(TradeRecord)
                    .filter(TradeRecord.entry_time >= today)
                    .all()
                )
                return [
                    {
                        "trade_id": r.trade_id,
                        "instrument": r.instrument,
                        "direction": r.direction,
                        "entry_price": r.entry_price,
                        "exit_price": r.exit_price,
                        "quantity": r.quantity,
                        "pnl": r.pnl,
                        "costs": r.costs,
                        "exit_reason": r.exit_reason,
                    }
                    for r in records
                ]
        except Exception as e:
            log.error(f"Failed to fetch today's trades: {e}")
            return []

    def start(self):
        """Initialize and subscribe to events."""
        self.initialize()
        event_bus.subscribe(EventType.TRADE, self.on_trade)
        log.info("Trade journal started")
