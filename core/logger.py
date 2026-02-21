"""Structured logging setup using loguru with file rotation."""

import sys
from loguru import logger

# Remove default handler
logger.remove()

# Console handler — human-readable
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> | {message}",
    level="INFO",
    colorize=True,
)

# File handler — structured JSON for analysis
logger.add(
    "logs/bot_{time:YYYY-MM-DD}.log",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    rotation="1 day",
    retention="30 days",
    compression="gz",
    level="DEBUG",
    enqueue=True,  # thread-safe
)

# Trade-specific log
logger.add(
    "logs/trades_{time:YYYY-MM-DD}.log",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}",
    rotation="1 day",
    retention="90 days",
    filter=lambda record: "trade" in record["extra"],
    level="INFO",
)


def get_logger(name: str):
    """Get a named logger instance."""
    return logger.bind(module=name)


def get_trade_logger():
    """Get logger for trade-specific events."""
    return logger.bind(trade=True)
