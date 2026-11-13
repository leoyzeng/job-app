"""Re-reading a form every few seconds while it waits for you (PLAN.md 8.2).

If the browser is closed or the computer restarts while you are typing into an
application, what you had typed up to the last reading is still known.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from .model import Reading
from .read import read_page

log = logging.getLogger("jobapp.forms")


class FormWatcher:
    def __init__(self, page, *, interval: float = 4.0,
                 on_reading: Optional[Callable[[Reading], Awaitable[None] | None]] = None):
        self.page = page
        self.interval = interval
        self.on_reading = on_reading
        self.latest: Optional[Reading] = None
        self.closed = False
        self.readings = 0
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            if self.page.is_closed():
                self.closed = True
                return
            try:
                reading = await read_page(self.page)
            except Exception as exc:  # noqa: BLE001 - the page may be navigating; try again next time
                if self.page.is_closed():
                    self.closed = True
                    return
                log.debug("Watcher could not read the page: %s", exc)
            else:
                self.latest = reading
                self.readings += 1
                if self.on_reading is not None:
                    result = self.on_reading(reading)
                    if asyncio.iscoroutine(result):
                        await result
            await asyncio.sleep(self.interval)
