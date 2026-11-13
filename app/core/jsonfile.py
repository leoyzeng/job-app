"""Reading and writing JSON files without ever leaving a half-written file.

A write goes to a temporary file first and is then swapped into place in one
step. The previous version is kept beside it as ``<name>.bak``.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable


class JsonCorrupt(Exception):
    """The file exists but is not valid JSON."""

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path} is not valid JSON: {reason}")
        self.path = path
        self.reason = reason


def read_json(path: Path) -> Any:
    """Return the parsed file. Raises FileNotFoundError or JsonCorrupt."""
    text = path.read_text(encoding="utf-8-sig")  # tolerate a BOM from Notepad
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonCorrupt(path, str(exc)) from exc


def _retry(action: Callable[[], Any], attempts: int = 6) -> Any:
    # On Windows a file can be locked for a moment by an editor, a virus
    # scanner or the search indexer. Waiting briefly is nearly always enough.
    delay = 0.05
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def write_json_atomic(path: Path, data: Any, *, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if backup and path.exists():
            _retry(lambda: shutil.copy2(path, backup_path(path)))
        _retry(lambda: os.replace(tmp, path))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
