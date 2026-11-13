"""Opening the dedicated browser: the app's own window, apart from your Chrome.

It uses your installed Chrome (or Edge) with a profile folder inside
``data/browser_profile``. Sign-ins made in that window are kept there between
runs, and nothing in your everyday Chrome profile is touched.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("jobapp.browser")


class BrowserLaunchError(Exception):
    def __init__(self, attempts: dict[str, str]):
        self.attempts = attempts
        tried = "; ".join(f"{name}: {why}" for name, why in attempts.items())
        super().__init__(f"No browser could be opened. {tried}")


def _first_line(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:300] if text else type(exc).__name__


async def launch_dedicated(playwright, profile_root: Path, channel: str = "chrome", headless: bool = False):
    """Open the dedicated browser. Returns (context, channel actually used).

    Tries the preferred browser first, then the other installed one, then the
    test browser that Playwright can download. Each gets its own profile
    folder, because a profile written by one browser is not safe to open with
    another.
    """
    order = [channel] + [name for name in ("chrome", "msedge", "chromium") if name != channel]
    attempts: dict[str, str] = {}
    for name in order:
        profile = profile_root / name
        profile.mkdir(parents=True, exist_ok=True)
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel=None if name == "chromium" else name,
                headless=headless,
                no_viewport=not headless,  # a visible window uses its real size
                args=["--no-first-run", "--no-default-browser-check"],
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user, next browser tried
            attempts[name] = _first_line(exc)
            log.info("Could not open %s: %s", name, attempts[name])
            continue
        if name != channel:
            log.warning("Preferred browser %s was not available; using %s.", channel, name)
        return context, name
    raise BrowserLaunchError(attempts)
