"""Historical data download from Kite Connect API."""

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from kiteconnect import KiteConnect
from core.config import settings
from core.logger import get_logger

log = get_logger("data_fetcher")

DATA_DIR = Path("data")


class DataFetcher:
    """Downloads and caches historical OHLCV data from Kite."""

    def __init__(self, kite: Optional[KiteConnect] = None):
        self.kite = kite
        DATA_DIR.mkdir(exist_ok=True)

    def _get_kite(self) -> KiteConnect:
        if self.kite:
            return self.kite
        kite = KiteConnect(api_key=settings.KITE_API_KEY)
        kite.set_access_token(settings.KITE_ACCESS_TOKEN)
        return kite

    def _resolve_token(self, symbol: str) -> int:
        """Resolve instrument symbol to token."""
        kite = self._get_kite()
        exchange, tradingsymbol = symbol.split(":")
        instruments = kite.instruments(exchange)
        for inst in instruments:
            if inst["tradingsymbol"] == tradingsymbol:
                return inst["instrument_token"]
        raise ValueError(f"Could not resolve token for {symbol}")

    def fetch_historical(
        self,
        symbol: str,
        from_date: datetime,
        to_date: datetime,
        interval: str = "5minute",
    ) -> pd.DataFrame:
        """Fetch historical OHLCV data from Kite.

        Args:
            symbol: e.g. "NSE:NIFTY25FEBFUT"
            from_date: start date
            to_date: end date
            interval: candle interval ("minute", "5minute", "15minute", "day")

        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        kite = self._get_kite()
        token = self._resolve_token(symbol)

        log.info(f"Fetching {symbol} ({interval}) from {from_date} to {to_date}")

        # Kite API allows max 60 days per request for minute data
        all_data = []
        current_start = from_date
        chunk_days = 55  # conservative

        while current_start < to_date:
            chunk_end = min(current_start + timedelta(days=chunk_days), to_date)
            try:
                data = kite.historical_data(
                    instrument_token=token,
                    from_date=current_start,
                    to_date=chunk_end,
                    interval=interval,
                )
                all_data.extend(data)
                log.info(
                    f"Fetched {len(data)} candles: {current_start.date()} to {chunk_end.date()}"
                )
            except Exception as e:
                log.error(f"Failed to fetch data chunk: {e}")

            current_start = chunk_end + timedelta(days=1)

        if not all_data:
            log.warning(f"No data fetched for {symbol}")
            return pd.DataFrame()

        df = pd.DataFrame(all_data)
        df.columns = [c.lower() for c in df.columns]
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)

        log.info(f"Total candles fetched: {len(df)}")
        return df

    def save_to_parquet(self, df: pd.DataFrame, symbol: str, interval: str = "5min"):
        """Save DataFrame to parquet file."""
        safe_name = symbol.replace(":", "_").replace("/", "_")
        filepath = DATA_DIR / f"{safe_name}_{interval}.parquet"
        df.to_parquet(filepath)
        log.info(f"Saved {len(df)} rows to {filepath}")
        return filepath

    def load_from_parquet(self, symbol: str, interval: str = "5min") -> pd.DataFrame:
        """Load DataFrame from parquet file."""
        safe_name = symbol.replace(":", "_").replace("/", "_")
        filepath = DATA_DIR / f"{safe_name}_{interval}.parquet"
        if not filepath.exists():
            log.warning(f"No cached data for {symbol} at {filepath}")
            return pd.DataFrame()
        df = pd.read_parquet(filepath)
        log.info(f"Loaded {len(df)} rows from {filepath}")
        return df

    def fetch_and_cache(
        self,
        symbol: str,
        days_back: int = 180,
        interval: str = "5minute",
    ) -> pd.DataFrame:
        """Fetch data and save to parquet."""
        to_date = datetime.now()
        from_date = to_date - timedelta(days=days_back)
        df = self.fetch_historical(symbol, from_date, to_date, interval)
        if not df.empty:
            self.save_to_parquet(df, symbol, interval.replace("minute", "min"))
        return df
