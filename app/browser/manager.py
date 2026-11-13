"""Opening job postings in the browser your settings choose.

There are two places a posting can open:

* Job App's browser window ("dedicated"): a separate Chrome window that Job App
  starts and controls, with its own profile in ``data/browser_profile``. This
  is the window the agents fill applications in. It stays open between
  postings, and if you close it, the next posting opens a fresh one.
* Your own Chrome ("own_chrome"): a new tab in the Chrome you use every day,
  with your own bookmarks and sign-ins. Job App only opens the tab; it cannot
  type into that Chrome (see docs/CHECKPOINTS.md for why, and for the helper
  extension that will make autofill possible there).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..commands import cmd
from ..window import find_chrome
from .launch import BrowserLaunchError, launch_dedicated

log = logging.getLogger("jobapp.browser")

MODES = ("dedicated", "own_chrome")
WHERE = {"dedicated": "Job App's browser window", "own_chrome": "your Chrome"}
BROWSER_NAMES = {"chrome": "Chrome", "msedge": "Edge", "chromium": "the test browser"}

# How long to wait for a page to start loading before giving up on reporting it.
# The tab stays open either way, so a slow site still finishes loading.
COMMIT_TIMEOUT_MS = 20_000


class OpenFailed(Exception):
    """The page could not be opened at all. The message is written for the user."""


@dataclass
class Opened:
    where: str           # "dedicated" or "own_chrome"
    message: str         # what the dashboard tells you
    loaded: bool = True  # False if the tab opened but the page did not load


def _web_link(url: str) -> str:
    # Only ordinary web links are opened. This also keeps a link from ever
    # being read as a command-line switch when it is handed to chrome.exe.
    text = (url or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        raise OpenFailed("That link is not a web address, so it was not opened.")
    return text


def _first_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return lines[0][:240] if lines else type(exc).__name__


# --- your own Chrome ----------------------------------------------------------------


def open_in_own_chrome(url: str) -> str:
    """Open a tab in the Chrome you use every day. Returns which browser was used."""
    exe = find_chrome()
    if exe is not None:
        try:
            # Chrome hands the link to the copy that is already running, which
            # opens it as a new tab in your most recent window. On macOS the
            # system's own "open" does that for an app bundle.
            bundle = next((p for p in exe.parents if p.suffix == ".app"), None)
            command = ["open", "-a", str(bundle), url] if sys.platform == "darwin" and bundle else [str(exe), url]
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            return "chrome"
        except OSError as exc:
            log.warning("Could not start Chrome: %s", exc)
    if webbrowser.open(url):
        return "default"
    raise OpenFailed("Chrome could not be started, and no default browser answered either.")


# --- Job App's browser window ------------------------------------------------------------


class DedicatedWindow:
    """Keeps one dedicated browser open and reuses it."""

    def __init__(self, profile_root: Path, *, headless: bool = False):
        self.profile_root = profile_root
        self.headless = headless
        self.channel_used: Optional[str] = None
        self._playwright = None
        self._context = None
        self._fresh = False  # just started, its first empty tab not yet used
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._context is not None

    def _forget(self, *_args) -> None:
        if self._context is not None:
            log.info("The dedicated browser window was closed.")
        self._context = None

    async def _context_for(self, channel: str):
        if self._context is not None:
            return self._context
        if self._playwright is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise OpenFailed(
                    f"The browser part of Job App is not installed. Run '{cmd('setup')}' again."
                ) from exc
            self._playwright = await async_playwright().start()
        try:
            context, used = await launch_dedicated(
                self._playwright, self.profile_root, channel, headless=self.headless,
            )
        except BrowserLaunchError as exc:
            first = next(iter(exc.attempts.values()), "")
            raise OpenFailed(
                f"Job App's browser window could not be opened. Run '{cmd('check')}' to see why."
                + (f" ({first})" if first else "")
            ) from exc
        context.on("close", self._forget)
        self._context, self.channel_used, self._fresh = context, used, True
        log.info("Opened the dedicated browser (%s).", used)
        return context

    async def _tab(self, context):
        # A freshly started window has one empty tab: use it rather than
        # leaving it lying around next to the posting. After that, every
        # posting gets its own tab.
        if self._fresh:
            self._fresh = False
            for page in context.pages:
                if not page.is_closed() and page.url in ("", "about:blank"):
                    return page
        return await context.new_page()

    async def open(self, url: str, channel: str = "chrome"):
        """Open the link in a tab of the dedicated window. Returns (page, load error or "")."""
        async with self._lock:
            page = None
            for attempt in (1, 2):
                context = await self._context_for(channel)
                try:
                    page = await self._tab(context)
                    break
                except Exception as exc:  # noqa: BLE001 - usually: you closed the window just now
                    if attempt == 2:
                        raise OpenFailed(f"Job App's browser window stopped responding. ({_first_line(exc)})") from exc
                    log.info("Dedicated browser went away (%s); opening a new one.", _first_line(exc))
                    self._context = None
        problem = ""
        try:
            await page.goto(url, wait_until="commit", timeout=COMMIT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - the tab stays open showing the browser's own error
            problem = _first_line(exc)
            log.info("Posting did not load in the dedicated browser: %s", problem)
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001 - only cosmetic
            pass
        return page, problem

    async def current_url(self) -> Optional[str]:
        """The web page you are looking at in this window: the tab that has focus, else the newest one."""
        if self._context is None:
            return None
        pages = [p for p in self._context.pages if not p.is_closed() and p.url.startswith(("http://", "https://"))]
        for page in pages:
            try:
                if await page.evaluate("() => document.hasFocus()"):
                    return page.url
            except Exception:  # noqa: BLE001 - a page that is navigating
                continue
        return pages[-1].url if pages else None

    async def close(self) -> None:
        async with self._lock:
            context, self._context = self._context, None
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001 - already gone
                    pass
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None


# --- the part the server talks to ------------------------------------------------------------


class Browsers:
    """Opens links for the dashboard, in whichever browser the settings choose."""

    def __init__(
        self,
        profile_root: Path,
        *,
        headless: bool = False,
        own_chrome: Callable[[str], str] = open_in_own_chrome,
        chrome_finder: Callable[[], Optional[Path]] = find_chrome,
    ):
        self.window = DedicatedWindow(profile_root, headless=headless)
        self._own_chrome = own_chrome
        self._chrome_finder = chrome_finder

    def info(self) -> dict:
        return {
            "window_open": self.window.is_open,
            "window_browser": BROWSER_NAMES.get(self.window.channel_used or "", None),
            "chrome_found": self._chrome_finder() is not None,
        }

    async def current_url(self):
        return await self.window.current_url()

    async def open(self, url: str, *, mode: str, channel: str = "chrome") -> Opened:
        url = _web_link(url)
        if mode == "own_chrome":
            used = await asyncio.to_thread(self._own_chrome, url)
            text = "Opened in your Chrome." if used == "chrome" else "Opened in your default browser."
            return Opened(where="own_chrome", message=text)
        if mode != "dedicated":
            raise OpenFailed("The browser setting is not one Job App knows. Choose one in Settings.")
        _page, problem = await self.window.open(url, channel)
        text = "Opened in Job App's browser window."
        used = self.window.channel_used
        if used and used != channel:
            text = f"Opened in Job App's browser window, using {BROWSER_NAMES.get(used, used)}."
        if problem:
            return Opened(
                where="dedicated", loaded=False,
                message=f"{text[:-1]}, but the page did not load. The tab shows the browser's own message.",
            )
        return Opened(where="dedicated", message=text)

    async def close(self) -> None:
        await self.window.close()
