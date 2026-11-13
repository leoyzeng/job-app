"""The daily search (Settings: "Search every day at").

While Job App is running it checks once a minute. When the chosen time has
passed and today's search has not run yet, it starts one, just as the
"Search for new jobs" button would. If Job App is started later in the day,
the search runs then. It runs at most once a day, and only through the same
checks as the button (a confirmed profile, the daily usage cap, no search
already running).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from ..core.events import EventBus
from ..core.jsonfile import JsonCorrupt, read_json, write_json_atomic
from ..core.models import Settings, now_iso
from ..paths import Paths
from .service import SearchRefused

log = logging.getLogger("jobapp.schedule")


class DailySearch:
    def __init__(self, paths: Paths, searcher, load_settings: Callable[[], Settings], bus: EventBus,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.path = paths.data / "schedule.json"
        self.searcher = searcher
        self.load_settings = load_settings
        self.bus = bus
        self.clock = clock
        self._task: Optional[asyncio.Task] = None

    def _read(self) -> dict:
        try:
            data = read_json(self.path)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, JsonCorrupt):
            return {}

    def view(self) -> dict:
        data = self._read()
        return {"daily_at": self.load_settings().search.daily_at, "last_day": data.get("last_day", ""),
                "last_result": data.get("last_result", ""), "last_at": data.get("last_at", "")}

    def due(self) -> bool:
        at = self.load_settings().search.daily_at
        if not at:
            return False
        now = self.clock()
        if now.strftime("%H:%M") < at:
            return False
        return self._read().get("last_day") != now.date().isoformat()

    async def tick(self) -> Optional[str]:
        """Start today's search if it is due. Returns what happened, or None if it was not due."""
        if not self.due():
            return None
        try:
            self.searcher.start()
            result = "started"
        except SearchRefused as exc:
            result = f"not started: {exc}"
        write_json_atomic(self.path, {"last_day": self.clock().date().isoformat(), "last_result": result,
                                      "last_at": now_iso()}, backup=False)
        log.info("Daily search %s", result)
        self.bus.publish("search_changed")
        return result

    def start(self, interval: float = 60) -> None:
        async def loop():
            await asyncio.sleep(5)       # let the app finish starting
            while True:
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 - never stop the loop
                    log.exception("Daily search check failed")
                await asyncio.sleep(interval)

        self._task = asyncio.create_task(loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
