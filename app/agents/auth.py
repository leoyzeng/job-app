"""How the app's agents sign in to Claude.

Every person who runs Job App signs in with their own account. Nothing is
shared, and there is no project-wide key. Two ways are supported:

``subscription``
    Your own Claude account (Pro or Max plan), signed in once with
    ``jobapp signin``. The Claude Agent SDK ships with its own copy of the
    Claude Code runtime, and that runtime keeps the sign-in. Usage counts
    against your plan's normal limits. An API key in the environment is
    ignored on purpose, because the runtime would otherwise prefer it and bill
    it instead.

``api_key``
    Your own Anthropic API key, from the Claude Console. Usage is billed to
    your Console account. The key is kept in the system's password store
    (Windows Credential Manager, the macOS Keychain, or the Secret Service on
    Linux) through the ``keyring`` package. Only if no password store is
    available is it kept in a file readable only by you, in your data folder.
    ``ANTHROPIC_API_KEY`` in the environment takes precedence over both.

The key is handed only to the agents' runtime process, through its
environment. It is never written to settings or logs, and never shown.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..core.models import Settings

log = logging.getLogger("jobapp.auth")

# Either of these makes the runtime use API billing instead of the plan sign-in.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Set when a cloud provider is chosen through the runtime's own variables (advanced).
_PROVIDER_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")

KEYRING_SERVICE = "Job App"
KEYRING_ACCOUNT = "anthropic-api-key"
KEY_FILE_NAME = "api_key"  # only used when there is no password store

# The environment as it was when Job App started, before subscription mode removed any API key from it.
_ORIGINAL_ENV = {name: os.environ.get(name) for name in _API_KEY_VARS}


def find_bundled_cli() -> Optional[Path]:
    """The Claude runtime that comes inside the claude-agent-sdk package."""
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    name = "claude.exe" if sys.platform == "win32" else "claude"
    candidate = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return candidate if candidate.is_file() else None


def find_cli() -> Optional[Path]:
    bundled = find_bundled_cli()
    if bundled:
        return bundled
    on_path = shutil.which("claude")
    return Path(on_path) if on_path else None


def apply_auth_mode(settings: Settings) -> list[str]:
    """Make this process's environment match the chosen sign-in mode.

    Returns the names of API-key variables that were set and are now ignored.
    Only the names are returned or logged, never the values.
    """
    if settings.auth.mode != "subscription":
        # Put back a key the user set in the environment, in case an earlier
        # call in this process removed it while subscription mode was chosen.
        for name, value in _ORIGINAL_ENV.items():
            if value and not os.environ.get(name):
                os.environ[name] = value
        return []
    ignored = [name for name in _API_KEY_VARS if os.environ.pop(name, None)]
    if ignored:
        log.info("Ignoring %s because sign-in mode is 'subscription'.", ", ".join(ignored))
    return ignored


# --- the API key ----------------------------------------------------------------


def _keyring():
    """The keyring module, if a real password store is available; else None."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as NoStore
    except Exception:  # noqa: BLE001 - not installed, or broken on this system
        return None
    try:
        if isinstance(keyring.get_keyring(), NoStore):
            return None
    except Exception:  # noqa: BLE001
        return None
    return keyring


def _key_file(config_dir: Path) -> Path:
    return config_dir / KEY_FILE_NAME


def looks_like_api_key(text: str) -> bool:
    text = (text or "").strip()
    return text.startswith("sk-ant-") and len(text) > 30 and not any(ch.isspace() for ch in text)


def save_api_key(config_dir: Path, key: str) -> str:
    """Keep the key. Returns where: 'password store' or 'file'."""
    key = key.strip()
    store = _keyring()
    if store is not None:
        try:
            store.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key)
            _key_file(config_dir).unlink(missing_ok=True)
            return "password store"
        except Exception as exc:  # noqa: BLE001 - fall back to the file
            log.warning("The password store refused the key (%s); keeping it in a private file instead.",
                        type(exc).__name__)
    config_dir.mkdir(parents=True, exist_ok=True)
    target = _key_file(config_dir)
    target.unlink(missing_ok=True)
    # Created readable by you only, before the key is written into it.
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    return "file"


def forget_api_key(config_dir: Path) -> None:
    store = _keyring()
    if store is not None:
        try:
            store.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:  # noqa: BLE001 - nothing stored
            pass
    _key_file(config_dir).unlink(missing_ok=True)


