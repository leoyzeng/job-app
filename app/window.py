"""Finding the installed browsers and opening the dashboard window."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Optional

log = logging.getLogger("jobapp.window")


def _first_existing(candidates: list[Path]) -> Optional[Path]:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _windows_candidates(relative: str) -> list[Path]:
    roots = [os.environ.get(name) for name in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")]
    return [Path(root) / relative for root in roots if root]


def _mac_candidates(app_name: str) -> list[Path]:
    """An app bundle's program, in /Applications or the user's own ~/Applications."""
    inside = Path(f"{app_name}.app") / "Contents" / "MacOS" / app_name
    return [Path("/Applications") / inside, Path.home() / "Applications" / inside]


def find_chrome() -> Optional[Path]:
    if sys.platform == "win32":
        return _first_existing(_windows_candidates(r"Google\Chrome\Application\chrome.exe"))
    if sys.platform == "darwin":
        return _first_existing(_mac_candidates("Google Chrome"))
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return Path(found)
    return None


def find_edge() -> Optional[Path]:
    if sys.platform == "win32":
        return _first_existing(_windows_candidates(r"Microsoft\Edge\Application\msedge.exe"))
    if sys.platform == "darwin":
        return _first_existing(_mac_candidates("Microsoft Edge"))
    for name in ("microsoft-edge", "microsoft-edge-stable"):
        if found := shutil.which(name):
            return Path(found)
    return None


def open_dashboard(url: str) -> str:
    """Open the dashboard in its own window. Returns how it was opened."""
    # A browser started with --app shows the page in a plain window with no
    # tabs or address bar, which is what makes the dashboard feel like an app.
    for name, finder in (("Chrome", find_chrome), ("Edge", find_edge)):
        exe = finder()
        if exe is None:
            continue
        try:
            subprocess.Popen(
                [str(exe), f"--app={url}", "--window-size=1120,900"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return f"{name} app window"
        except OSError as exc:
            log.warning("Could not start %s: %s", name, exc)
    if webbrowser.open(url):
        return "default browser"
    return "not opened"


def open_folder(folder: Path) -> bool:
    """Show a folder in Explorer, Finder or the Linux file manager."""
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))  # noqa: S606 - a folder of our own
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("Could not open %s: %s", folder, exc)
        return False
    return True
