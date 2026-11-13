"""Asking a Claude agent for an answer.

Every agent the app starts goes through here, so the same limits apply to all
of them: no file, shell or web tools unless a caller adds its own; nothing read
from Claude Code settings on this computer; a cap on turns and on spend; and a line
in ``data/logs/agents.log`` for every run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..commands import LOGS_HINT, cmd
from ..core.models import Settings
from ..paths import Paths
from .auth import agent_env, apply_auth_mode, forget_transcripts

log = logging.getLogger("jobapp.agents")


class AgentError(Exception):
    """An agent run did not produce a usable answer.

    ``kind`` is one of: not_signed_in, limit, timeout, not_installed,
    bad_output, failed. ``message`` is written for the user.
    """

    MESSAGES = {
        "not_signed_in": f"The agents are not signed in to Claude. Run '{cmd('signin')}', then try again.",
        "limit": "Your Claude usage limit or API rate limit has been reached. Try again later.",
        "timeout": "Claude did not answer in time. Check your internet connection and try again.",
        "not_installed": f"The agent packages are not installed. Run '{cmd('setup')}' again.",
        "bad_output": f"The agent's answer could not be understood. Try again; the details are in agents.log in {LOGS_HINT}.",
        "failed": f"The agent stopped with an error. The details are in agents.log in {LOGS_HINT}.",
    }

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.message = self.MESSAGES.get(kind, self.MESSAGES["failed"])
        self.detail = detail
        super().__init__(f"{kind}: {detail or self.message}")


@dataclass
class AgentReply:
    data: Any = None                 # the structured answer, when one was asked for
    text: str = ""                   # everything the agent said
    path: str = ""                   # "structured", "text" or "plain"
    model: Optional[str] = None
    turns: Optional[int] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    tools_called: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)


def classify(text: str) -> str:
    """Turn an error message from the runtime into one of our kinds."""
    lowered = text.lower()
    if any(word in lowered for word in ("not logged in", "/login", "authenticat", "oauth", "401", "invalid api key",
                                        "invalid x-api-key", "credit balance")):
        return "not_signed_in"
    if any(word in lowered for word in ("usage limit", "rate limit", "rate_limit", "429", "limit reached", "quota")):
        return "limit"
    return "failed"


def parse_json(text: str) -> Any:
    """Find the JSON object in an agent's reply. Raises ValueError."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object found in the reply")


