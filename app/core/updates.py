"""Tell you when a newer version of Job App is published.

Once a day, if Settings allows it, Job App asks GitHub for the latest release
of the repository it was cloned from (read from ``.git/config``, or set
``REPOSITORY`` in ``app/__init__.py``). Nothing about you is sent: the request
carries only Job App's version in its User-Agent. Nothing is installed; the
notice tells you to ``git pull``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .. import REPOSITORY, __version__
from .jsonfile import read_json, write_json_atomic

log = logging.getLogger("jobapp.updates")

DAY = 24 * 60 * 60
_GITHUB = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def repository(program: Path) -> Optional[str]:
    """'owner/name' of the GitHub repository this copy came from, if known."""
    if REPOSITORY:
        return REPOSITORY
    try:
        config = (program / ".git" / "config").read_text(encoding="utf-8")
    except OSError:
        return None
    in_origin = False
    for line in config.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_origin = line.replace(" ", "") in ('[remote"origin"]',)
        elif in_origin and line.startswith("url"):
            match = _GITHUB.search(line.split("=", 1)[-1].strip())
            if match:
                return f"{match.group(1)}/{match.group(2)}"
    return None


def version_tuple(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", (text or "").split("-", 1)[0])
    return tuple(int(n) for n in numbers[:3]) or (0,)


def newer(latest: str, current: str = __version__) -> bool:
    return version_tuple(latest) > version_tuple(current)


def _fetch_latest(repo: str) -> Optional[dict]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"JobApp/{__version__}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # no releases yet
            return {}
        raise
    return {"tag": str(data.get("tag_name") or ""), "url": str(data.get("html_url") or "")}


def check(cache: Path, program: Path, fetch=_fetch_latest, now: Optional[float] = None) -> Optional[dict]:
    """Look at most once a day. Returns {"tag", "url"} when a newer release exists, else None."""
    now = time.time() if now is None else now
    try:
        saved = read_json(cache)
    except Exception:  # noqa: BLE001 - missing or damaged: look again
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    if now - float(saved.get("checked_at") or 0) >= DAY:
        repo = repository(program)
        if not repo:
            return None
        try:
            latest = fetch(repo) or {}
        except Exception as exc:  # noqa: BLE001 - offline, rate-limited: try tomorrow
            log.info("Could not check for a new version: %s", exc)
            latest = {}
        saved = {"checked_at": now, "repo": repo, **latest}
        try:
            write_json_atomic(cache, saved, backup=False)
        except OSError:
            pass
    tag = saved.get("tag") or ""
    if tag and newer(tag):
        return {"tag": tag, "url": saved.get("url") or ""}
    return None
