"""Central async pub/sub event bus for inter-module communication."""

import asyncio
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List
from core.logger import get_logger

log = get_logger("event_bus")


class EventType(str, Enum):
    TICK = "TICK"
    CANDLE = "CANDLE"
    SIGNAL = "SIGNAL"
    ORDER = "ORDER"
    TRADE = "TRADE"
    ALERT = "ALERT"
    HEARTBEAT = "HEARTBEAT"
    POSITION_UPDATE = "POSITION_UPDATE"
    KILL_SWITCH = "KILL_SWITCH"
    SHUTDOWN = "SHUTDOWN"
    SENTIMENT = "SENTIMENT"


# Type alias for subscriber callbacks
Subscriber = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


class EventBus:
    """Simple asyncio-based pub/sub event bus."""

    def __init__(self):
        self._subscribers: Dict[EventType, List[Subscriber]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    def subscribe(self, event_type: EventType, callback: Subscriber):
        """Register a callback for an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
        log.debug(f"Subscribed {callback.__qualname__} to {event_type.value}")

    def unsubscribe(self, event_type: EventType, callback: Subscriber):
        """Remove a callback from an event type."""
        if event_type in self._subscribers:
            self._subscribers[event_type] = [
                cb for cb in self._subscribers[event_type] if cb != callback
            ]

    async def publish(self, event_type: EventType, data: Dict[str, Any]):
        """Publish an event to all subscribers."""
        await self._queue.put((event_type, data))

    async def _dispatch(self, event_type: EventType, data: Dict[str, Any]):
        """Dispatch event to all registered subscribers."""
        subscribers = self._subscribers.get(event_type, [])
        if not subscribers:
            return

        tasks = []
        for callback in subscribers:
            try:
                tasks.append(asyncio.create_task(callback(data)))
            except Exception as e:
                log.error(f"Error creating task for {callback.__qualname__}: {e}")

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    log.error(
                        f"Subscriber {subscribers[i].__qualname__} raised: {result}"
                    )

    async def start(self):
        """Start the event bus processing loop."""
        self._running = True
        log.info("Event bus started")
        while self._running:
            try:
                event_type, data = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
                await self._dispatch(event_type, data)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error(f"Event bus error: {e}")

    async def stop(self):
        """Stop the event bus."""
        self._running = False
        # Drain remaining events
        while not self._queue.empty():
            try:
                event_type, data = self._queue.get_nowait()
                await self._dispatch(event_type, data)
            except asyncio.QueueEmpty:
                break
        log.info("Event bus stopped")


# Singleton
event_bus = EventBus()
