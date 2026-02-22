"""Fetch historical news sentiment from AlphaVantage for backtesting.

Usage:
    python backtesting/fetch_sentiment_history.py

Requires ALPHAVANTAGE_API_KEY in .env (free key at alphavantage.co).
Free tier: 25 requests/day. This script uses 7 requests (one per month Aug 2025–Feb 2026).

Output:
    data/sentiment_history.parquet  (columns: date, score, magnitude, article_count)
"""

import asyncio
import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pandas as pd

# Read .env directly so this script works without the full project installed
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("fetch_sentiment")

ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"

# Backtesting period
START_DATE = datetime(2025, 8, 1, tzinfo=timezone.utc)
END_DATE = datetime(2026, 2, 28, 23, 59, 59, tzinfo=timezone.utc)

# Seconds to sleep between API calls (free tier: 5 req/min = 12s minimum)
RATE_LIMIT_SLEEP = 15

OUTPUT_FILE = Path("data/sentiment_history.parquet")


async def fetch_month(
    session: aiohttp.ClientSession,
    api_key: str,
    time_from: datetime,
    time_to: datetime,
) -> list[dict]:
    """Fetch all articles for a given month from AlphaVantage NEWS_SENTIMENT."""
    params = {
        "function": "NEWS_SENTIMENT",
        "topics": "economy_fiscal,economy_monetary,financial_markets",
        "time_from": time_from.strftime("%Y%m%dT%H%M"),
        "time_to": time_to.strftime("%Y%m%dT%H%M"),
        "limit": 1000,
        "apikey": api_key,
    }

    async with session.get(
        ALPHAVANTAGE_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        if resp.status != 200:
            log.warning(f"AlphaVantage HTTP {resp.status} for {time_from.strftime('%Y-%m')}")
            return []

        data = await resp.json()

        if "Note" in data:
            log.warning(f"Rate limit message: {data['Note']}")
            return []

        if "Information" in data:
            log.warning(f"API info: {data['Information']}")
            return []

        if "Error Message" in data:
            log.error(f"AlphaVantage error: {data['Error Message']}")
            return []

        articles = data.get("feed", [])
        log.info(
            f"  {time_from.strftime('%Y-%m')}: fetched {len(articles)} articles"
        )
        return articles


def aggregate_daily(articles: list[dict]) -> list[dict]:
    """Group articles by date and compute weighted daily sentiment scores.

    Weight = relevance_score from article metadata (0.0–1.0).
    Magnitude = normalized std-dev of article scores (proxy for news divergence).
    """
    daily: dict[object, dict] = {}

    for article in articles:
        time_published = article.get("time_published", "")
        if not time_published or len(time_published) < 8:
            continue

        try:
            article_date = datetime.strptime(time_published[:8], "%Y%m%d").date()
        except ValueError:
            continue

        score = float(article.get("overall_sentiment_score", 0.0))
        relevance = float(article.get("relevance_score", 0.5))

        if article_date not in daily:
            daily[article_date] = {"scores": [], "weights": []}

        daily[article_date]["scores"].append(score)
        daily[article_date]["weights"].append(relevance)

    records = []
    for date, d in sorted(daily.items()):
        scores = d["scores"]
        weights = d["weights"]
        count = len(scores)

        total_weight = sum(weights)
        if total_weight > 0:
            weighted_score = sum(s * w for s, w in zip(scores, weights)) / total_weight
        else:
            weighted_score = sum(scores) / count

        # Magnitude: std-dev of scores normalised to 0–1
        # High divergence (bears vs bulls) → higher magnitude
        if count > 1:
            std = statistics.stdev(scores)
            magnitude = min(1.0, std * 2.5)
        else:
            magnitude = 0.2  # single article → low confidence

        records.append(
            {
                "date": pd.Timestamp(date),
                "score": round(float(weighted_score), 4),
                "magnitude": round(float(magnitude), 4),
                "article_count": count,
            }
        )

    return records


def build_monthly_intervals(
    start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Build list of (month_start, month_end) intervals."""
    intervals = []
    current = start
    while current <= end:
        if current.month == 12:
            next_month = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)

        month_end = min(
            next_month - timedelta(seconds=1),
            end,
        )
        intervals.append((current, month_end))
        current = next_month

    return intervals


async def main():
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "")
    if not api_key:
        log.error(
            "ALPHAVANTAGE_API_KEY not set. "
            "Get a free key at https://www.alphavantage.co/support/#api-key "
            "then add ALPHAVANTAGE_API_KEY=your_key to .env"
        )
        return

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    intervals = build_monthly_intervals(START_DATE, END_DATE)

    log.info(f"Fetching {len(intervals)} months of data ({START_DATE.strftime('%b %Y')} – {END_DATE.strftime('%b %Y')})")
    log.info(f"Using {len(intervals)} of your 25 daily AlphaVantage API requests")

    all_articles: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for i, (t_from, t_to) in enumerate(intervals):
            log.info(f"[{i + 1}/{len(intervals)}] Fetching {t_from.strftime('%B %Y')}...")
            articles = await fetch_month(session, api_key, t_from, t_to)
            all_articles.extend(articles)

            if i < len(intervals) - 1:
                log.info(f"  Sleeping {RATE_LIMIT_SLEEP}s (rate limit)...")
                await asyncio.sleep(RATE_LIMIT_SLEEP)

    if not all_articles:
        log.error("No articles fetched. Verify your API key and try again.")
        return

    log.info(f"Aggregating {len(all_articles):,} total articles into daily scores...")
    records = aggregate_daily(all_articles)

    if not records:
        log.error("No valid articles with parseable timestamps. Exiting.")
        return

    df = pd.DataFrame(records)
    df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

    df.to_parquet(OUTPUT_FILE, index=False)

    log.info("=" * 50)
    log.info(f"Saved {len(df)} days of sentiment data → {OUTPUT_FILE}")
    log.info(f"Date range : {df['date'].min().date()} to {df['date'].max().date()}")
    log.info(f"Score range: {df['score'].min():.3f} to {df['score'].max():.3f}")
    log.info(f"Avg score  : {df['score'].mean():.3f}")
    log.info(f"Total articles: {df['article_count'].sum():,}")
    log.info("=" * 50)
    log.info("Run backtest now: python run_backtest.py adaptive")


if __name__ == "__main__":
    asyncio.run(main())
