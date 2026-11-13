"""The checks that actually start things: the dedicated browser and the agents.

They run in one event loop, together with a local web server, because that is
exactly how the finished app runs. If these pass on your computer, the combination
that autofill depends on works there.
"""

from __future__ import annotations

import asyncio
import random
import traceback
from pathlib import Path
from typing import Optional

from ..commands import cmd
from ..browser.launch import BrowserLaunchError, launch_dedicated
from ..core.models import Settings
from ..paths import Paths
from . import Result
from .testpage import READ_VALUES, LocalFormSite


def _trace(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]


def _resume(paths: Paths) -> Optional[Path]:
    for pattern in ("resume.pdf", "resume.docx", "*.pdf", "*.docx"):
        found = sorted(paths.profile.glob(pattern))
        if found:
            return found[0]
    return None


# --- the dedicated browser ---------------------------------------------------------


async def browser_check(paths: Paths, settings: Settings, headless: bool) -> Result:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return Result("browser", "fail", "The browser test could not run because Playwright is not installed.",
                      fix=f"Run '{cmd('setup')}' again.", details={"error": str(exc)})

    steps: dict[str, object] = {}
    resume = _resume(paths)
    try:
        async with LocalFormSite() as site, async_playwright() as p:
            # First visit: open, fill one of each kind of field, attach a file.
            context, used = await launch_dedicated(p, paths.browser_profile, settings.browser.channel, headless)
            steps["opened"] = used
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(site.url)
            await page.fill("#name", "Test Person")
            await page.fill("#email", "test.person@example.com")
            await page.select_option("#degree", label="Master's")
            await page.check("#auth-yes")
            await page.check("#updates")
            await page.fill("#why", "Line one.\nLine two.")
            if resume is not None:
                await page.set_input_files("#resume", str(resume))
            values = await page.evaluate(READ_VALUES)
            steps["values"] = values
            await page.evaluate("() => localStorage.setItem('jobapp_check', 'kept')")
            await context.close()

            # Second visit: is what the browser stored still there?
            context, _ = await launch_dedicated(p, paths.browser_profile, used, headless)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(site.url)
            steps["kept"] = await page.evaluate("() => localStorage.getItem('jobapp_check')")
            await context.close()
    except BrowserLaunchError as exc:
        return Result(
            "browser", "fail", "The dedicated browser could not be opened.",
            fix=f"Close any Chrome window titled 'Job App check form' and run '{cmd('check')}' again. "
                "If it still fails, the log file below has the reason for each browser tried.",
            details={"attempts": exc.attempts, "steps": steps},
        )
    except Exception as exc:  # noqa: BLE001 - everything is reported, nothing is fatal
        return Result("browser", "fail", f"The browser test stopped: {type(exc).__name__}.",
                      fix="The log file below has the full error.",
                      details={"steps": steps, "trace": _trace(exc)})

    expected = {
        "name": "Test Person", "email": "test.person@example.com", "degree": "Master's",
        "authorized": "Yes", "updates": True, "why": "Line one.\nLine two.",
    }
    wrong = {key: values.get(key) for key, want in expected.items() if values.get(key) != want}
    if resume is not None and values.get("resume") != resume.name:
        wrong["resume"] = values.get("resume")
    names = {"chrome": "Chrome", "msedge": "Edge", "chromium": "the Playwright test browser"}
    browser = names.get(str(steps["opened"]), str(steps["opened"]))
    if wrong:
        return Result("browser", "fail", f"{browser} opened, but these fields did not take: {', '.join(wrong)}.",
                      fix="The log file below has what was read back.", details={"steps": steps, "wrong": wrong})
    if steps.get("kept") != "kept":
        return Result("browser", "fail", f"{browser} filled the form, but did not keep its data between runs.",
                      fix="Sign-ins would be lost each time. The log file below has the details.",
                      details={"steps": steps})
    attached = "attached your resume, " if resume is not None else ""
    note = "" if resume is not None else " No resume was in the profile folder, so attaching a file was not tested."
    status = "ok" if steps["opened"] == settings.browser.channel else "warn"
    return Result("browser", status,
                  f"Dedicated browser: opened {browser}, filled every kind of field, {attached}"
                  f"and kept its data between runs.{note}",
                  details={"steps": steps})


# --- the agents --------------------------------------------------------------------
#
# These go through app/agents/runner.py, the same code every agent in the app
# uses, so a pass here means the real thing works.


def _failure(name: str, what: str, exc: BaseException, stderr: list[str]) -> Result:
    from ..agents.runner import AgentError, classify

    text = f"{type(exc).__name__}: {exc}"
    tail = [line for line in stderr[-15:] if line.strip()]
    if isinstance(exc, AgentError):
        kind = exc.kind
    elif isinstance(exc, asyncio.TimeoutError):
        kind = "timeout"
    else:
        kind = classify(text + " " + " ".join(tail))
    fix = AgentError(kind).message if kind != "failed" else "The log file below has the full error."
    if kind == "not_signed_in":
        fix = (f"Run '{cmd('signin')}' and sign in with your own Claude account or API key, "
               f"then run '{cmd('check')}' again.")
    return Result(name, "fail", f"{what} failed: {text[:200]}", fix=fix,
                  details={"kind": kind, "error": text, "stderr": tail, "trace": _trace(exc)})


