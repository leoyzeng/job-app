"""The three search agents: planner, web searcher and fit evaluator.

Each gets its standing instructions from ``prompts/`` and a JSON request.
Their answers are checked here before anything else uses them: lengths are
capped, values are tidied, and the evaluator's verdicts are matched back to
the user's own requirement lines so an agent cannot add, drop or reword one.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from ..agents.runner import AgentReply
from ..core.models import Settings
from ..core.ranking import resume_score
from ..paths import Paths
from ..profile.models import SearchReading
from ..profile.requirements import norm
from ..profile.service import load_instructions
from .models import Posting, SearchPlan
from .text import clip

AskFn = Callable[..., Awaitable[AgentReply]]
# (prompt, instructions, schema, read_page) -> AgentReply
WebAgentFn = Callable[..., Awaitable[AgentReply]]

_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}


def _object(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


PLAN_SCHEMA = _object({
    "queries": _STRS, "kind_words": _STRS, "role_words": _STRS, "exclude_words": _STRS,
    "countries": _STRS, "years": _STRS, "terms": _STRS, "lists": _STRS,
    "companies": {"type": "array", "items": _object({"name": _STR, "why": _STR})},
    "notes": _STR,
})

EVAL_SCHEMA = _object({"results": {"type": "array", "items": _object({
    "key": _STR,
    "hard": {"type": "array", "items": _object({
        "requirement": _STR, "result": {"type": "string", "enum": ["pass", "fail", "unknown"]}, "evidence": _STR})},
    "soft": {"type": "array", "items": _object({
        "preference": _STR, "result": {"type": "string", "enum": ["met", "not_met", "unknown"]}, "evidence": _STR})},
    "resume": _object({"level": {"type": "string", "enum": ["strong", "some", "little", "none"]},
                       "skills": _STRS, "similar_experience": _STR}),
    "concerns": _STRS,
    "summary": _STR, "closed": {"type": "boolean"}, "scam_signs": _STRS,
})}})

WEB_SCHEMA = _object({
    "postings": {"type": "array", "items": _object({
        "company": _STR, "title": _STR, "location": _STR, "url": _STR, "found_on": _STR,
        "is_employer_page": {"type": "boolean"}, "posted_at": _STR, "pay": _STR, "type": _STR,
        "summary": _STR, "why_relevant": _STR, "doubts": _STR,
    })},
    "tried": _STR,
})


def _words(values: Any, limit: int, *, lower: bool = True, length: int = 60) -> list[str]:
    out = []
    for value in values if isinstance(values, list) else []:
        text = " ".join(str(value).split())[:length]
        if lower:
            text = text.lower()
        if text and text not in out:
            out.append(text)
    return out[:limit]


def clean_plan(data: Any, watchlist: list[str]) -> SearchPlan:
    data = data if isinstance(data, dict) else {}
    watched = {norm(n) for n in watchlist}
    companies = []
    for item in data.get("companies") or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:80]
        if name and norm(name) not in watched and name not in [c["name"] for c in companies]:
            companies.append({"name": name, "why": " ".join(str(item.get("why") or "").split())[:160]})
    return SearchPlan(
        queries=_words(data.get("queries"), 12, lower=False, length=120),
        kind_words=_words(data.get("kind_words"), 15),
        role_words=_words(data.get("role_words"), 50),
        exclude_words=_words(data.get("exclude_words"), 30),
        countries=[c.upper() for c in _words(data.get("countries"), 5) if re.fullmatch(r"[a-z]{2}", c)],
        years=[y for y in _words(data.get("years"), 4) if re.fullmatch(r"20\d\d", y)],
        terms=[t.title() for t in _words(data.get("terms"), 6) if re.fullmatch(r"(summer|fall|winter|spring) 20\d\d", t)],
        lists=[x for x in _words(data.get("lists"), 2) if x in ("internships", "new_grad")],
        companies=companies[:25],
        notes=" ".join(str(data.get("notes") or "").split())[:400],
    )


def simple_plan(reading: SearchReading) -> SearchPlan:
    """A plan made by plain rules, used if the planner cannot be reached."""
    types = " ".join(reading.types).lower()
    kind = ["intern", "internship", "co-op", "coop"] if ("intern" in types or "co-op" in types) else []
    lists = ["internships"] if kind else (["new_grad"] if "entry" in types or "new grad" in types else [])
    places = " ".join(reading.locations).lower()
    countries = ["US"] if re.search(r"\b(us|usa|united states)\b", places) else []
    years = sorted(set(re.findall(r"\b(20\d\d)\b", " ".join(h.text for h in reading.hard))))
    terms = [f"Summer {y}" for y in years] if kind else []
    queries = [f"{t} {years[0] if years else ''}".strip() for t in reading.titles[:8]]
    return SearchPlan(queries=queries, kind_words=kind, countries=countries, years=years, terms=terms, lists=lists,
                      notes="Made by simple rules because the planner could not be reached.")


def reading_fingerprint(reading: SearchReading) -> str:
    raw = json.dumps(reading.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _requirements(reading: SearchReading) -> dict:
    return {
        "hard": [{"text": h.text, "test": h.test} for h in reading.hard],
        "soft": [{"text": s.text, "weight": s.weight} for s in reading.soft],
    }


async def make_plan(ask: AskFn, paths: Paths, reading: SearchReading, profile: dict,
                    watchlist: list[str]) -> tuple[SearchPlan, AgentReply]:
    request = {
        **_requirements(reading),
        "titles": reading.titles, "keywords": reading.keywords, "locations": reading.locations,
        "remote_ok": reading.remote_ok, "pay_floor": reading.pay_floor, "types": reading.types,
        "profile": profile, "watchlist": watchlist, "today": date.today().isoformat(),
    }
    reply = await ask(name="search_planner", instructions=load_instructions(paths, "search_planner"),
                      prompt=json.dumps(request, indent=1, ensure_ascii=False), schema=PLAN_SCHEMA)
    return clean_plan(reply.data, watchlist), reply


# --- the fit evaluator ------------------------------------------------------------------------


class Verdict(BaseModel):
    hard: list[dict] = Field(default_factory=list)       # {"requirement", "result", "evidence"}
    soft: list[dict] = Field(default_factory=list)       # {"preference", "weight", "result", "evidence"}
    soft_score: int = 50
    soft_points: Optional[int] = None
    soft_hits: list[str] = Field(default_factory=list)
    soft_misses: list[str] = Field(default_factory=list)
    resume_level: str = ""
    resume_skills: list[str] = Field(default_factory=list)
    resume_note: str = ""
    resume_score: Optional[int] = None
    concerns: list[str] = Field(default_factory=list)
    summary: str = ""
    closed: bool = False
    scam_signs: list[str] = Field(default_factory=list)

    @property
    def failed(self) -> list[dict]:
        return [h for h in self.hard if h["result"] == "fail"]

    @property
    def unknown(self) -> list[dict]:
        return [h for h in self.hard if h["result"] == "unknown"]


def _mentions(text: str, skill: str) -> bool:
    """True if the posting names this skill as a word ("C" is not found inside "CAD")."""
    pattern = r"(?<![\w+#])" + re.escape(skill.lower()) + r"(?![\w+#])"
    return re.search(pattern, text.lower()) is not None


def verified_skills(claimed: Any, profile_skills: list[str], posting_text: str) -> list[str]:
    """The skills the agent says match, kept only if they are in your profile and named in the posting."""
    yours = {norm(skill): skill for skill in profile_skills if skill}
    out = []
    for item in claimed if isinstance(claimed, list) else []:
        mine = yours.get(norm(str(item)))
        if mine and mine not in out and _mentions(posting_text, mine):
            out.append(mine)
    return out[:12]


def clean_verdict(raw: Any, reading: SearchReading, *, posting_text: str = "",
                  profile_skills: Optional[list[str]] = None) -> Verdict:
    """Match the agent's verdict to the user's own requirement lines, one each, in order.

    Soft preferences are scored here, from the agent's met / not met per line and your weights.
    The resume match is only kept when ``profile_skills`` is given (resume match switched on).
    """
    raw = raw if isinstance(raw, dict) else {}
    given = {}
    for item in raw.get("hard") or []:
        if isinstance(item, dict) and item.get("result") in ("pass", "fail", "unknown"):
            given.setdefault(norm(str(item.get("requirement") or "")), item)
    hard = []
    for requirement in reading.hard:
        item = given.get(norm(requirement.text))
        if item is None:
            hard.append({"requirement": requirement.text, "result": "unknown", "evidence": "Not assessed."})
            continue
        result = item["result"]
        evidence = clip(" ".join(str(item.get("evidence") or "").split()), 300)
        if result == "fail" and not evidence:
            result = "unknown"  # a failure has to point at something
        hard.append({"requirement": requirement.text, "result": result, "evidence": evidence})

    soft_given = {}
    for item in raw.get("soft") or []:
        if isinstance(item, dict) and item.get("result") in ("met", "not_met", "unknown"):
            soft_given.setdefault(norm(str(item.get("preference") or "")), item)
    soft, points, total = [], 0, 0
    for preference in reading.soft:
        weight = max(1, int(preference.weight or 1))
        item = soft_given.get(norm(preference.text)) or {}
        result = item.get("result", "unknown")
        evidence = clip(" ".join(str(item.get("evidence") or "").split()), 200)
        if result != "unknown" and not evidence:
            result = "unknown"
        soft.append({"preference": preference.text, "weight": weight, "result": result, "evidence": evidence})
        total += weight
        points += weight if result == "met" else -weight if result == "not_met" else 0
    if soft_given or not raw.get("soft_score"):
        soft_points: Optional[int] = points
        score = 50 + round(50 * points / total) if total else 50
        hits = [s["preference"] for s in soft if s["result"] == "met"]
        misses = [s["preference"] for s in soft if s["result"] == "not_met"]
    else:  # an older evaluator that gives only a score
        soft_points = None
        try:
            score = int(raw.get("soft_score", 50))
        except (TypeError, ValueError):
            score = 50
        hits = _words(raw.get("soft_hits"), 8, lower=False, length=120)
        misses = _words(raw.get("soft_misses"), 8, lower=False, length=120)

    level, skills, note, rscore = "", [], "", None
    if profile_skills is not None:
        resume = raw.get("resume") if isinstance(raw.get("resume"), dict) else {}
        level = resume.get("level") if resume.get("level") in ("strong", "some", "little", "none") else "none"
        skills = verified_skills(resume.get("skills"), profile_skills, posting_text)
        note = clip(" ".join(str(resume.get("similar_experience") or "").split()), 200) if level != "none" else ""
        rscore = resume_score(level, len(skills))
    return Verdict(
        hard=hard,
        soft=soft,
        soft_score=max(0, min(100, score)),
        soft_points=soft_points,
        soft_hits=hits,
        soft_misses=misses,
        resume_level=level,
        resume_skills=skills,
        resume_note=note,
        resume_score=rscore,
        concerns=_words(raw.get("concerns"), 8, lower=False, length=160),
        summary=clip(" ".join(str(raw.get("summary") or "").split()), 700),
        closed=raw.get("closed") is True,
        scam_signs=_words(raw.get("scam_signs"), 5, lower=False, length=120),
    )


def match_fields(verdict: Verdict) -> dict:
    """What a verdict puts on a job's Match."""
    return {
        "soft_score": verdict.soft_score, "soft_points": verdict.soft_points, "soft_hits": verdict.soft_hits,
        "soft_misses": verdict.soft_misses, "resume_score": verdict.resume_score,
        "resume_level": verdict.resume_level, "resume_skills": verdict.resume_skills,
        "resume_note": verdict.resume_note,
    }


