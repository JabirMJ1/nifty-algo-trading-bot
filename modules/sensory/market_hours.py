"""Market session time helpers. All times in IST."""

from datetime import datetime, time
import pytz

IST = pytz.timezone("Asia/Kolkata")

# NSE
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
PRE_MARKET_START = time(9, 0)
NO_SIGNAL_UNTIL = time(9, 30)  # skip first 15 min (opening volatility)

# MCX — bullion/energy commodities (GOLDPETAL, GOLD, SILVER, CRUDEOIL etc.) trade 9:00 AM – 11:30 PM IST
MCX_OPEN = time(9, 0)
MCX_CLOSE = time(23, 30)
MCX_NO_SIGNAL_UNTIL = time(9, 15)  # skip first 15 min after MCX open


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


def is_mcx_open() -> bool:
    """Check if MCX session is open (9:00 AM – 11:30 PM IST, Mon–Fri)."""
    now = now_ist()
    if now.weekday() >= 5:
        return False
    return MCX_OPEN <= now.time() <= MCX_CLOSE


def is_pre_market() -> bool:
    """Check if we're in pre-market session (9:00 – 9:15 IST)."""
    now = now_ist()
    if now.weekday() >= 5:
        return False
    current_time = now.time()
    return PRE_MARKET_START <= current_time < MARKET_OPEN


def is_signal_allowed(exchange: str = "NSE") -> bool:
    """Gate signals by exchange session.

    MCX: 9:15 AM – 11:30 PM IST (bullion/energy commodities).
    NSE: 9:30 AM – 3:30 PM IST.
    """
    t = now_ist().time()
    if exchange == "MCX":
        return is_mcx_open() and t >= MCX_NO_SIGNAL_UNTIL
    return is_market_open() and t >= NO_SIGNAL_UNTIL


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