async def agent_reply_check(paths: Paths, settings: Settings) -> Result:
    from ..agents.runner import AgentError, build_options, run_once

    stderr: list[str] = []
    try:
        options, stderr = build_options(
            paths, settings, model=settings.models.autofill, max_turns=1,
            system_prompt="This is a connection test. Reply with exactly the word you are asked for and nothing else.",
        )
        reply = await asyncio.wait_for(run_once("Reply with exactly: ready", options), timeout=150)
    except AgentError as exc:
        return Result("agent_reply", "fail", "The agent test could not run.", fix=exc.message, details={"kind": exc.kind})
    except Exception as exc:  # noqa: BLE001
        return _failure("agent_reply", "Asking Claude for a reply", exc, stderr)
    details = {"text": reply.text[:300], "model": reply.model, "turns": reply.turns, "cost_usd": reply.cost_usd,
               "duration_ms": reply.duration_ms, "kinds": reply.kinds}
    if "ready" not in reply.text.lower():
        return Result("agent_reply", "fail", f"Claude answered, but not as expected: {(reply.text or 'no text')[:160]}",
                      fix="The log file below has what came back.", details=details)
    return Result("agent_reply", "ok",
                  f"An agent answered using model {reply.model or settings.models.autofill} "
                  f"in {(reply.duration_ms or 0) / 1000:.1f} seconds.", details=details)


async def agent_structured_check(paths: Paths, settings: Settings) -> Result:
    """An agent returns an answer in a fixed shape, the way the profile and requirements readers do."""
    from ..agents.runner import AgentError, ask_structured

    schema = {
        "type": "object",
        "properties": {"word": {"type": "string"}, "number": {"type": "integer"}},
        "required": ["word", "number"],
        "additionalProperties": False,
    }
    try:
        reply = await ask_structured(
            paths, settings, name="check_structured",
            instructions='This is a connection test. Return one JSON object like {"word": "...", "number": 0}.',
            prompt='Return the word "ready" and the number 7.',
            schema=schema, model=settings.models.search, timeout=150,
        )
    except AgentError as exc:
        return Result("agent_structured", "fail", f"An agent could not return a structured answer ({exc.kind}).",
                      fix=exc.message, details={"kind": exc.kind, "detail": exc.detail})
    details = {"data": reply.data, "path": reply.path, "model": reply.model, "turns": reply.turns,
               "cost_usd": reply.cost_usd}
    if reply.data.get("word") != "ready" or reply.data.get("number") != 7:
        return Result("agent_structured", "fail", f"The structured answer was not what was asked for: {reply.data}",
                      fix="The log file below has what came back.", details=details)
    how = "the runtime's structured output" if reply.path == "structured" else "plain JSON in the reply"
    return Result("agent_structured", "ok", f"An agent returned a structured answer ({how}).", details=details)


async def agent_tool_check(paths: Paths, settings: Settings, headless: bool) -> Result:
    """An agent reads a field in the dedicated browser through one of the app's own tools."""
    from ..agents.runner import AgentError, build_options, run_once

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return Result("agent_tool", "fail", "The tool test could not run because a package is missing.",
                      fix=f"Run '{cmd('setup')}' again.", details={"error": str(exc)})

    secret = f"Test Person {random.randint(1000, 9999)}"  # proves the tool was really used
    stderr: list[str] = []
    try:
        async with LocalFormSite() as site, async_playwright() as p:
            context, _ = await launch_dedicated(p, paths.browser_profile, settings.browser.channel, headless)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(site.url)
                await page.fill("#name", secret)

                @tool("read_name_field", "Returns the text currently in the Full name field of the open form.", {})
                async def read_name_field(args):
                    return {"content": [{"type": "text", "text": await page.input_value("#name")}]}

                server = create_sdk_mcp_server("jobapp", tools=[read_name_field])
                options, stderr = build_options(
                    paths, settings, model=settings.models.autofill, max_turns=4,
                    mcp_servers={"jobapp": server},
                    allowed_tools=["mcp__jobapp__read_name_field"],
                    system_prompt="This is a connection test. Use the tool you are given, then answer in one line.",
                )
                reply = await asyncio.wait_for(
                    run_once("Call the read_name_field tool once, then reply with exactly the text it returned.", options),
                    timeout=200,
                )
            finally:
                await context.close()
    except BrowserLaunchError as exc:
        return Result("agent_tool", "fail", "The tool test could not open the dedicated browser.",
                      fix="See the browser test above.", details={"attempts": exc.attempts})
    except AgentError as exc:
        return Result("agent_tool", "fail", "The tool test could not run.", fix=exc.message, details={"kind": exc.kind})
    except Exception as exc:  # noqa: BLE001
        return _failure("agent_tool", "An agent using an app tool", exc, stderr)

    details = {"text": reply.text[:300], "tools_called": reply.tools_called, "expected": secret,
               "turns": reply.turns, "cost_usd": reply.cost_usd, "kinds": reply.kinds}
    used = any("read_name_field" in name for name in reply.tools_called)
    if used and secret in reply.text:
        return Result("agent_tool", "ok",
                      "An agent read a field in the dedicated browser through one of the app's own tools.",
                      details=details)
    return Result("agent_tool", "fail",
                  "The agent ran, but did not read the field through the app's tool."
                  if not used else "The agent used the tool, but reported a different value.",
                  fix="The log file below has what the agent did.", details=details)
