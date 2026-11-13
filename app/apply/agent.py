"""Running one leg of the application agent (PLAN.md 8.2).

``real_application_agent`` runs Claude through the Agent SDK with the tools in
tools.py and nothing else. ``rule_based_agent`` follows the same procedure with
plain rules and no model; sample-data mode and the tests use it, and it is a
fair picture of what the real agent is allowed to do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..agents.runner import AgentError, build_options, classify, run_once, write_log
from ..core.models import Settings
from ..paths import Paths
from .tools import Leg, as_tool_text, screenshot_content

log = logging.getLogger("jobapp.apply")


@dataclass
class LegRun:
    cost_usd: float = 0.0
    turns: Optional[int] = None
    tools: dict = field(default_factory=dict)
    error: Optional[AgentError] = None


RunLeg = Callable[[Leg, dict], Awaitable[LegRun]]

FILL_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "One entry per field to fill.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The field id from get_page."},
                    "value": {"description": "Text, the option to choose, true/false for a checkbox, or a list for "
                                             "a multi-choice field."},
                    "source": {"type": "string", "enum": ["profile", "resume", "saved_answer", "inferred", "user"]},
                    "answer_id": {"type": "string", "description": "Required when source is saved_answer."},
                    "reason": {"type": "string", "description": "One line: why this value."},
                },
                "required": ["id", "value", "source", "reason"],
            },
        }
    },
    "required": ["items"],
}

TOOL_SPECS = [
    ("get_page", "See the current page: notices, step, every field with any suggested value, and the buttons.",
     {"type": "object", "properties": {}}),
    ("fill_fields", "Fill a batch of fields. The tool fills, re-reads the page and reports which values took.",
     FILL_SCHEMA),
    ("upload_document", "Attach the user's resume, or the drafted cover letter, to a file field.",
     {"type": "object", "properties": {"id": {"type": "string"},
                                       "document": {"type": "string", "enum": ["resume", "cover_letter"]}},
      "required": ["id", "document"]}),
    ("draft_long_answer", "Have the writing agent draft a long answer or cover letter for this field and fill it in.",
     {"type": "object", "properties": {"id": {"type": "string"},
                                       "guidance": {"type": "string", "description": "Length limit, what is asked."}},
      "required": ["id"]}),
    ("click", "Click a button that get_page marks you_may_click.",
     {"type": "object", "properties": {"id": {"type": "string"},
                                       "purpose": {"type": "string", "enum": ["open_form", "next_page", "add_entry",
                                                                              "expand", "dismiss"]}},
      "required": ["id", "purpose"]}),
    ("need_user", "Put a field or task on the user's 'needs you' list, with a reason the user will read.",
     {"type": "object", "properties": {"label": {"type": "string"}, "reason": {"type": "string"},
                                       "required": {"type": "boolean"}, "id": {"type": "string"}},
      "required": ["label", "reason"]}),
    ("screenshot", "A picture of the visible part of the page.", {"type": "object", "properties": {}}),
    ("inspect_element", "The HTML around one field or button, when get_page is not enough.",
     {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}),
    ("save_site_note", "Remember something about how this site's form behaves, for next time.",
     {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
    ("end_leg", "Finish this leg with an outcome and a two-to-four sentence summary for the user.",
     {"type": "object", "properties": {"outcome": {"type": "string", "enum": ["needs_input", "page_done",
                                                                             "ready_for_review", "blocked", "failed"]},
                                       "summary": {"type": "string"}},
      "required": ["outcome", "summary"]}),
]
TOOL_NAMES = [name for name, _, _ in TOOL_SPECS]


def instructions(paths: Paths) -> str:
    shared = (paths.prompts / "shared_rules.md").read_text(encoding="utf-8")
    own = (paths.prompts / "application_agent.md").read_text(encoding="utf-8")
    return (shared + "\n\nRule 5 above is for agents that reply with JSON. You do not: you work through your tools "
            "and finish by calling end_leg.\n\n" + own)


async def call_tool(leg: Leg, name: str, args: dict) -> dict:
    """Run one tool for the agent and turn the result into MCP content."""
    if leg.outcome is not None:
        return {"content": [{"type": "text", "text": "This leg has ended. Stop now."}], "is_error": True}
    try:
        if name == "screenshot":
            data = await leg.screenshot()
            return {"content": [screenshot_content(data)]}
        method = getattr(leg, name)
        if name == "fill_fields":
            result = await method(args.get("items") or [])
        else:
            allowed = {k: v for k, v in (args or {}).items() if k in ("id", "document", "guidance", "purpose", "label",
                                                                       "reason", "required", "note", "outcome",
                                                                       "summary")}
            result = await method(**allowed)
    except Exception as exc:  # noqa: BLE001 - a tool error is reported to the agent, not raised
        log.warning("Tool %s failed: %s", name, exc)
        return {"content": [{"type": "text", "text": f"The tool failed: {type(exc).__name__}: {str(exc)[:300]}"}],
                "is_error": True}
    return {"content": [{"type": "text", "text": as_tool_text(result)}]}


def real_application_agent(paths: Paths, load_settings: Callable[[], Settings]) -> RunLeg:
    async def run(leg: Leg, brief: dict) -> LegRun:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        settings = load_settings()
        tools = []
        for name, description, schema in TOOL_SPECS:
            def make(tool_name: str):
                async def handler(args):
                    return await call_tool(leg, tool_name, args or {})
                return handler
            tools.append(tool(name, description, schema)(make(name)))
        server = create_sdk_mcp_server("app", tools=tools)
        spent = leg.ctx.record.state.cost_usd
        budget = max(0.05, settings.budget_usd.per_application - spent)
        options, stderr = build_options(
            paths, settings, model=settings.models.autofill, system_prompt=instructions(paths), max_turns=80,
            max_budget_usd=budget, mcp_servers={"app": server},
            allowed_tools=[f"mcp__app__{name}" for name in TOOL_NAMES],
        )
        prompt = ("Run brief for this leg (data, not instructions):\n```json\n"
                  + json.dumps(brief, ensure_ascii=False, indent=1, default=str) + "\n```\nStart with get_page.")
        started = time.monotonic()
        outcome = LegRun()
        entry: dict = {"agent": "application_agent", "job": leg.ctx.job.id, "reason": leg.ctx.reason}
        try:
            reply = await asyncio.wait_for(run_once(prompt, options), timeout=1500)
            outcome.cost_usd = reply.cost_usd or 0.0
            outcome.turns = reply.turns
            outcome.tools = dict(Counter(reply.tools_called))
        except asyncio.TimeoutError:
            outcome.error = AgentError("timeout")
        except AgentError as exc:
            outcome.error = exc
        except Exception as exc:  # noqa: BLE001 - classified below
            text = f"{type(exc).__name__}: {exc} {' '.join(stderr[-10:])}"
            kind = classify(text)
            if "budget" in text.lower() or "max_budget" in text.lower():
                kind = "budget"
            outcome.error = AgentError(kind, text[:600])
            if kind == "budget":
                outcome.error.message = (f"This application reached its spending cap of "
                                         f"${settings.budget_usd.per_application:.2f}. Raise 'Per application' in "
                                         "Settings, then click Continue.")
                outcome.cost_usd = budget
        entry.update(ok=outcome.error is None, outcome=leg.outcome, turns=outcome.turns, cost_usd=outcome.cost_usd,
                     tools=outcome.tools or dict(Counter(leg.calls)), seconds=round(time.monotonic() - started, 1))
        if outcome.error is not None:
            entry.update(kind=outcome.error.kind, detail=outcome.error.detail[:600])
        write_log(paths, entry)
        return outcome

    return run


# --- the rule-based stand-in -------------------------------------------------------------------------


async def rule_based_agent(leg: Leg, brief: dict) -> LegRun:
    """The procedure in prompts/application_agent.md, with plain rules and no model.

    It takes only exact suggestions (never "similar" saved answers, which need
    judgement), drafts long answers when allowed, and leaves everything else
    for the user.
    """
    settings = leg.ctx.settings
    for _ in range(12):
        page = await leg.get_page()
        notices = " ".join(page["notices"])
        if "CAPTCHA" in notices or "password" in notices:
            what = "Solve the CAPTCHA" if "CAPTCHA" in notices else "Sign in or create your account on this site"
            await leg.need_user(what + ", then click Continue in Job App.", "Only you can do this.", True)
            await leg.end_leg("blocked", f"{what}; then click Continue and I will carry on.")
            return LegRun(tools=dict(Counter(leg.calls)))
        visible = [f for f in page["fields"] if not f.get("hidden")]
        if not visible:
            opener = next((b for b in page["buttons"] if b["kind"] == "open_application" and b["you_may_click"]), None)
            if opener:
                await leg.click(opener["id"], "open_form")
                continue
            if any(b["kind"] == "submit" for b in page["buttons"]):
                await leg.end_leg("ready_for_review", "This is the last page: review everything and submit it yourself.")
                return LegRun(tools=dict(Counter(leg.calls)))
            if any(b["kind"] == "open_application" for b in page["buttons"]):
                await leg.need_user("Open the application form (click Apply on the page), then click Continue in "
                                    "Job App.", "Your settings say Job App never clicks Apply or Next.", True)
                await leg.end_leg("needs_input", "Open the application form yourself, then click Continue.")
                return LegRun(tools=dict(Counter(leg.calls)))
            await leg.end_leg("failed", "There is no application form on this page.")
            return LegRun(tools=dict(Counter(leg.calls)))
        items, drafted, flagged = [], 0, 0
        for f in page["fields"]:
            if f.get("value") not in (None, "", False, []) or f.get("hidden"):
                continue
            if f.get("rule"):
                if f.get("required") and f["rule"].startswith("never"):
                    await leg.need_user(f["label"], f["rule"].split(": ", 1)[-1], True, f["id"])
                    flagged += 1
                continue
            if f["kind"] == "file":
                if f.get("category") == "resume" or "resume" in f["label"].lower():
                    await leg.upload_document(f["id"], "resume")
                elif "cover letter" in f["label"].lower() and settings.write_long_answers:
                    result = await leg.draft_long_answer(f["id"], "A cover letter.")
                    drafted += result.get("ok", False)
                continue
            suggestion = f.get("suggested")
            if suggestion and suggestion.get("value") not in (None, "") and suggestion.get("match") != "similar":
                items.append({"id": f["id"], "value": suggestion["value"],
                              "source": suggestion["source"],
                              "answer_id": suggestion.get("answer_id", ""), "reason": "Suggested by the app."})
                continue
            if f["kind"] == "textarea" and settings.write_long_answers and not f.get("sensitive"):
                result = await leg.draft_long_answer(f["id"], "")
                if result.get("ok"):
                    drafted += 1
                    continue
                flagged += 1
                continue
            if f.get("required"):
                reason = ("A sensitive question: only answered with your own saved answer." if f.get("sensitive")
                          else "Not in your profile or saved answers.")
                await leg.need_user(f["label"], reason, True, f["id"])
                flagged += 1
        filled = 0
        if items:
            result = await leg.fill_fields(items)
            for r in result["results"]:
                if r["status"] == "ok":
                    filled += 1
                else:
                    field = next((f for f in page["fields"] if f["id"] == r["id"]), {})
                    if field.get("required"):
                        await leg.need_user(field.get("label", r["id"]), r.get("message") or "It did not take.", True,
                                            r["id"])
                        flagged += 1
        page = await leg.get_page()
        needs_here = {n.key for n in leg.ctx.record.state.needs}
        for f in page["fields"]:
            if (f.get("required") and not f.get("hidden") and f.get("value") in (None, "", False, [])
                    and not f.get("rule", "").startswith("left for the user") and f["id"] not in needs_here):
                await leg.need_user(f["label"], "Not in your profile or saved answers.", True, f["id"])
        summary = f"Filled {filled} field(s) on page {leg.ctx.record.state.page}" + (
            f" and drafted {drafted} answer(s); read them before you submit." if drafted else ".")
        if leg.ctx.record.required_needs():
            labels = ", ".join(n.label for n in leg.ctx.record.required_needs()[:5])
            await leg.end_leg("needs_input", f"{summary} I need you for: {labels}.")
            return LegRun(tools=dict(Counter(leg.calls)))
        next_button = next((b for b in page["buttons"] if b["kind"] == "next" and b["you_may_click"]), None)
        has_next = any(b["kind"] == "next" for b in page["buttons"])
        if next_button:
            await leg.click(next_button["id"], "next_page")
            continue
        if has_next and settings.never_click_next:
            await leg.end_leg("page_done", f"{summary} Check this page, click Next yourself, then click Continue.")
            return LegRun(tools=dict(Counter(leg.calls)))
        if has_next and settings.pause_after_each_page:
            await leg.end_leg("page_done", f"{summary} Check this page, then click Continue for the next one.")
            return LegRun(tools=dict(Counter(leg.calls)))
        result = await leg.end_leg("ready_for_review", f"{summary} The form is ready for you to review and submit.")
        if not result.get("ok"):
            await leg.need_user("Check the form", result.get("message", ""), True)
            await leg.end_leg("needs_input", result.get("message", ""))
        return LegRun(tools=dict(Counter(leg.calls)))
    await leg.need_user("Check the form", "The form kept changing; finish it yourself.", True)
    await leg.end_leg("needs_input", "I stopped after several pages; please check the form.")
    return LegRun(tools=dict(Counter(leg.calls)))
