"""The check command's test of autofill with the real application agent.

The agent fills the Greenhouse-style practice form in Job App's browser window
for one leg, with the same tools and limits as a real application. Nothing is
sent anywhere: the practice form is served from this computer. It starts with no
saved answers, so it should stop for the two sensitive questions and never
click Submit. This uses some of your Claude usage (one agent run, and one or
two drafts if "Write cover letters and long answers" is on).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime

from ..core.models import Job, Settings
from ..paths import Paths
from . import Result


async def autofill_check(paths: Paths, settings: Settings, headless: bool) -> Result:
    from ..agents.runner import ask_structured
    from ..apply.agent import real_application_agent
    from ..apply.answers import AnswerStore
    from ..apply.questions import QuestionMatcher
    from ..apply.record import ApplicationRecord
    from ..apply.tools import Leg, LegContext, build_brief
    from ..apply.writer import Writer
    from ..browser.manager import DedicatedWindow
    from ..core.jsonfile import read_json
    from ..profile.demo import SAMPLE_FACTS, SAMPLE_RESUME_LINES, simple_pdf
    from ..profile.models import Facts, ProfileFile
    from ..profile.resume import current_resume
    from .form_checks import PracticeSite

    folder = paths.logs / "autofill_check" / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    facts, whose = None, "your confirmed profile"
    try:
        profile = ProfileFile.model_validate(read_json(paths.profile / "profile.json"))
        if profile.confirmed:
            facts = profile.facts
    except Exception:  # noqa: BLE001 - no profile yet: the sample person is used
        pass
    profile_folder = paths.profile
    resume = current_resume(paths.profile)
    if facts is None or resume is None:
        whose = "the made-up sample person (confirm your profile and add your resume to test with yours)"
        facts = Facts.model_validate(SAMPLE_FACTS)
        profile_folder = folder / "profile"
        profile_folder.mkdir(exist_ok=True)
        resume = profile_folder / "resume.pdf"
        resume.write_bytes(simple_pdf(SAMPLE_RESUME_LINES))

    async def ask(*, name, instructions, prompt, schema):
        return await ask_structured(paths, settings, name=name, instructions=instructions, prompt=prompt,
                                    schema=schema, model=settings.models.writer, timeout=300, max_turns=4,
                                    max_budget_usd=0.6)

    async def ask_matcher(*, name, instructions, prompt, schema):
        return await ask_structured(paths, settings, name=name, instructions=instructions, prompt=prompt,
                                    schema=schema, model=settings.models.search, timeout=150, max_turns=3,
                                    max_budget_usd=0.3)

    window = DedicatedWindow(paths.browser_profile, headless=headless)
    details: dict = {"facts": whose, "folder": str(folder), "model": settings.models.autofill}
    try:
        async with PracticeSite(paths.ui / "practice") as site:
            page, problem = await window.open(f"{site.base}/greenhouse.html", settings.browser.channel)
            if problem:
                return Result("autofill", "fail", f"The practice form did not open ({problem}).",
                              fix="See the browser test above.", details=details)
            await page.wait_for_load_state("networkidle")
            job = Job(id="check_autofill", company="Practice Robotics", title="Robotics Software Intern",
                      url=f"{site.base}/greenhouse.html")
            ctx = LegContext(job=job, page=page, record=ApplicationRecord(folder / "application"),
                             answers=AnswerStore(folder / "answers.json"), settings=settings, facts=facts,
                             resume=resume, profile_folder=profile_folder, reason="prepare",
                             site_notes=folder / "site_notes",
                             writer=Writer(paths, ask) if settings.write_long_answers else None,
                             matcher=QuestionMatcher(paths, ask_matcher) if settings.match_questions_by_meaning else None)
            leg = Leg(ctx)
            run = real_application_agent(paths, lambda: settings)
            result = await asyncio.wait_for(run(leg, build_brief(ctx, [])), timeout=1600)
            submitted = await page.locator("#practice-sent").count() > 0
            record = ctx.record
            filled = [f for f in record.fields if f.verified]
            details.update(
                outcome=leg.outcome, summary=leg.summary, cost_usd=result.cost_usd, turns=result.turns,
                tools=result.tools or dict(Counter(leg.calls)),
                filled=[{"label": f.label, "source": f.source, "value": f.value if not f.category.startswith("sensitive")
                         else "(answered)"} for f in record.fields],
                needs=[{"label": n.label, "reason": n.reason, "required": n.required} for n in record.state.needs],
                review_notes=record.state.review_notes, submitted=submitted,
                error=(result.error.kind + ": " + result.error.detail[:600]) if result.error else "",
            )
    except Exception as exc:  # noqa: BLE001
        details["error"] = f"{type(exc).__name__}: {str(exc)[:1500]}"
        return Result("autofill", "fail", f"The autofill test stopped: {str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__}",
                      fix="The log file below has the details.", details=details)
    finally:
        await window.close()
    if details["submitted"]:
        return Result("autofill", "fail", "The practice form was submitted. This must never happen; please report it.",
                      details=details)
    if leg.outcome is None:
        why = details["error"] or "the agent stopped without finishing"
        return Result("autofill", "fail", f"The application agent did not finish the practice form ({why[:160]}).",
                      fix="If it says the usage limit was reached, try again later. Otherwise the log file has details.",
                      details=details)
    sensitive_left = sum(1 for n in details["needs"] if "authorized" in n["label"] or "sponsorship" in n["label"])
    summary = (f"The application agent filled {len(filled)} field(s) on the practice form and stopped with "
               f"'{leg.outcome}' (about ${details['cost_usd'] or 0:.2f} of usage). Nothing was submitted.")
    if leg.outcome not in ("needs_input", "ready_for_review") or len(filled) < 6 or sensitive_left < 2:
        return Result("autofill", "warn", summary + " That is less than expected; the log file shows what it did.",
                      details=details)
    return Result("autofill", "ok", summary, details=details)
