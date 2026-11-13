"""Bring your data over from an older copy of Job App.

Before version 0.9, everything lived inside the program folder (``profile``,
``data`` and ``config``). Now it lives in a per-user folder (see
``app/paths.py``). ``jobapp import <old folder>`` copies it across once. The
old folder is left exactly as it was.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..commands import cmd
from ..paths import default_paths

# Not worth carrying over: the agents' scratch folder and the dashboard's session cookie.
SKIP = {"agent_workdir", ".session_token", "__pycache__"}


def _has_files(folder: Path) -> bool:
    return folder.is_dir() and any(item.is_file() for item in folder.rglob("*"))


def import_old_folder(old: str) -> int:
    source = Path(old).expanduser().resolve()
    paths = default_paths()
    target = paths.root
    if not (source / "profile").is_dir() and not (source / "data").is_dir():
        print(f"{source} does not look like an old Job App folder: it has no 'profile' or 'data' folder.")
        return 1
    if source == target or source == paths.program:
        print("That is the folder Job App already uses. Nothing to do.")
        return 1
    if _has_files(target / "profile") or (target / "data" / "tracker.json").exists() \
            or (target / "data" / "answers.json").exists():
        print(f"Your data folder already has a profile or jobs in it: {target}")
        print("To replace it, close Job App, rename or move that folder, then run this again.")
        return 1

    def ignore(_dir, names):
        return [name for name in names if name in SKIP]

    copied = []
    for name in ("profile", "data"):
        if (source / name).is_dir():
            shutil.copytree(source / name, target / name, dirs_exist_ok=True, ignore=ignore)
            copied.append(name)
    settings = source / "config" / "settings.json"
    if settings.is_file():
        (target / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, target / "config" / "settings.json")
        copied.append("settings")
    print(f"Copied {', '.join(copied)} from {source}")
    print(f"to {target}")
    print("The old folder is unchanged. Once Job App works with the copy, you can delete the old one yourself.")
    return 0


# What "jobapp forget" removes from the data folder. Only these, never the folder's other contents,
# in case JOBAPP_HOME points at a folder that holds other things too.
OURS = ("profile", "data", "data_demo", "config")


def forget_everything(assume_yes: bool = False) -> int:
    """Delete everything Job App keeps about you on this computer."""
    from ..agents.auth import forget_api_key, forget_transcripts

    paths = default_paths()
    root = paths.root
    present = [root / name for name in OURS if (root / name).exists()]
    print("This deletes everything Job App keeps about you on this computer:")
    print(f"  {root}")
    print("  your profile and resume, requirements, jobs, answers, applications, settings,")
    print("  the browser window's sign-ins and cookies, your stored API key,")
    print("  and the Claude runtime's transcripts of Job App's agent runs.")
    print("It cannot be undone. Close Job App first.")
    if not assume_yes:
        try:
            typed = input("Type DELETE to go ahead: ").strip()
        except EOFError:
            typed = ""
        if typed != "DELETE":
            print("Nothing was deleted.")
            return 1
    forget_transcripts(paths.agent_cwd)
    forget_api_key(paths.config)
    left = []
    for folder in present:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            left.append(folder)
    try:
        root.rmdir()  # only if nothing else is in it
    except OSError:
        pass
    if left:
        print("Some files could not be deleted, probably because Job App or its browser window is still open:")
        for folder in left:
            print(f"  {folder}")
        return 1
    print("Done. Everything Job App kept about you is deleted.")
    print("Your Claude account sign-in, if you used one, is kept by the Claude runtime;")
    print(f"remove it with '{cmd('signin --sign-out')}' before running this, if you want that gone too.")
    return 0