def posting_for_agent(key: str, posting: Posting, limit: int = 7000) -> dict:
    return {
        "key": key, "company": posting.company, "title": posting.title, "location": posting.location,
        "pay": posting.pay, "type": posting.type, "remote": posting.remote, "posted_at": posting.posted_at,
        "notes": posting.notes[:6], "description": clip(posting.description, limit),
    }


async def evaluate(ask: AskFn, paths: Paths, reading: SearchReading, profile: dict,
                   items: list[tuple[str, Posting]], *, resume: Optional[dict] = None
                   ) -> tuple[dict[str, Verdict], AgentReply]:
    """Judge postings. With ``resume`` (skills, positions, projects) each also gets a resume match."""
    request = {
        **_requirements(reading), "profile": profile, "today": date.today().isoformat(),
        "postings": [posting_for_agent(key, posting) for key, posting in items],
    }
    if resume:
        request["resume"] = resume
    reply = await ask(name="fit_evaluator", instructions=load_instructions(paths, "fit_evaluator"),
                      prompt=json.dumps(request, indent=1, ensure_ascii=False), schema=EVAL_SCHEMA)
    results = (reply.data or {}).get("results") if isinstance(reply.data, dict) else None
    verdicts: dict[str, Verdict] = {}
    for raw in results if isinstance(results, list) else []:
        key = str((raw or {}).get("key") or "") if isinstance(raw, dict) else ""
        if key in dict(items) and key not in verdicts:
            posting = dict(items)[key]
            verdicts[key] = clean_verdict(raw, reading, posting_text=f"{posting.title}\n{posting.description}",
                                          profile_skills=(resume or {}).get("skills") if resume else None)
    return verdicts, reply


