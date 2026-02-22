"""News sentiment feed — fetches Indian market headlines from NewsAPI and
scores them with Claude Haiku, then publishes SENTIMENT events on the bus."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp
import anthropic

from core.config import settings
from core.event_bus import EventType, event_bus
from core.logger import get_logger
from modules.sensory.market_hours import now_ist

log = get_logger("news_sentiment")

# IST offset
_IST = timezone(timedelta(hours=5, minutes=30))

# NewsAPI endpoint
_NEWS_URL = "https://newsapi.org/v2/everything"

# Topics relevant to Nifty / Indian macro
_QUERY = (
    "Nifty OR Sensex OR NSE OR BSE OR RBI OR "
    "India budget OR India GDP OR India inflation OR "
    "India economy OR Dalal Street"
)

_SYSTEM_PROMPT = """You are a financial market sentiment analyst specializing in Indian equity markets (Nifty50 index futures).

You will receive a list of recent news headlines. Analyze their collective impact on Nifty50 futures trading sentiment.

Respond ONLY with valid JSON in this exact format:
{
  "overall": <float between -1.0 and 1.0>,
  "magnitude": <float between 0.0 and 1.0>,
  "summary": "<one sentence max 100 chars>"
}

Where:
- overall: -1.0 = very bearish (sell Nifty), 0.0 = neutral, +1.0 = very bullish (buy Nifty)
- magnitude: 0.0 = weak/irrelevant news, 1.0 = high-impact market-moving news
- summary: concise summary of dominant theme

Consider as HIGH magnitude (>0.7): RBI rate decisions, Union Budget, geopolitical shocks, market circuit breakers, major corporate events.
Consider as LOW magnitude (<0.3): routine corporate news, minor data releases, speculative articles."""


class NewsSentimentFeed:
    """Periodically fetches news and publishes SENTIMENT events."""

    def __init__(self):
        self._last_score: float = 0.0
        self._last_magnitude: float = 0.0
        self._last_summary: str = "No news analyzed yet"
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._running = False

    async def start(self):
        """Start the background news polling loop."""
        if not settings.NEWS_API_KEY:
            log.warning("NEWS_API_KEY not set — news sentiment disabled")
            return
        if not settings.ANTHROPIC_API_KEY:
            log.warning("ANTHROPIC_API_KEY not set — news sentiment disabled")
            return

        self._running = True
        # Run first fetch immediately, then loop
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

    async def _fetch_headlines(self) -> list[str]:
        """Fetch recent Indian market headlines from NewsAPI."""
        # Free plan has ~24h delay; paid plan has real-time access.
        # Use 28h window to handle both tiers.
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
            async with session.get(_NEWS_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.warning(f"NewsAPI returned {resp.status}")
                    return []

                data = await resp.json()
                articles = data.get("articles", [])

                headlines = []
                for a in articles:
                    title = a.get("title", "").strip()
                    if title and title != "[Removed]":
                        headlines.append(title)

                return headlines

    async def _analyze_sentiment(self, headlines: list[str]) -> dict:
        """Send headlines to Claude Haiku for sentiment scoring."""
        if not headlines:
            return {"overall": 0.0, "magnitude": 0.0, "summary": "No relevant news found"}

        headlines_text = "\n".join(f"- {h}" for h in headlines)
        user_message = f"Analyze the following {len(headlines)} recent headlines:\n\n{headlines_text}"

        message = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
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

        # Clamp values to valid ranges
        result["overall"] = max(-1.0, min(1.0, float(result.get("overall", 0.0))))
        result["magnitude"] = max(0.0, min(1.0, float(result.get("magnitude", 0.0))))
        result["summary"] = str(result.get("summary", ""))[:120]

        return result

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

        self._last_score = result["overall"]
        self._last_magnitude = result["magnitude"]
        self._last_summary = result["summary"]

        sentiment_data = {
            "symbol": "MARKET",
            "score": self._last_score,
            "magnitude": self._last_magnitude,
            "summary": self._last_summary,
            "headline_count": len(headlines),
            "timestamp": now_ist(),
        }

        await event_bus.publish(EventType.SENTIMENT, sentiment_data)

        score_str = f"{self._last_score:+.2f}"
        mag_str = f"{self._last_magnitude:.2f}"
        log.info(
            f"Sentiment: score={score_str} magnitude={mag_str} "
            f"headlines={len(headlines)} | {self._last_summary}"
        )

    async def test_fetch(self):
        """Standalone test — call directly to verify API connectivity."""
        log.info("Running test fetch...")
        await self._fetch_and_analyze()
        log.info(
            f"Result: score={self._last_score:+.2f} "
            f"magnitude={self._last_magnitude:.2f} | {self._last_summary}"
        )
