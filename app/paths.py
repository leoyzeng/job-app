"""Where everything lives on disk.

The program (``app``, ``ui``, ``prompts``) stays in the folder you cloned.
Everything that is yours (profile, data, settings) lives in a per-user folder,
so updating the program with ``git pull`` never touches it:

- Windows: ``%LOCALAPPDATA%\\JobApp``
- macOS:   ``~/Library/Application Support/JobApp``
- Linux:   ``$XDG_DATA_HOME/job-app`` (usually ``~/.local/share/job-app``)

Set ``JOBAPP_HOME`` to use another folder. Tests point the whole app at a
temporary folder by passing it as ``root``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent.parent
HOME_VARIABLE = "JOBAPP_HOME"


def user_data_root() -> Path:
    """The folder that holds this user's profile, data and settings."""
    chosen = os.environ.get(HOME_VARIABLE, "").strip()
    if chosen:
        return Path(chosen).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "JobApp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "JobApp"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "job-app"


@dataclass(frozen=True)
class Paths:
    root: Path                       # your data: profile, data, config
    demo: bool = False
    app_root: Optional[Path] = None  # the program; None means the same folder as root (tests)

    @property
    def program(self) -> Path:
        return self.app_root or self.root

    # --- inputs -----------------------------------------------------------
    @property
    def ui(self) -> Path:
        return self.program / "ui"

    @property
    def profile(self) -> Path:
        # Sample-data mode has its own made-up profile, so it never reads or
        # changes your resume and requirements.
        return self.root / "data_demo" / "profile" if self.demo else self.root / "profile"

    @property
    def prompts(self) -> Path:
        return self.program / "prompts"

    # --- what the app records ---------------------------------------------
    @property
    def data(self) -> Path:
        # Sample-data mode keeps its files apart so it can never touch the
        # real tracker.
        return self.root / ("data_demo" if self.demo else "data")

    @property
    def tracker(self) -> Path:
        return self.data / "tracker.json"

    @property
    def answers(self) -> Path:
        return self.data / "answers.json"

    @property
    def applications(self) -> Path:
        return self.data / "applications"

    @property
    def browser_profile(self) -> Path:
        return self.data / "browser_profile"

    @property
    def agent_cwd(self) -> Path:
        # Agents are started from here so their session files are kept apart
        # from any other use of Claude on this computer.
        return self.data / "agent_workdir"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    # --- settings -----------------------------------------------------------
    @property
    def config(self) -> Path:
        return self.data if self.demo else self.root / "config"

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def session_token(self) -> Path:
        # Shared by the real and the sample-data instance: browser cookies are
        # per host, not per port, so two tokens would overwrite each other.
        return self.root / "data" / ".session_token"

    def ensure(self) -> None:
        for folder in (self.data, self.config, self.logs, self.profile, self.session_token.parent):
            folder.mkdir(parents=True, exist_ok=True)


def default_paths(demo: bool = False) -> Paths:
    return Paths(user_data_root(), demo, APP_ROOT)
