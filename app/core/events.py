"""Live updates: the server announces a change, open dashboards hear it."""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._listeners: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._listeners.discard(queue)

    def publish(self, kind: str, **payload: Any) -> None:
        event = {"type": kind, **payload}
        for queue in list(self._listeners):
            if queue.full():
                # A dashboard that stopped listening: drop its oldest event
                # so a stuck page can never hold up the others.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)
