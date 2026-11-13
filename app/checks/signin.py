"""Sign in to Claude for the app's agents: ``jobapp signin`` (Windows) or ``./jobapp.sh signin``.

Each person signs in with their own account, in one of two ways:

1. Their Claude account (Pro or Max plan). The runtime that comes with the
   Claude Agent SDK opens the browser and keeps the sign-in.
2. Their own Anthropic API key from the Claude Console, kept in the system's
   password store.
"""

from __future__ import annotations

import asyncio
import getpass
import subprocess
from datetime import datetime

from ..commands import cmd
from ..agents.auth import (
    agents_ready,
    apply_auth_mode,
    find_api_key,
    find_cli,
    forget_api_key,
    looks_like_api_key,
    save_api_key,
    sign_in_status,
)
from ..core.settings import load_settings, update_settings
from ..paths import default_paths

CONSOLE_KEYS_URL = "https://platform.claude.com/settings/keys"


def _note(paths, outcome: str) -> None:
    """One line per attempt in logs/signin.log. Never a key or account details."""
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        with open(paths.logs / "signin.log", "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {outcome}\n")
    except OSError:
        pass


def _ask(question: str, choices: str, default: str) -> str:
    while True:
        try:
            answer = input(question).strip().lower() or default
        except EOFError:
            return default
        if answer in choices:
            return answer
        print(f"Please type one of: {', '.join(choices)}.")


def describe(status: dict) -> str:
    if status.get("mode") == "api_key":
        if not status.get("signed_in"):
            return "API key chosen, but no key is stored."
        where = {"environment": "the ANTHROPIC_API_KEY environment variable",
                 "password store": "your system's password store",
                 "file": "a private file in your data folder",
                 "cloud provider": "your cloud provider's settings"}.get(status.get("source"), "unknown")
        return f"Using your own API key, from {where}."
    if not status.get("known"):
        return "Could not find out whether Claude is signed in."
    if not status.get("signed_in"):
        return "Not signed in to Claude yet."
    plan = f" ({status['plan']} plan)" if status.get("plan") else ""
    return f"Signed in with your Claude account{plan}."


def _plan_sign_in(paths) -> int:
    settings = update_settings(paths.settings, {"auth": {"mode": "subscription"}})
    apply_auth_mode(settings)
    cli = find_cli()
    if cli is None:
        print(f"The Claude runtime was not found. Run '{cmd('setup')}' first.")
        _note(paths, "plan: runtime not found")
        return 1
    before = sign_in_status(cli)
    if before.get("known") and before.get("signed_in") and before.get("plan"):
        print(f"Already signed in with your Claude account ({before.get('plan')} plan). Nothing to do.")
        print(f"To use a different account: '{cmd('signin --sign-out')}', then '{cmd('signin')}' again.")
        _note(paths, f"plan: already signed in (plan {before.get('plan')})")
        return 0
    print("Your browser will open. Sign in with your own Claude account (Pro or Max),")
    print("then come back to this window. If a code is shown, paste it here when asked.")
    print()
    try:
        code = subprocess.call([str(cli), "auth", "login", "--claudeai"])
    except OSError as exc:
        print(f"The sign-in could not be started: {exc}")
        _note(paths, f"plan: could not start: {type(exc).__name__}")
        return 1
    after = sign_in_status(cli)
    print()
    if after.get("known") and after.get("signed_in"):
        print(f"Signed in. Run '{cmd('check')}' to confirm the agents can reach Claude.")
        _note(paths, f"plan: signed in (method {after.get('method')}, plan {after.get('plan')})")
        return 0
    print(f"Sign-in did not complete. Run '{cmd('signin')}' to try again.")
    _note(paths, f"plan: not completed (exit code {code})")
    return code or 1


def _key_sign_in(paths, test: bool = True) -> int:
    print("Create a key in the Claude Console, under Settings, API keys:")
    print(f"  {CONSOLE_KEYS_URL}")
    print("Usage is billed to your Console account. The key is kept in your system's password store")
    print("and is only handed to the agents; it is never shown or written to Job App's files.")
    print()
    try:
        key = getpass.getpass("Paste your API key (it is not shown as you type), then press Enter: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("No key entered. Nothing changed.")
        return 1
    if not looks_like_api_key(key):
        print("That does not look like an Anthropic API key (they start with 'sk-ant-'). Nothing changed.")
        _note(paths, "key: rejected, wrong shape")
        return 1
    where = save_api_key(paths.config, key)
    settings = update_settings(paths.settings, {"auth": {"mode": "api_key"}})
    if where == "file":
        print("No system password store was found, so the key is kept in a file only you can read:")
        print(f"  {paths.config / 'api_key'}")
    else:
        print("Key saved in your system's password store.")
    _note(paths, f"key: saved ({where})")
    if not test:
        return 0
    print("Testing it with one very short request (a fraction of a cent) ...", flush=True)
    from .live import agent_reply_check

    result = asyncio.run(agent_reply_check(paths, settings))
    if result.status == "ok":
        print("The key works. Job App's agents will use it.")
        _note(paths, "key: tested ok")
        return 0
    print(f"The test did not pass: {result.summary}")
    if result.fix:
        print(f"To fix: {result.fix}")
    _note(paths, f"key: test failed ({result.details.get('kind') if result.details else 'unknown'})")
    return 1


def run_signin(choice: str = "", sign_out: bool = False, status_only: bool = False) -> int:
    paths = default_paths()
    paths.ensure()
    settings = load_settings(paths.settings)

    if status_only:
        print(describe(agents_ready(paths.config, settings)))
        return 0

    if sign_out:
        forget_api_key(paths.config)
        cli = find_cli()
        if cli is not None:
            apply_auth_mode(settings)
            subprocess.call([str(cli), "auth", "logout"])
        print("Signed out: the stored API key is removed, and the Claude runtime's sign-in is cleared.")
        key, source = find_api_key(paths.config)
        if key and source == "environment":
            print("ANTHROPIC_API_KEY is still set in your environment; remove it there if you want it gone.")
        _note(paths, "signed out")
        return 0

    print("Job App's agents sign in with your own Claude account or API key. Nothing is shared with anyone else.")
    print(f"Now: {describe(agents_ready(paths.config, settings))}")
    print()
    if not choice:
        print("How should the agents sign in?")
        print("  1. My Claude account (Pro or Max plan). Usage counts against my plan's limits.")
        print("  2. My own Anthropic API key. Usage is billed to my Claude Console account.")
        current = "2" if settings.auth.mode == "api_key" else "1"
        choice = "plan" if _ask(f"Type 1 or 2 [{current}]: ", ("1", "2"), current) == "1" else "key"
        print()
    return _plan_sign_in(paths) if choice == "plan" else _key_sign_in(paths)
