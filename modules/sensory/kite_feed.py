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
        self._consecutive_403s: int = 0
        self._restarting: bool = False

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
        """Resolve instrument symbols to Kite tokens.

        Supports two formats:
        - Exact:   MCX:GOLDPETAL26APRFUT  — resolves directly
        - Generic: MCX:GOLDPETAL          — auto-picks the nearest-expiry FUT contract

        If an exact symbol fails (e.g. contract expired), falls back to front-month
        resolution using the base name so the bot keeps running across rollovers.
        """
        all_instruments = self.kite.instruments()

        for symbol in settings.instruments_list:
            exchange, tradingsymbol = symbol.split(":")

            # Try exact match first
            resolved = next(
                (i for i in all_instruments
                 if i["exchange"] == exchange and i["tradingsymbol"] == tradingsymbol),
                None,
            )

            if resolved:
                token = resolved["instrument_token"]
                self._instrument_tokens[symbol] = token
                self._token_to_symbol[token] = symbol
                log.info(f"Resolved {symbol} -> token {token}")
                continue

            # Exact match failed — treat tradingsymbol as a base name and find front-month FUT.
            # MCX commodity futures have instrument_type "FUTCOM", not "FUT".
            candidates = [
                i for i in all_instruments
                if i["exchange"] == exchange
                and i["instrument_type"] in ("FUT", "FUTCOM")
                and i["name"] == tradingsymbol
                and i.get("expiry")
            ]
            if candidates:
                front = min(candidates, key=lambda i: i["expiry"])
                resolved_symbol = f"{exchange}:{front['tradingsymbol']}"
                token = front["instrument_token"]
                self._instrument_tokens[resolved_symbol] = token
                self._token_to_symbol[token] = resolved_symbol
                log.info(
                    f"Auto-resolved {symbol} -> {resolved_symbol} "
                    f"(expiry {front['expiry']}) token {token}"
                )
            else:
                # Last-chance: NSE:NIFTY with TRADE_OPTIONS=True should live in NFO
                if exchange == "NSE" and tradingsymbol == "NIFTY" and settings.TRADE_OPTIONS:
                    nfo_candidates = [
                        i for i in all_instruments
                        if i["exchange"] == "NFO"
                        and i["instrument_type"] == "FUT"
                        and i["name"] == "NIFTY"
                        and i.get("expiry")
                    ]
                    if nfo_candidates:
                        front = min(nfo_candidates, key=lambda i: i["expiry"])
                        resolved_symbol = f"NFO:{front['tradingsymbol']}"
                        token = front["instrument_token"]
                        self._instrument_tokens[resolved_symbol] = token
                        self._token_to_symbol[token] = resolved_symbol
                        log.warning(
                            f"Auto-coerced NSE:NIFTY → {resolved_symbol} "
                            f"(expiry {front['expiry']}) — update INSTRUMENTS to NFO:NIFTY"
                        )
                        continue
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
        if "403" in str(reason):
            self._consecutive_403s += 1
        else:
            self._consecutive_403s = 0

    def _on_reconnect(self, ws, attempts_count):
        """Callback on reconnect attempts — refresh token if repeated 403s detected."""
        log.info(f"WebSocket reconnecting... attempt {attempts_count}")
        # 2+ consecutive 403s = expired token; refresh and recreate the ticker
        if self._consecutive_403s >= 2 and not self._restarting:
            log.warning(
                f"Repeated 403 errors ({self._consecutive_403s}x) — refreshing Kite token"
            )
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._refresh_and_restart(), self._loop
                )

    async def _refresh_and_restart(self):
        """Refresh the Kite access token and recreate the WebSocket connection."""
        if self._restarting:
            return
        self._restarting = True
        try:
            if self.ticker:
                self.ticker.close()
                await asyncio.sleep(2)

            from modules.sensory.auto_token import refresh_kite_token
            new_token = await asyncio.get_event_loop().run_in_executor(
                None, refresh_kite_token
            )
            if new_token:
                self.kite.set_access_token(new_token)
                log.info("Kite token refreshed — restarting WebSocket")
                await event_bus.publish(
                    EventType.ALERT,
                    {
                        "type": "TOKEN_REFRESH",
                        "message": (
                            f"WebSocket token auto-refreshed after "
                            f"{self._consecutive_403s} consecutive 403 errors. Reconnecting..."
                        ),
                    },
                )
            else:
                log.warning("Token refresh returned None — retrying with existing token")
                await event_bus.publish(
                    EventType.ALERT,
                    {
                        "type": "TOKEN_REFRESH_FAILED",
                        "message": "WebSocket 403 errors — token refresh returned None. Manual intervention may be needed.",
                    },
                )

            self._consecutive_403s = 0
            self.ticker = KiteTicker(settings.KITE_API_KEY, self.kite.access_token)
            self.ticker.on_ticks = self._on_ticks
            self.ticker.on_connect = self._on_connect
            self.ticker.on_close = self._on_close
            self.ticker.on_error = self._on_error
            self.ticker.on_reconnect = self._on_reconnect

            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ticker.connect(threaded=True)
            )
        except Exception as e:
            log.error(f"Token refresh and WebSocket restart failed: {e}")
        finally:
            self._restarting = False

    async def refresh_token(self):
        """Refresh the Kite access token using auto_token (pyotp) if credentials are set.

        Must be called before any Kite API call (e.g. margins fetch in OrderManager).
        Called explicitly in main.py before _init_modules() so that the KiteConnect
        instance passed to OrderManager already has a valid token.
        """
        try:
            from modules.sensory.auto_token import refresh_kite_token
            new_token = await asyncio.get_event_loop().run_in_executor(None, refresh_kite_token)
            if new_token:
                self.kite.set_access_token(new_token)
                log.info("Kite authenticated with auto-refreshed token")
            else:
                self.authenticate()
        except Exception as e:
            log.warning(f"Auto token refresh failed: {e}. Falling back to existing token.")
            self.authenticate()

    async def start(self):
        """Start the Kite WebSocket feed in a background thread."""
        self._loop = asyncio.get_event_loop()

        # Token must already be set by an earlier refresh_token() call (from main.py run()).
        # Re-authenticate here only as a safety net if called in isolation.
        if not self.kite.access_token:
            await self.refresh_token()

        self.resolve_instruments()

        self.ticker = KiteTicker(
            settings.KITE_API_KEY, self.kite.access_token
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

    async def force_reconnect(self):
        """Reconnect the WebSocket without refreshing the token — for silent disconnects."""
        if self._restarting:
            return
        self._restarting = True
        log.warning("WebSocket force-reconnect triggered (dead feed detected)")
        try:
            if self.ticker:
                self.ticker.close()
                await asyncio.sleep(2)
            self.ticker = KiteTicker(settings.KITE_API_KEY, self.kite.access_token)
            self.ticker.on_ticks = self._on_ticks
            self.ticker.on_connect = self._on_connect
            self.ticker.on_close = self._on_close
            self.ticker.on_error = self._on_error
            self.ticker.on_reconnect = self._on_reconnect
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.ticker.connect(threaded=True)
            )
            log.info("WebSocket force-reconnected successfully")
        except Exception as e:
            log.error(f"WebSocket force-reconnect failed: {e}")
        finally:
            self._restarting = False

    async def stop(self):
        """Stop the WebSocket feed."""
        if self.ticker:
            self.ticker.close()
            log.info("Kite WebSocket feed stopped")

    def get_kite(self) -> KiteConnect:
        """Return the authenticated KiteConnect instance."""
        return self.kite
