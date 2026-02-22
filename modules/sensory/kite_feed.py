"""WebSocket tick ingestion from Kite Connect."""

import asyncio
from typing import Dict, Any, Optional
from kiteconnect import KiteConnect, KiteTicker
from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger

log = get_logger("kite_feed")


class KiteFeed:
    """Manages Kite Connect authentication and WebSocket tick feed."""

    def __init__(self):
        self.kite = KiteConnect(api_key=settings.KITE_API_KEY)
        self.ticker: Optional[KiteTicker] = None
        self._instrument_tokens: Dict[str, int] = {}
        self._token_to_symbol: Dict[int, str] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def authenticate(self):
        """Set access token for Kite Connect session."""
        self.kite.set_access_token(settings.KITE_ACCESS_TOKEN)
        log.info("Kite Connect authenticated")

    def generate_session(self, request_token: str) -> Dict[str, Any]:
        """Generate session from request token (for initial login flow)."""
        data = self.kite.generate_session(
            request_token, api_secret=settings.KITE_API_SECRET
        )
        self.kite.set_access_token(data["access_token"])
        log.info("Kite session generated")
        return data

    def resolve_instruments(self):
        """Resolve instrument symbols to tokens."""
        all_instruments = self.kite.instruments()
        for symbol in settings.instruments_list:
            exchange, tradingsymbol = symbol.split(":")
            for inst in all_instruments:
                if (
                    inst["exchange"] == exchange
                    and inst["tradingsymbol"] == tradingsymbol
                ):
                    token = inst["instrument_token"]
                    self._instrument_tokens[symbol] = token
                    self._token_to_symbol[token] = symbol
                    log.info(f"Resolved {symbol} -> token {token}")
                    break
            else:
                log.warning(f"Could not resolve instrument: {symbol}")

        if not self._instrument_tokens:
            raise ValueError("No instruments resolved. Check INSTRUMENTS config.")

    def _on_ticks(self, ws, ticks):
        """Callback when ticks are received — publishes to event bus."""
        for tick in ticks:
            token = tick.get("instrument_token")
            symbol = self._token_to_symbol.get(token, f"UNKNOWN:{token}")
            tick_data = {
                "symbol": symbol,
                "token": token,
                "last_price": tick.get("last_price", 0),
                "volume": tick.get("volume_traded", 0),
                "buy_quantity": tick.get("total_buy_quantity", 0),
                "sell_quantity": tick.get("total_sell_quantity", 0),
                "open": tick.get("ohlc", {}).get("open", 0),
                "high": tick.get("ohlc", {}).get("high", 0),
                "low": tick.get("ohlc", {}).get("low", 0),
                "close": tick.get("ohlc", {}).get("close", 0),
                "change": tick.get("change", 0),
                "timestamp": tick.get("exchange_timestamp"),
            }
            # Schedule the coroutine on the event loop
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    event_bus.publish(EventType.TICK, tick_data), self._loop
                )

    def _on_connect(self, ws, response):
        """Callback on WebSocket connect — subscribe to instruments."""
        tokens = list(self._instrument_tokens.values())
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        log.info(f"Subscribed to {len(tokens)} instruments: {list(self._instrument_tokens.keys())}")

    def _on_close(self, ws, code, reason):
        """Callback on WebSocket close."""
        log.warning(f"WebSocket closed: code={code}, reason={reason}")

    def _on_error(self, ws, code, reason):
        """Callback on WebSocket error."""
        log.error(f"WebSocket error: code={code}, reason={reason}")

    def _on_reconnect(self, ws, attempts_count):
        """Callback on reconnect attempts."""
        log.info(f"WebSocket reconnecting... attempt {attempts_count}")

    async def start(self):
        """Start the Kite WebSocket feed in a background thread."""
        self._loop = asyncio.get_event_loop()
        self.authenticate()
        self.resolve_instruments()

        self.ticker = KiteTicker(
            settings.KITE_API_KEY, settings.KITE_ACCESS_TOKEN
        )
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_close = self._on_close
        self.ticker.on_error = self._on_error
        self.ticker.on_reconnect = self._on_reconnect

        log.info("Starting Kite WebSocket feed...")
        # KiteTicker.connect() blocks, so run in thread
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.ticker.connect(threaded=True)
        )

    async def stop(self):
        """Stop the WebSocket feed."""
        if self.ticker:
            self.ticker.close()
            log.info("Kite WebSocket feed stopped")

    def get_kite(self) -> KiteConnect:
        """Return the authenticated KiteConnect instance."""
        return self.kite
