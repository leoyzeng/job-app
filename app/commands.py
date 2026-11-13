"""How to run Job App's commands on this computer, for messages shown to you.

Windows uses ``jobapp.cmd`` in the folder you cloned; macOS and Linux use
``jobapp.sh``. Messages name the one that works here.
"""

from __future__ import annotations

import sys

WINDOWS = sys.platform == "win32"


def cmd(name: str = "") -> str:
    """The command to type in the Job App folder, e.g. ``jobapp signin`` or ``./jobapp.sh signin``."""
    base = "jobapp" if WINDOWS else "./jobapp.sh"
    return f"{base} {name}".strip()


LOGS_HINT = "the logs folder inside your Job App data folder (Settings, Open my data folder)"
