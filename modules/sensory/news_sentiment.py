"""News sentiment feed — fetches Indian market headlines from RSS feeds and
Alpha Vantage, scores them with Claude Haiku, then publishes SENTIMENT events
on the bus.

Sources (fetched in parallel):
  1. RSS feeds  — ET Markets, Moneycontrol, Business Standard (~5 min delay)
  2. Alpha Vantage NEWS_SENTIMENT — global macro: Fed, oil, geopolitical
  3. NewsAPI (fallback only) — used when both primary sources return nothing
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp
import anthropic
import feedparser

from core.config import settings
from core.event_bus import EventType, event_bus
from core.logger import get_logger
from modules.sensory.market_hours import now_ist

log = get_logger("news_sentiment")

# IST offset
_IST = timezone(timedelta(hours=5, minutes=30))

# ── RSS feeds — Indian financial news ────────────────────────────────────────
_RSS_FEEDS = [
    "https://economictimes.indiatimes.com/rssfeedstopstories.cms",          # ET Top Stories
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",  # ET Markets
    "https://www.livemint.com/rss/markets",                                 # Livemint Markets
]
_RSS_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# ── Alpha Vantage — global macro news ────────────────────────────────────────
_AV_NEWS_URL = "https://www.alphavantage.co/query"
_AV_TOPICS = "economy_monetary,financial_markets,economy_macro"

# ── NewsAPI — legacy fallback ─────────────────────────────────────────────────
_NEWS_URL = "https://newsapi.org/v2/everything"
_QUERY = (
    "Nifty OR Sensex OR NSE OR BSE OR RBI OR "
    "India budget OR India GDP OR India inflation OR "
    "India economy OR Dalal Street"
)

_SYSTEM_PROMPT = """You are a financial market sentiment analyst specializing in Indian markets.

You will receive a list of recent news headlines. Analyze their collective impact on TWO asset classes traded in India:
1. Indian equity markets (Nifty50 index futures on NSE)
2. Commodities traded on MCX (gold, silver, crude oil)

IMPORTANT: These asset classes often diverge. Classic examples:
- Geopolitical tension (Iran, Ukraine, Middle East) → bearish equities (risk-off), BUT bullish gold/silver (safe-haven) and crude (supply risk)
- US Fed rate pause / dollar weakness → bearish equities, bullish gold
- Strong India GDP / RBI rate cut → bullish equities, neutral commodities
- Risk-on rally / trade deal → bullish equities, bearish gold

Respond ONLY with valid JSON in this exact format:
{
  "equity": {
    "score": <float between -1.0 and 1.0>,
    "magnitude": <float between 0.0 and 1.0>
  },
  "commodity": {
    "score": <float between -1.0 and 1.0>,
    "magnitude": <float between 0.0 and 1.0>
  },
  "summary": "<one sentence max 100 chars>"
}

Where:
- equity.score: -1.0 = very bearish Nifty, 0.0 = neutral, +1.0 = very bullish Nifty
- commodity.score: -1.0 = bearish gold/silver/crude, 0.0 = neutral, +1.0 = bullish commodities
- magnitude: 0.0 = weak/irrelevant news, 1.0 = high-impact market-moving news
- summary: concise summary of the dominant theme across all headlines

