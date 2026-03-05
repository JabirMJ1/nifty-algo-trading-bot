"""Configuration module — all settings loaded from .env via pydantic-settings."""

from typing import List
from pydantic_settings import BaseSettings
from pydantic import field_validator, model_validator


class Settings(BaseSettings):
    # Kite Connect
    KITE_API_KEY: str = ""
    KITE_API_SECRET: str = ""
    KITE_ACCESS_TOKEN: str = ""
    # Auto token refresh credentials (optional — if set, token is refreshed automatically on startup)
    KITE_USER_ID: str = ""
    KITE_PASSWORD: str = ""
    KITE_TOTP_SECRET: str = ""

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
    # Futures margin as % of contract value (SEBI SPAN+Exposure).
    # MCX Gold/Goldpetal ~17%, Nifty Futures ~15%. Conservative default: 20%.
    MARGIN_PCT: float = 20.0

    # Instruments (comma-separated in .env)
    INSTRUMENTS: str = "NSE:NIFTY25FEBFUT"

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

    # News Sentiment
    NEWS_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    NEWS_FETCH_INTERVAL_MIN: int = 15
    NEWS_SENTIMENT_ENABLED: bool = True

    # Historical sentiment (backtesting)
    ALPHAVANTAGE_API_KEY: str = ""

    # S3 — EOD data archive (optional)
    S3_BUCKET: str = ""   # e.g. "my-trading-bot-data"; leave empty to disable

    # Healthchecks.io dead-man's switch (optional — leave empty to disable)
    HEALTHCHECKS_URL: str = ""

    # Options — directional buying (CE/PE)
    TRADE_OPTIONS: bool = False        # True → use OptionsDirectionalBuyer instead of futures
    OPTIONS_LOT_SIZE: int = 75         # Nifty weekly lot size
    OPTIONS_STOP_PCT: float = 50.0     # exit if premium drops this % from entry
    OPTIONS_TARGET_MULT: float = 2.5   # exit if premium reaches this multiple of entry

    # Costs
    BROKERAGE_PER_ORDER: float = 20.0  # rupees
    STT_PCT: float = 0.05
    EXCHANGE_CHARGES_PCT: float = 0.0019

    @model_validator(mode="after")
    def _check_live_credentials(self) -> "Settings":
        if not self.PAPER_TRADE:
            missing = [
                name for name, val in [
                    ("KITE_API_KEY", self.KITE_API_KEY),
                    ("KITE_API_SECRET", self.KITE_API_SECRET),
                    ("KITE_ACCESS_TOKEN", self.KITE_ACCESS_TOKEN),
                ]
                if not val
            ]
            if missing:
                raise ValueError(
                    f"Live trading requires these env vars to be set: {', '.join(missing)}"
                )
        return self

    @property
    def instruments_list(self) -> list:
        return [i.strip() for i in self.INSTRUMENTS.split(",") if i.strip()]

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


# Singleton
settings = Settings()
