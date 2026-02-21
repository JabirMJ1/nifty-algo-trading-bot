"""NSE market session time helpers. All times in IST."""

from datetime import datetime, time
import pytz

IST = pytz.timezone("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
PRE_MARKET_START = time(9, 0)
NO_SIGNAL_UNTIL = time(9, 45)  # skip first 30 min


def now_ist() -> datetime:
    """Current time in IST."""
    return datetime.now(IST)


def is_market_open() -> bool:
    """Check if NSE market is currently open (9:15 – 15:30 IST, Mon–Fri)."""
    now = now_ist()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    current_time = now.time()
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def is_pre_market() -> bool:
    """Check if we're in pre-market session (9:00 – 9:15 IST)."""
    now = now_ist()
    if now.weekday() >= 5:
        return False
    current_time = now.time()
    return PRE_MARKET_START <= current_time < MARKET_OPEN


def is_signal_allowed() -> bool:
    """No signals in first 30 min of market (9:15–9:45)."""
    now = now_ist()
    current_time = now.time()
    return is_market_open() and current_time >= NO_SIGNAL_UNTIL


def minutes_to_close() -> int:
    """Minutes remaining until market close. Returns -1 if market is closed."""
    if not is_market_open():
        return -1
    now = now_ist()
    close_dt = now.replace(hour=15, minute=30, second=0, microsecond=0)
    delta = close_dt - now
    return max(0, int(delta.total_seconds() / 60))


def ist_to_utc(dt: datetime) -> datetime:
    """Convert IST datetime to UTC."""
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt.astimezone(pytz.UTC)


def utc_to_ist(dt: datetime) -> datetime:
    """Convert UTC datetime to IST."""
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    return dt.astimezone(IST)
