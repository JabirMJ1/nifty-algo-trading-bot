"""Telegram alert module — sends formatted messages via Telegram Bot API."""

import aiohttp
from typing import Any, Dict
from core.config import settings
from core.event_bus import event_bus, EventType
from core.logger import get_logger

log = get_logger("telegram_alerts")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramAlerts:
    """Sends trading alerts to Telegram."""

    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self._enabled = bool(self.token and self.chat_id)
        self._session: aiohttp.ClientSession = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_message(self, text: str):
        """Send a message to the configured Telegram chat."""
        if not self._enabled:
            log.debug(f"Telegram disabled. Message: {text}")
            return

        url = TELEGRAM_API.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"Telegram send failed ({resp.status}): {body}")
                else:
                    log.debug("Telegram message sent")
        except Exception as e:
            log.error(f"Telegram send error: {e}")

    async def on_alert(self, alert_data: Dict[str, Any]):
        """Handle ALERT events from event bus."""
        alert_type = alert_data.get("type", "INFO")
        message = alert_data.get("message", "")

        formatted = f"<b>[{alert_type}]</b>\n{message}"
        await self.send_message(formatted)

    async def on_trade(self, trade_data: Dict[str, Any]):
        """Send trade notifications."""
        trade_type = trade_data.get("type")

        if trade_type == "ENTRY":
            msg = (
                f"<b>Trade Opened</b>\n"
                f"Direction: {trade_data.get('direction')}\n"
                f"Instrument: {trade_data.get('instrument')}\n"
                f"Price: {trade_data.get('price', 0):.2f}\n"
                f"Qty: {trade_data.get('quantity')}\n"
                f"Strategy: {trade_data.get('strategy')}\n"
                f"SL: {trade_data.get('stop_loss', 0):.2f}\n"
                f"Target: {trade_data.get('target', 0):.2f}"
            )
            await self.send_message(msg)

        elif trade_type == "EXIT":
            trade = trade_data.get("trade", {})
            pnl = trade.get("pnl", trade_data.get("pnl", 0))
            result = "PROFIT" if pnl > 0 else "LOSS"
            msg = (
                f"<b>Trade Closed ({result})</b>\n"
                f"Instrument: {trade.get('instrument', trade_data.get('instrument'))}\n"
                f"PnL: {pnl:.2f}\n"
                f"Reason: {trade_data.get('reason', '')}\n"
                f"Balance: {trade_data.get('balance', 'N/A')}"
            )
            await self.send_message(msg)

    async def send_heartbeat(self):
        """Send a heartbeat message."""
        await self.send_message("<b>[HEARTBEAT]</b> Bot is alive and running.")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def start(self):
        event_bus.subscribe(EventType.ALERT, self.on_alert)
        event_bus.subscribe(EventType.TRADE, self.on_trade)
        status = "enabled" if self._enabled else "disabled (no token/chat_id)"
        log.info(f"Telegram alerts {status}")
