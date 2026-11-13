"""Text for bug reports ("Copy diagnostics" in Settings).

It says how this copy of Job App is set up and what failed recently, and
nothing about you: no resume, profile, answers, jobs, companies, links, page
contents, keys, email addresses or folder names. Nothing is sent anywhere;
you paste it yourself.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from typing import Optional

from .. import __version__
from .models import Settings

PACKAGES = ("claude-agent-sdk", "playwright", "starlette", "uvicorn", "pydantic", "pypdf", "python-docx", "keyring")


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def _recent_agent_failures(log: Path, limit: int = 10) -> list[str]:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return []
    found = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("ok") is False:
            found.append(f"{str(entry.get('at', ''))[:16]} {entry.get('agent')}: {entry.get('kind')}")
        if len(found) >= limit:
            break
    return found


def _last_check(logs: Path) -> list[str]:
    reports = sorted(logs.glob("check-*.json"))
    if not reports:
        return ["no check run yet"]
    try:
        data = json.loads(reports[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["last check report unreadable"]
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return ["last check report has no results"]
    return [f"{r.get('name')}: {r.get('status')}" for r in results if isinstance(r, dict)]


def collect(*, settings: Settings, logs: Path, signed_in: Optional[bool], counts: dict,
            browsers: dict, demo: bool) -> str:
    lines = [
        f"Job App {__version__}{' (sample data)' if demo else ''}",
        f"System: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python: {sys.version.split()[0]}",
        "Packages: " + ", ".join(f"{name} {_version(name)}" for name in PACKAGES),
        f"Browsers found: {', '.join(name for name, found in browsers.items() if found) or 'none'}",
        f"Sign-in: {settings.auth.mode}, signed in: {signed_in}",
        "Settings: " + ", ".join(f"{key}={value}" for key, value in {
            "browser": settings.browser.mode,
            "channel": settings.browser.channel,
            "pause_after_each_page": settings.pause_after_each_page,
            "write_long_answers": settings.write_long_answers,
            "never_click_next": settings.never_click_next,
            "match_questions_by_meaning": settings.match_questions_by_meaning,
            "parallel": settings.max_parallel_applications,
            "models": f"{settings.models.autofill}/{settings.models.search}/{settings.models.writer}",
            "budget": f"{settings.budget_usd.per_day}/{settings.budget_usd.per_application}/{settings.budget_usd.per_search}",
            "sources_off": sorted(k for k, v in settings.search.sources.items() if not v),
            "watchlist_size": len(settings.search.watchlist),
        }.items()),
        "Jobs: " + ", ".join(f"{group} {count}" for group, count in counts.items()),
        "Last check: " + "; ".join(_last_check(logs)),
    ]
    failures = _recent_agent_failures(logs / "agents.log")
    lines.append("Recent agent failures: " + ("; ".join(failures) if failures else "none"))
    return "\n".join(lines)