# --- the web search agent ------------------------------------------------------------------------


def clean_web_results(data: Any, limit: int) -> list[dict]:
    out = []
    postings = data.get("postings") if isinstance(data, dict) else None
    for item in postings if isinstance(postings, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = " ".join(str(item.get("title") or "").split())[:200]
        company = " ".join(str(item.get("company") or "").split())[:200]
        if not url.lower().startswith(("http://", "https://")) or not title or not company:
            continue
        clean = {k: " ".join(str(item.get(k) or "").split())[:400] for k in (
            "location", "found_on", "posted_at", "pay", "type", "summary", "why_relevant", "doubts")}
        clean.update(url=url[:2000], title=title, company=company, is_employer_page=item.get("is_employer_page") is True)
        out.append(clean)
    return out[:limit]


async def web_search(agent: WebAgentFn, paths: Paths, reading: SearchReading, queries: list[str],
                     max_results: int, read_page: Callable[[str], Awaitable[str]]) -> tuple[list[dict], AgentReply]:
    request = {**_requirements(reading), "queries": queries, "max_results": max_results,
               "today": date.today().isoformat()}
    reply = await agent(prompt=json.dumps(request, indent=1, ensure_ascii=False),
                        instructions=load_instructions(paths, "search_agent"), schema=WEB_SCHEMA, read_page=read_page)
    return clean_web_results(reply.data, max_results), reply


def real_web_agent(paths: Paths, load_settings: Callable[[], Settings]) -> WebAgentFn:
    """The web search agent, run with the Claude Agent SDK: WebSearch plus the app's own page reader, nothing else."""
    from ..agents.runner import ask_structured

    async def run(*, prompt: str, instructions: str, schema: dict, read_page) -> AgentReply:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        @tool("read_page", "Read one public web page and return its text. Says when the page is on the employer's "
              "own applicant system, and when a posting is closed. Refuses some job boards.", {"url": str})
        async def read_page_tool(args):
            text = await read_page(str(args.get("url") or ""))
            return {"content": [{"type": "text", "text": text}]}

        settings = load_settings()
        server = create_sdk_mcp_server("jobapp", tools=[read_page_tool])
        return await ask_structured(
            paths, settings, name="search_agent", instructions=instructions, prompt=prompt, schema=schema,
            model=settings.models.search, timeout=900, max_turns=30, max_budget_usd=0.6, fallback=False,
            extra={"tools": ["WebSearch"], "mcp_servers": {"jobapp": server},
                   "allowed_tools": ["WebSearch", "mcp__jobapp__read_page"]},
        )

    return run
