"""Runs the checks, prints a short report, and saves the full one to data/logs."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
from datetime import datetime

from ..commands import cmd
from .. import __version__
from ..core.jsonfile import write_json_atomic
from ..core.settings import load_settings
from ..paths import default_paths
from . import Result
from .env import env_checks

MARK = {"ok": "[ ok ]", "warn": "[note]", "fail": "[FAIL]", "skip": "[skip]"}


def _status_of(results: list[Result], name: str) -> str:
    return next((r.status for r in results if r.name == name), "skip")


async def _live(paths, settings, args, results: list[Result]) -> None:
    from .live import agent_reply_check, agent_structured_check, agent_tool_check, browser_check

    packages_ok = _status_of(results, "packages_later") == "ok"
    signed_in = _status_of(results, "claude_signin") in ("ok", "warn")

    if args.skip_browser:
        results.append(Result("browser", "skip", "Dedicated browser: not tested (skipped on request)."))
    elif not packages_ok:
        results.append(Result("browser", "skip", "Dedicated browser: not tested, because packages are missing."))
    else:
        print("Testing the dedicated browser. A browser window will open and close twice ...", flush=True)
        results.append(await browser_check(paths, settings, args.headless))

    from .search_checks import feeds_check, headless_check, web_search_check

    if packages_ok:
        print("Reaching the job feeds the search reads ...", flush=True)
        results.append(await feeds_check(paths, settings))
        if not args.skip_browser:
            print("Starting the hidden browser used for JavaScript job pages ...", flush=True)
            results.append(await headless_check(settings))
            from .form_checks import practice_check, real_forms_check

            if _status_of(results, "browser") in ("ok", "warn"):
                print("Filling the practice forms in Job App's browser window. It opens once more ...", flush=True)
                results.append(await practice_check(paths, settings, args.headless))
            print("Reading real application forms in a hidden browser (read only, nothing is typed or sent) ...", flush=True)
            results.append(await real_forms_check(paths, settings))

    if args.skip_agent:
        results.append(Result("agent_reply", "skip", "Agents: not tested (skipped on request)."))
        return
    if not packages_ok or not signed_in:
        why = "packages are missing" if not packages_ok else "Claude is not signed in"
        results.append(Result("agent_reply", "skip", f"Agent reply: not tested, because {why}."))
        results.append(Result("agent_structured", "skip", f"Agent structured answer: not tested, because {why}."))
        results.append(Result("agent_tool", "skip", f"Agent using an app tool: not tested, because {why}."))
        results.append(Result("search_web", "skip", f"Agent web search: not tested, because {why}."))
        results.append(Result("autofill", "skip", f"Autofill with the application agent: not tested, because {why}."))
        return

    print("Asking Claude for a one-word reply. This uses a small amount of your Claude usage ...", flush=True)
    reply = await agent_reply_check(paths, settings)
    results.append(reply)
    if reply.status != "ok":
        results.append(Result("agent_structured", "skip", "Agent structured answer: not tested, because the reply test failed."))
        results.append(Result("agent_tool", "skip", "Agent using an app tool: not tested, because the reply test failed."))
        results.append(Result("search_web", "skip", "Agent web search: not tested, because the reply test failed."))
        results.append(Result("autofill", "skip", "Autofill with the application agent: not tested, because the reply test failed."))
        return
    print("Asking an agent for an answer in a fixed shape ...", flush=True)
    results.append(await agent_structured_check(paths, settings))
    if _status_of(results, "browser") not in ("ok", "warn"):
        results.append(Result("agent_tool", "skip", "Agent using an app tool: not tested, because the browser test did not pass."))
    else:
        print("Letting an agent read a field in the dedicated browser. The window opens once more ...", flush=True)
        results.append(await agent_tool_check(paths, settings, args.headless))
    print("Letting an agent run one web search ...", flush=True)
    results.append(await web_search_check(paths, settings))
    if getattr(args, "skip_autofill", False):
        results.append(Result("autofill", "skip", "Autofill with the application agent: not tested (skipped on request)."))
    elif _status_of(results, "browser") not in ("ok", "warn"):
        results.append(Result("autofill", "skip",
                              "Autofill with the application agent: not tested, because the browser test did not pass."))
    else:
        from .apply_checks import autofill_check

        print("Letting the application agent fill a practice form in Job App's browser window. Nothing is sent;",
              flush=True)
        print("this takes a few minutes and uses some of your Claude usage ...", flush=True)
        results.append(await autofill_check(paths, settings, args.headless))


def run_checks(args: argparse.Namespace) -> int:
    paths = default_paths()
    paths.ensure()
    settings = load_settings(paths.settings)
    started = datetime.now()
    print(f"Job App check, {started:%Y-%m-%d %H:%M}")
    print()

    results = env_checks(paths, settings)
    if args.quick:
        # Setup runs this before you have had a chance to sign in, so a
        # missing sign-in is the next step here rather than a failure.
        for result in results:
            if result.name == "claude_signin" and result.status == "fail":
                result.status = "warn"
                result.fix = f"Run '{cmd('signin')}' once, then '{cmd('check')}'."
    if not args.quick:
        try:
            asyncio.run(_live(paths, settings, args, results))
        except KeyboardInterrupt:
            results.append(Result("stopped", "fail", "The check was stopped before it finished."))

    lines = []
    for result in results:
        lines.append(f"{MARK[result.status]} {result.summary}")
        if result.fix:
            lines.append(f"       To fix: {result.fix}")
    failed = [r for r in results if r.status == "fail"]
    skipped = [r for r in results if r.status == "skip"]
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} thing(s) need attention. Fix the first one and run '{cmd('check')}' again.")
    elif args.quick:
        signed_in = _status_of(results, "claude_signin") == "ok"
        lines.append("Everything needed is installed. Next: "
                     + (f"'{cmd('check')}' for the full test" if signed_in else f"'{cmd('signin')}', then '{cmd('check')}' for the full test")
                     + f". '{cmd('run')}' starts Job App.")
    elif skipped:
        lines.append("Nothing failed, but some tests were skipped (see above).")
    else:
        lines.append("Everything works on this computer.")

    stamp = f"{started:%Y%m%d-%H%M%S}"
    text_path = paths.logs / f"check-{stamp}.txt"
    json_path = paths.logs / f"check-{stamp}.json"
    lines.append(f"Full details: {json_path}")
    report = "\n".join(lines)
    print()
    print(report)

    text_path.write_text(f"Job App check, {started:%Y-%m-%d %H:%M}\n\n{report}\n", encoding="utf-8")
    write_json_atomic(json_path, {
        "app_version": __version__,
        "started": started.isoformat(timespec="seconds"),
        "quick": bool(args.quick),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "results": [
            {"name": r.name, "status": r.status, "summary": r.summary, "fix": r.fix, "details": r.details}
            for r in results
        ],
    }, backup=False)
    return 1 if failed else 0
