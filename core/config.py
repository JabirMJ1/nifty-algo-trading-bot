"""Configuration module — all settings loaded from .env via pydantic-settings."""

from typing import List
from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    # Kite Connect
    KITE_API_KEY: str = ""
    KITE_API_SECRET: str = ""
    KITE_ACCESS_TOKEN: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Database
    DB_URL: str = "postgresql://user:pass@localhost:5432/tradingbot"
    REDIS_URL: str = "redis://localhost:6379"

    # Trading
    PAPER_TRADE: bool = True
    CAPITAL: int = 100000  # in rupees
    MAX_DAILY_LOSS_PCT: float = 2.0
    RISK_PER_TRADE_PCT: float = 1.0
    MAX_OPEN_POSITIONS: int = 3

    # Instruments (comma-separated in .env)
    INSTRUMENTS: List[str] = ["NSE:NIFTY25FEBFUT"]

    # Strategy defaults
    EMA_FAST: int = 9
    EMA_SLOW: int = 21
    RSI_PERIOD: int = 14
    ATR_PERIOD: int = 14
    CANDLE_INTERVAL_MIN: int = 5

    # Risk defaults
    STOP_ATR_MULTIPLIER: float = 1.5
    TRAIL_ATR_MULTIPLIER: float = 1.0
    TARGET_ATR_MULTIPLIER: float = 3.0

    # Slippage simulation (ticks)
    SLIPPAGE_TICKS: int = 2

    # Costs
    BROKERAGE_PER_ORDER: float = 20.0  # rupees
    STT_PCT: float = 0.05
    EXCHANGE_CHARGES_PCT: float = 0.0019

    @field_validator("INSTRUMENTS", mode="before")
    @classmethod
    def parse_instruments(cls, v):
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


# Singleton
settings = Settings()
