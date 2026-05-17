"""Fetch NSE:NIFTY50 15-min historical data from Kite and merge with existing parquet.

Run after regenerating your daily token:
    python generate_token.py
    python fetch_historical_data.py
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect

from core.config import settings
from core.logger import get_logger

log = get_logger("fetch_historical")

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path("data")
PARQUET_PATH = DATA_DIR / "NSE_NIFTY50_15min.parquet"

# NSE:NIFTY50 index instrument token — fixed, does not change
NIFTY50_TOKEN = 256265

# Kite allows ~2 years of 15-min data; fetch from 2024-01-01 to today
FETCH_FROM = datetime(2024, 1, 1, tzinfo=IST)
FETCH_TO   = datetime.now(tz=IST)
INTERVAL   = "15minute"
CHUNK_DAYS = 55  # conservative — Kite limit is 60 days per call for intraday


def fetch_chunks(kite: KiteConnect) -> pd.DataFrame:
    all_rows = []
    start = FETCH_FROM
    while start < FETCH_TO:
        end = min(start + timedelta(days=CHUNK_DAYS), FETCH_TO)
        try:
            data = kite.historical_data(
                instrument_token=NIFTY50_TOKEN,
                from_date=start,
                to_date=end,
                interval=INTERVAL,
            )
            all_rows.extend(data)
            print(f"  Fetched {len(data):>4} candles  {start.date()} → {end.date()}")
        except Exception as e:
            print(f"  ERROR {start.date()} → {end.date()}: {e}")
        start = end + timedelta(days=1)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    return df


def main():
    # Validate token
    kite = KiteConnect(api_key=settings.KITE_API_KEY)
    kite.set_access_token(settings.KITE_ACCESS_TOKEN)
    try:
        profile = kite.profile()
        print(f"Token valid — {profile['user_name']}")
    except Exception as e:
        print(f"Token invalid: {e}")
        print("Run:  python generate_token.py  then retry.")
        return

    print(f"\nFetching NSE:NIFTY50 15-min data from {FETCH_FROM.date()} to {FETCH_TO.date()}")
    print("─" * 60)
    fresh = fetch_chunks(kite)
    if fresh.empty:
        print("No data returned from Kite.")
        return

    # Normalise timezone to IST before merging
    import pytz
    ist_tz = pytz.FixedOffset(330)
    if fresh.index.tz is None:
        fresh.index = fresh.index.tz_localize(ist_tz)
    else:
        fresh.index = fresh.index.tz_convert(ist_tz)

    # Merge with existing parquet
    if PARQUET_PATH.exists():
        existing = pd.read_parquet(PARQUET_PATH)
        print(f"\nExisting parquet: {len(existing)} candles  "
              f"({existing.index.min().date()} → {existing.index.max().date()})")
        if existing.index.tz is None:
            existing.index = existing.index.tz_localize(ist_tz)
        else:
            existing.index = existing.index.tz_convert(ist_tz)
        combined = pd.concat([existing, fresh])
        combined = combined[~combined.index.duplicated(keep="last")]
        combined.sort_index(inplace=True)
    else:
        combined = fresh

    combined.to_parquet(PARQUET_PATH)
    days = combined.index.normalize().nunique()
    print(f"\nSaved {len(combined)} candles across {days} trading days")
    print(f"Range: {combined.index.min().date()} → {combined.index.max().date()}")
    print(f"Parquet: {PARQUET_PATH}")


if __name__ == "__main__":
    main()