Consider as HIGH magnitude (>0.7): RBI rate decisions, Union Budget, geopolitical shocks, Fed decisions, oil supply disruptions.
Consider as LOW magnitude (<0.3): routine corporate news, minor data releases, speculative articles."""


class NewsSentimentFeed:
    """Periodically fetches news and publishes SENTIMENT events."""

    def __init__(self):
        self._last_equity_score: float = 0.0
        self._last_equity_magnitude: float = 0.0
        self._last_commodity_score: float = 0.0
        self._last_commodity_magnitude: float = 0.0
        self._last_summary: str = "No news analyzed yet"
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._running = False

    async def start(self):
        """Start the background news polling loop."""
        if not settings.ANTHROPIC_API_KEY:
            log.warning("ANTHROPIC_API_KEY not set — news sentiment disabled")
            return

        self._running = True
        asyncio.create_task(self._news_loop())
        log.info(
            f"News sentiment feed started (interval: {settings.NEWS_FETCH_INTERVAL_MIN} min)"
        )

    async def stop(self):
        self._running = False

    async def _news_loop(self):
        """Main polling loop — fetch every NEWS_FETCH_INTERVAL_MIN minutes."""
        interval_secs = settings.NEWS_FETCH_INTERVAL_MIN * 60

        while self._running:
            try:
                await self._fetch_and_analyze()
            except Exception as e:
                log.warning(f"News loop error: {e}")

            await asyncio.sleep(interval_secs)

    # ── Fetch methods ─────────────────────────────────────────────────────────

    async def _fetch_rss_headlines(self) -> list[str]:
        """Fetch India-specific headlines from RSS feeds (~5 min delay, free, no key needed)."""
        headlines = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=8)

        async with aiohttp.ClientSession(headers=_RSS_HEADERS) as session:
            for url in _RSS_FEEDS:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            log.debug(f"RSS {url.split('/')[2]} returned {resp.status}")
                            continue
                        content = await resp.text()
                    # Parse XML content string (no network call, no blocking)
                    feed = feedparser.parse(content)
                    for entry in feed.entries[:10]:
                        title = entry.get("title", "").strip()
                        if not title:
                            continue
                        # Skip entries older than 8 hours
                        published = entry.get("published_parsed")
                        if published:
                            pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                            if pub_dt < cutoff:
                                continue
                        headlines.append(title)
                except Exception as e:
                    log.debug(f"RSS fetch failed ({url.split('/')[2]}): {e}")

        return headlines

    async def _fetch_alphavantage_headlines(self) -> list[str]:
        """Fetch global macro headlines from Alpha Vantage (Fed, oil, geopolitical).

        Retries once if the first response returns an empty feed.
        """
        if not settings.ALPHAVANTAGE_API_KEY:
            return []

        params = {
            "function": "NEWS_SENTIMENT",
            "topics": _AV_TOPICS,
            "sort": "LATEST",
            "limit": "30",
            "apikey": settings.ALPHAVANTAGE_API_KEY,
        }

        for attempt in range(2):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        _AV_NEWS_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status != 200:
                            log.debug(f"Alpha Vantage news returned {resp.status}")
                            return []
                        data = await resp.json()
            except Exception as e:
                log.debug(f"Alpha Vantage fetch failed: {e}")
                return []

            headlines = [
                article.get("title", "").strip()
                for article in data.get("feed", [])
                if article.get("title", "").strip()
            ][:15]

            if headlines:
                return headlines

            if attempt == 0:
                log.debug("Alpha Vantage returned empty feed — retrying in 5s")
                await asyncio.sleep(5)

        log.debug("Alpha Vantage returned empty feed after retry — falling back to RSS only")
        return []

    async def _fetch_newsapi_headlines(self) -> list[str]:
        """Legacy NewsAPI fetch — last-resort fallback only."""
        if not settings.NEWS_API_KEY:
            return []

        from_time = (datetime.now(timezone.utc) - timedelta(hours=28)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        params = {
            "q": _QUERY,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10,
            "from": from_time,
            "apiKey": settings.NEWS_API_KEY,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _NEWS_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    log.warning(f"NewsAPI returned {resp.status}")
                    return []
                data = await resp.json()
                return [
                    a.get("title", "").strip()
                    for a in data.get("articles", [])
                    if a.get("title", "").strip() and a.get("title") != "[Removed]"
                ]

    async def _fetch_headlines(self) -> list[str]:
        """Fetch headlines from RSS + Alpha Vantage in parallel, deduplicate."""
        rss_headlines, av_headlines = await asyncio.gather(
            self._fetch_rss_headlines(),
            self._fetch_alphavantage_headlines(),
        )

        # Deduplicate by normalised title
        seen: set[str] = set()
        combined: list[str] = []
        for h in rss_headlines + av_headlines:
            key = h.lower().strip(".,!?\"' ")
            if key not in seen:
                seen.add(key)
                combined.append(h)

        log.debug(
            f"Headlines: {len(rss_headlines)} RSS + {len(av_headlines)} AV "
            f"= {len(combined)} unique"
        )

        # NewsAPI fallback: only when both primary sources returned nothing
        if not combined:
            log.debug("Primary sources empty — falling back to NewsAPI")
            combined = await self._fetch_newsapi_headlines()

        return combined[:20]  # cap for Claude context window

    # ── Sentiment analysis ────────────────────────────────────────────────────

    async def _analyze_sentiment(self, headlines: list[str]) -> dict:
        """Send headlines to Claude Haiku for dual equity/commodity sentiment scoring."""
        if not headlines:
            return {
                "equity_score": 0.0, "equity_magnitude": 0.0,
                "commodity_score": 0.0, "commodity_magnitude": 0.0,
                "summary": "No relevant news found",
            }

        headlines_text = "\n".join(f"- {h}" for h in headlines)
        user_message = f"Analyze the following {len(headlines)} recent headlines:\n\n{headlines_text}"

        message = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = message.content[0].text.strip()

        # Parse JSON — handle markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)

        equity = result.get("equity", {})
        commodity = result.get("commodity", {})

        return {
            "equity_score": max(-1.0, min(1.0, float(equity.get("score", 0.0)))),
            "equity_magnitude": max(0.0, min(1.0, float(equity.get("magnitude", 0.0)))),
            "commodity_score": max(-1.0, min(1.0, float(commodity.get("score", 0.0)))),
            "commodity_magnitude": max(0.0, min(1.0, float(commodity.get("magnitude", 0.0)))),
            "summary": str(result.get("summary", ""))[:120],
        }

    # ── Main pipeline ─────────────────────────────────────────────────────────

    async def _fetch_and_analyze(self):
        """Full pipeline: fetch → analyze → publish."""
        log.debug("Fetching news headlines...")

        headlines = await self._fetch_headlines()

        if not headlines:
            log.debug("No new headlines found — keeping last score")
            return

        try:
            result = await self._analyze_sentiment(headlines)
        except Exception as e:
            log.warning(f"Sentiment analysis failed: {e} — keeping last score")
            return

        self._last_equity_score = result["equity_score"]
        self._last_equity_magnitude = result["equity_magnitude"]
        self._last_commodity_score = result["commodity_score"]
        self._last_commodity_magnitude = result["commodity_magnitude"]
        self._last_summary = result["summary"]

        ts = now_ist()

        await event_bus.publish(EventType.SENTIMENT, {
            "symbol": "EQUITY",
            "score": self._last_equity_score,
            "magnitude": self._last_equity_magnitude,
            "summary": self._last_summary,
            "headline_count": len(headlines),
            "timestamp": ts,
        })

        await event_bus.publish(EventType.SENTIMENT, {
            "symbol": "COMMODITY",
            "score": self._last_commodity_score,
            "magnitude": self._last_commodity_magnitude,
            "summary": self._last_summary,
            "headline_count": len(headlines),
            "timestamp": ts,
        })

        log.info(
            f"Sentiment equity={self._last_equity_score:+.2f}/{self._last_equity_magnitude:.2f} "
            f"commodity={self._last_commodity_score:+.2f}/{self._last_commodity_magnitude:.2f} "
            f"headlines={len(headlines)} | {self._last_summary}"
        )

    async def test_fetch(self):
        """Standalone test — call directly to verify API connectivity."""
        log.info("Running test fetch...")
        await self._fetch_and_analyze()
        log.info(
            f"Result: equity={self._last_equity_score:+.2f}/{self._last_equity_magnitude:.2f} "
            f"commodity={self._last_commodity_score:+.2f}/{self._last_commodity_magnitude:.2f} "
            f"| {self._last_summary}"
        )
