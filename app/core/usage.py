"""Today's agent usage, and the daily cap (PLAN.md 4.6, budget_usd.per_day).

Every agent run writes a line to ``data/logs/agents.log`` with what the
runtime reports it cost. With a Claude plan sign-in nothing is charged per
run: the figure is the runtime's estimate, used here as a measure of how much
of your plan's limits Job App has used today. When today's total reaches the
cap, searches and applications do not start until tomorrow (or until you
raise the cap in Settings).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import Settings

log = logging.getLogger("jobapp.usage")


def entry_cost(entry: dict) -> float:
    """What one agents.log line says the run cost."""
    if isinstance(entry.get("cost_usd"), (int, float)):
        return float(entry["cost_usd"])
    total = 0.0
    for attempt in entry.get("attempts") or []:
        if isinstance(attempt, dict) and isinstance(attempt.get("cost_usd"), (int, float)):
            total += float(attempt["cost_usd"])
    return total


class Usage:
    def __init__(self, log_path: Path, load_settings: Callable[[], Settings]):
        self.log_path = log_path
        self.load_settings = load_settings
        self._cache: tuple = (None, 0.0)

    def today(self) -> float:
        day = datetime.now().astimezone().date().isoformat()
        try:
            stat = self.log_path.stat()
        except FileNotFoundError:
            return 0.0
        key = (day, stat.st_mtime_ns, stat.st_size)
        if self._cache[0] == key:
            return self._cache[1]
        total = 0.0
        try:
            with self.log_path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if day not in line[:40]:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if str(entry.get("at", ""))[:10] == day:
                        total += entry_cost(entry)
        except OSError as exc:
            log.warning("Could not read agents.log: %s", exc)
            return self._cache[1]
        total = round(total, 4)
        self._cache = (key, total)
        return total

    def problem(self) -> str:
        """Why agents should not start now, or "" if they may."""
        cap = self.load_settings().budget_usd.per_day
        if cap <= 0:
            return ""
        used = self.today()
        if used >= cap:
            return (f"Today's agent usage (about ${used:.2f}) has reached your daily cap of ${cap:.2f}. "
                    "Raise the cap in Settings, or carry on tomorrow.")
        return ""

    def view(self) -> dict:
        cap = self.load_settings().budget_usd.per_day
        used = self.today()
        return {"today_usd": used, "cap_usd": cap, "reached": cap > 0 and used >= cap}