def write_log(paths: Paths, entry: dict) -> None:
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **entry}
        with open(paths.logs / "agents.log", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        log.warning("Could not write agents.log: %s", exc)


def build_options(paths: Paths, settings: Settings, *, model: str, **extra):
    """Options every agent run starts from. Returns (options, list that collects stderr)."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions
    except ImportError as exc:
        raise AgentError("not_installed", str(exc)) from exc
    apply_auth_mode(settings)
    try:
        env = agent_env(paths.config, settings)
    except LookupError as exc:
        raise AgentError("not_signed_in", "API-key sign-in is chosen, but no API key is stored") from exc
    paths.agent_cwd.mkdir(parents=True, exist_ok=True)
    stderr: list[str] = []
    extra.setdefault("tools", [])           # none of the built-in file, shell or web tools
    options = ClaudeAgentOptions(
        setting_sources=[],                 # ignore any Claude Code settings on this computer
        model=model,
        cwd=str(paths.agent_cwd),
        env=env,                            # your own API key in API-key mode; nothing otherwise
        stderr=lambda line: stderr.append(line),
        **extra,
    )
    # The runtime keeps a transcript of every run under ~/.claude; Job App removes its own unless asked to keep them.
    options._jobapp_forget = not settings.keep_agent_transcripts
    return options, stderr


def tidy_transcripts(options, older_than_seconds: float = 900) -> None:
    """Remove transcripts of earlier runs; ones still being written (newer than the cutoff) stay."""
    if getattr(options, "_jobapp_forget", False) and getattr(options, "cwd", None):
        try:
            forget_transcripts(Path(str(options.cwd)), older_than_seconds)
        except OSError:
            pass


async def run_once(prompt: str, options) -> AgentReply:
    """One agent run, start to finish. Raises whatever the SDK raises."""
    reply = AgentReply(path="plain")
    try:
        await _collect(prompt, options, reply)
    finally:
        tidy_transcripts(options)
    return reply


async def _collect(prompt: str, options, reply: AgentReply) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock, query

    problem = ""
    async for message in query(prompt=prompt, options=options):
        reply.kinds.append(type(message).__name__)
        if isinstance(message, SystemMessage) and getattr(message, "subtype", "") == "init":
            reply.model = (getattr(message, "data", None) or {}).get("model")
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    reply.text += block.text
                elif isinstance(block, ToolUseBlock):
                    reply.tools_called.append(block.name)
        elif isinstance(message, ResultMessage):
            reply.turns = message.num_turns
            reply.cost_usd = message.total_cost_usd
            reply.duration_ms = message.duration_ms
            # getattr: these fields were added to the SDK over time.
            structured = getattr(message, "structured_output", None)
            if structured is not None:
                reply.data, reply.path = structured, "structured"
            if not reply.text and message.result:
                reply.text = message.result
            if message.is_error:
                errors = getattr(message, "errors", None) or ""
                problem = f"{message.subtype}: {message.result or ''} {errors}".strip()
    if problem and reply.data is None:
        raise RuntimeError(problem)


async def ask_structured(
    paths: Paths,
    settings: Settings,
    *,
    name: str,
    instructions: str,
    prompt: str,
    schema: dict,
    model: str,
    timeout: float = 240,
    max_turns: int = 6,
    max_budget_usd: Optional[float] = 0.75,
    extra: Optional[dict] = None,
    fallback: bool = True,
) -> AgentReply:
    """Ask for an answer in a fixed JSON shape. Raises AgentError.

    First the runtime's structured-output feature is used. If that yields
    nothing usable, the agent is asked once more to reply with plain JSON,
    which is then parsed here (unless ``fallback`` is False, for agents whose
    work is too costly to repeat). The caller validates the result's contents.
    ``extra`` adds options, such as the tools a particular agent may use.
    """
    started = time.monotonic()
    attempts: list[dict] = []

    async def attempt(structured: bool) -> AgentReply:
        options_in: dict[str, Any] = {"system_prompt": instructions, "max_turns": max_turns, **(extra or {})}
        if max_budget_usd:
            options_in["max_budget_usd"] = max_budget_usd
        if structured:
            options_in["output_format"] = {"type": "json_schema", "schema": schema}
        options, stderr = build_options(paths, settings, model=model, **options_in)
        ask = prompt if structured else (
            prompt + "\n\nReply with one JSON object in the format described in your instructions, and nothing else."
        )
        record: dict = {"structured": structured}
        attempts.append(record)
        try:
            reply = await asyncio.wait_for(run_once(ask, options), timeout=timeout)
        except asyncio.TimeoutError as exc:
            record["error"] = "timeout"
            raise AgentError("timeout") from exc
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            text = f"{type(exc).__name__}: {exc} {' '.join(stderr[-10:])}"
            record["error"] = text[:600]
            raise AgentError(classify(text), text[:600]) from exc
        record.update(path=reply.path, turns=reply.turns, cost_usd=reply.cost_usd, model=reply.model,
                      kinds=reply.kinds[-6:])
        if reply.tools_called:
            record["tools"] = dict(Counter(reply.tools_called))
        if not isinstance(reply.data, dict):
            try:
                reply.data, reply.path = parse_json(reply.text), "text"
                record["path"] = "text"
            except ValueError as exc:
                record["error"] = f"unparseable reply: {reply.text[:1500]}"
                raise AgentError("bad_output", str(exc)) from exc
        return reply

    try:
        try:
            reply = await attempt(structured=True)
        except AgentError as first:
            # Sign-in, limits and timeouts will not be cured by asking again.
            if not fallback or first.kind not in ("bad_output", "failed"):
                raise
            reply = await attempt(structured=False)
    except AgentError as exc:
        write_log(paths, {"agent": name, "ok": False, "kind": exc.kind, "attempts": attempts,
                          "seconds": round(time.monotonic() - started, 1)})
        log.warning("Agent %s failed: %s", name, exc)
        raise
    write_log(paths, {"agent": name, "ok": True, "attempts": attempts,
                      "seconds": round(time.monotonic() - started, 1)})
    log.info("Agent %s answered (%s, %s turns, cost %s)", name, reply.path, reply.turns, reply.cost_usd)
    return reply