def find_api_key(config_dir: Path) -> tuple[Optional[str], str]:
    """(key, where it came from). Where is 'environment', 'password store', 'file' or ''."""
    from_env = os.environ.get("ANTHROPIC_API_KEY") or _ORIGINAL_ENV.get("ANTHROPIC_API_KEY")
    if from_env:
        return from_env.strip(), "environment"
    store = _keyring()
    if store is not None:
        try:
            stored = store.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:  # noqa: BLE001
            stored = None
        if stored:
            return stored.strip(), "password store"
    try:
        stored = _key_file(config_dir).read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    if stored:
        return stored, "file"
    return None, ""


def uses_cloud_provider() -> bool:
    return any(os.environ.get(name) for name in _PROVIDER_VARS)


def agent_env(config_dir: Path, settings: Settings) -> dict[str, str]:
    """Environment variables to hand to the agents' runtime for the chosen sign-in.

    Raises LookupError when API-key mode is chosen and no key is available.
    """
    if settings.auth.mode != "api_key" or uses_cloud_provider():
        return {}
    key, _ = find_api_key(config_dir)
    if not key:
        raise LookupError("no API key")
    return {"ANTHROPIC_API_KEY": key}


# --- status -----------------------------------------------------------------------


def sign_in_status(cli: Path) -> dict:
    """Ask the runtime whether it is signed in. Makes no request to Claude.

    Only the non-personal parts of the answer are returned, so that log files
    never contain an email address or organization name.
    """
    try:
        done = subprocess.run(
            [str(cli), "auth", "status", "--json"],
            capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"known": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        raw = json.loads(done.stdout)
    except ValueError:
        return {"known": False, "error": (done.stderr or done.stdout or "no output").strip()[:300]}
    return {
        "known": True,
        "signed_in": bool(raw.get("loggedIn")),
        "method": raw.get("authMethod"),
        "provider": raw.get("apiProvider"),
        "plan": raw.get("subscriptionType"),
    }


def agents_ready(config_dir: Path, settings: Settings) -> dict:
    """Can the agents sign in with the chosen mode? Sends nothing to Claude.

    Returns {"known": bool, "signed_in": bool, "mode": ..., plus details}.
    The details never include a key, an email address or an organization name.
    """
    mode = settings.auth.mode
    apply_auth_mode(settings)
    if mode == "api_key":
        if uses_cloud_provider():
            return {"known": True, "signed_in": True, "mode": mode, "source": "cloud provider"}
        key, source = find_api_key(config_dir)
        return {"known": True, "signed_in": bool(key), "mode": mode, "source": source,
                "key_looks_right": looks_like_api_key(key or "")}
    cli = find_cli()
    if cli is None:
        return {"known": False, "signed_in": False, "mode": mode, "error": "runtime not found"}
    status = sign_in_status(cli)
    status["mode"] = mode
    return status


# --- the runtime's own session files ----------------------------------------------


def runtime_config_dir() -> Path:
    chosen = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(chosen).expanduser() if chosen else Path.home() / ".claude"


def transcript_folder(agent_cwd: Path) -> Path:
    """Where the runtime keeps the transcripts of agent runs started in ``agent_cwd``.

    The runtime names the folder after the working directory, with every
    character that is not a letter or digit replaced by '-'. Job App's agents
    always start in their own folder, so this folder holds only Job App's runs.
    """
    name = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in str(agent_cwd.resolve()))
    return runtime_config_dir() / "projects" / name


def forget_transcripts(agent_cwd: Path, older_than_seconds: float = 0) -> int:
    """Delete the runtime's transcripts of Job App's agent runs. Returns how many items went.

    ``older_than_seconds`` leaves alone files still being written by a run in progress.
    """
    import time

    cutoff = time.time() - older_than_seconds
    folder = transcript_folder(agent_cwd)
    # Only ever the one folder named after Job App's own agent folder.
    if folder.parent != runtime_config_dir() / "projects" or not folder.name.endswith("agent-workdir"):
        return 0
    if not folder.is_dir():
        return 0
    removed = 0
    for item in folder.iterdir():
        try:
            if older_than_seconds and item.stat().st_mtime > cutoff:
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed += 1
        except OSError:
            pass
    return removed
