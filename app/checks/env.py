"""What is installed: Python, packages, browsers, the Claude sign-in."""

from __future__ import annotations

import importlib
import importlib.metadata
import platform
import subprocess
import sys

from ..commands import LOGS_HINT, cmd
from ..agents.auth import agents_ready, apply_auth_mode, find_bundled_cli, find_cli, sign_in_status
from .signin import describe
from ..core.models import Settings
from ..paths import Paths
from ..window import find_chrome, find_edge
from . import Result

# (import name, package name on PyPI, needed for)
CORE = [("starlette", "starlette", "dashboard"), ("uvicorn", "uvicorn", "dashboard"),
        ("pydantic", "pydantic", "dashboard")]
LATER = [("playwright", "playwright", "the dedicated browser"),
         ("claude_agent_sdk", "claude-agent-sdk", "the agents"),
         ("pypdf", "pypdf", "reading a PDF resume"),
         ("docx", "python-docx", "reading a Word resume"),
         ("multipart", "python-multipart", "uploading a resume")]


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "?"


def _packages(group, name: str, label: str, missing_is: str) -> Result:
    found, missing = {}, []
    for module, package, purpose in group:
        try:
            importlib.import_module(module)
            found[package] = _version(package)
        except Exception as exc:  # noqa: BLE001 - a broken install counts as missing
            missing.append(f"{package} (for {purpose}): {type(exc).__name__}")
    if missing:
        return Result(name, missing_is, f"{label}: missing {', '.join(missing)}.",
                      fix=f"Run '{cmd('setup')}' again. If it still fails, the reason is in pip-install.log in {LOGS_HINT}.",
                      details={"found": found, "missing": missing})
    listing = ", ".join(f"{package} {version}" for package, version in found.items())
    return Result(name, "ok", f"{label}: {listing}.", details={"found": found})


def env_checks(paths: Paths, settings: Settings) -> list[Result]:
    results: list[Result] = []

    version = platform.python_version()
    ok = sys.version_info >= (3, 10)
    results.append(Result(
        "python", "ok" if ok else "fail",
        f"Python {version} on {platform.system()} {platform.release()}" + ("." if ok else ", but 3.10 or newer is needed."),
        fix="" if ok else f"Install Python 3.12 from python.org, then run '{cmd('setup')}' again.",
        details={"executable": sys.executable, "version": sys.version},
    ))

    results.append(_packages(CORE, "packages_core", "Dashboard packages", "fail"))
    results.append(_packages(LATER, "packages_later", "Browser and agent packages", "fail"))

    # Folders the app writes to.
    problems = []
    for folder in (paths.data, paths.config, paths.profile, paths.logs):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"{folder}: {exc}")
    results.append(Result(
        "folders", "fail" if problems else "ok",
        "Cannot write to: " + "; ".join(problems) if problems else f"Can write to {paths.root}.",
        fix="Choose a data folder you own by setting JOBAPP_HOME, or fix the folder's permissions." if problems else "",
    ))

    resumes = sorted(p.name for p in paths.profile.glob("*") if p.suffix.lower() in (".pdf", ".docx"))
    results.append(Result(
        "resume", "ok" if resumes else "warn",
        f"Resume found: {resumes[0]}." if resumes else "No resume added yet.",
        fix="" if resumes else "Add your resume (PDF or Word) on the Profile screen once Job App is running.",
        details={"files": resumes},
    ))

    chrome, edge = find_chrome(), find_edge()
    wanted = settings.browser.channel
    have = {"chrome": chrome, "msedge": edge}
    if have.get(wanted):
        results.append(Result("browser_installed", "ok", f"{'Chrome' if wanted == 'chrome' else 'Edge'} found: {have[wanted]}.",
                              details={"chrome": str(chrome), "edge": str(edge)}))
    elif chrome or edge:
        other = "Chrome" if chrome else "Edge"
        results.append(Result("browser_installed", "warn",
                              f"The preferred browser ({wanted}) was not found. {other} will be used instead.",
                              details={"chrome": str(chrome), "edge": str(edge)}))
    else:
        results.append(Result("browser_installed", "warn", "Neither Chrome nor Edge was found in the usual places.",
                              fix="Install Chrome. The browser test below shows whether one can be opened anyway.",
                              details={"chrome": None, "edge": None}))

    # Sign-in. Looks at what is stored on this computer; sends nothing to Claude.
    ignored = apply_auth_mode(settings)
    if ignored:
        results.append(Result(
            "api_key", "warn",
            f"{' and '.join(ignored)} is set on this computer. Job App ignores it, because you chose to sign in with your Claude account.",
            details={"ignored": ignored},
        ))
    cli = find_cli()
    if cli is None:
        results.append(Result("claude_runtime", "fail", "The Claude runtime that the agents need was not found.",
                              fix=f"Run '{cmd('setup')}' again. If it still fails, the reason is in pip-install.log in {LOGS_HINT}."))
        results.append(Result("claude_signin", "skip", "Sign-in not checked, because the runtime is missing."))
        return results
    bundled = find_bundled_cli() is not None
    try:
        shown = subprocess.run([str(cli), "--version"], capture_output=True, text=True, timeout=45,
                               stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        shown = f"could not be started ({type(exc).__name__})"
    results.append(Result(
        "claude_runtime", "ok" if bundled else "warn",
        f"Claude runtime: {shown} ({'comes with the app' if bundled else 'the one installed on this computer'}).",
        details={"path": str(cli), "bundled": bundled},
    ))

    if settings.auth.mode == "api_key":
        status = agents_ready(paths.config, settings)
        safe = {k: v for k, v in status.items() if k in ("mode", "source", "key_looks_right", "signed_in")}
        if not status.get("signed_in"):
            results.append(Result("claude_signin", "fail", "API-key sign-in is chosen, but no API key is stored.",
                                  fix=f"Run '{cmd('signin --api-key')}' and paste your own key from the Claude Console.",
                                  details=safe))
        elif status.get("source") != "cloud provider" and not status.get("key_looks_right"):
            results.append(Result("claude_signin", "warn",
                                  "An API key is stored, but it does not look like an Anthropic key (sk-ant-...).",
                                  fix=f"Run '{cmd('signin --api-key')}' to replace it.", details=safe))
        else:
            results.append(Result("claude_signin", "ok", describe(status), details=safe))
        return results

    status = sign_in_status(cli)
    if not status.get("known"):
        results.append(Result("claude_signin", "fail", "Could not find out whether Claude is signed in.",
                              fix=f"Run '{cmd('signin')}', then '{cmd('check')}' again.", details=status))
    elif not status["signed_in"]:
        results.append(Result("claude_signin", "fail", "Claude is not signed in on this computer yet.",
                              fix=f"Run '{cmd('signin')}' and sign in with your own Claude account or API key, "
                                  f"then run '{cmd('check')}' again.",
                              details=status))
    elif any(word in str(status.get("method") or "").lower() for word in ("key", "console")):
        results.append(Result("claude_signin", "warn",
                              f"Claude is signed in, but through '{status.get('method')}' rather than a Claude plan.",
                              fix=f"To use your plan's limits, run '{cmd('signin --plan')}'. "
                                  f"To use an API key instead, run '{cmd('signin --api-key')}'.",
                              details=status))
    else:
        plan = f" ({status['plan']} plan)" if status.get("plan") else ""
        results.append(Result("claude_signin", "ok", f"Signed in with your Claude account{plan}.", details=status))

    return results
