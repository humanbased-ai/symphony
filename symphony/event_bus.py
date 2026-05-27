"""Asyncio-native pub/sub event bus.

The orchestrator calls ``publish`` to broadcast runtime events.  Every active
SSE client, notification service, and test subscriber receives a copy via its
own bounded ``asyncio.Queue``.  Subscribers that fall behind are silently
dropped (the queue raises ``QueueFull``) so a slow consumer never blocks the
orchestrator.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


LOGGER = logging.getLogger(__name__)

# How many events each subscriber queue can buffer before dropping.
_DEFAULT_MAXSIZE = 500


class EventBus:
    """Fan-out pub/sub bus backed by per-subscriber asyncio.Queue instances."""

    def __init__(self, *, maxsize: int = _DEFAULT_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        """Broadcast *event* to all active subscribers.

        Subscribers that have not drained their queues fast enough are silently
        skipped — the event is dropped for that subscriber only.
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                LOGGER.debug("EventBus: subscriber queue full, dropping event %s", event.get("type"))

    def subscriber_count(self) -> int:
        """Number of currently active subscribers."""
        return len(self._subscribers)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Context manager that yields a queue and removes it on exit.

        Usage::

            async with event_bus.subscribe() as q:
                event = await q.get()
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.append(q)
        try:
            yield q
        finally:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


def event_to_sse_data(event: dict[str, Any]) -> str:
    """Serialize *event* as an SSE ``data:`` line pair."""
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
