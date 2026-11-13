"""Recognising application questions by meaning (Settings: "Recognise questions by meaning").

The wording rules in forms/classify.py catch the usual phrasings of sensitive
questions, and answers.py matches your saved answers by wording. This module
adds a question matcher agent that reads each question with its context (the
section, the headings, the choices, the questions around it, the job's
location) and says what it really asks, whether it is sensitive, and which of
your saved answers answers it.

The agent only proposes. Code decides what may be used:

* a question is made sensitive if the agent says so, never the other way round;
* a match must point at one of your saved answers, of the same type (a choice,
  text or a file), not one kept for a single job;
* a "same" match on a choice question must pick an option, and if your saved
  answer maps onto an option by itself, it must be that option;
* a "same" match on a text question fills your saved answer exactly;
* a "derived" match (worked out from a related answer) is only allowed for
  work authorization, sponsorship, visa status and citizenship, only on a
  choice question, and only for the same place;
* a match is refused when your saved answer was recorded for a different topic
  or a different place.

Every answer reused this way is listed on the listing for you to check.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable, Optional

from ..agents.runner import AgentError
from ..core.models import Job
from ..forms.fill import match_option, norm
from ..forms.model import Field, Reading
from ..paths import Paths
from ..search.text import clip, in_us
from .answers import Answer, AnswerStore, family

log = logging.getLogger("jobapp.questions")

TOPICS = ("work_authorization", "sponsorship", "visa_status", "citizenship", "salary", "criminal", "clearance", "age",
          "gender", "race_ethnicity", "veteran", "disability", "sexual_orientation", "pronouns", "start_date",
          "relocation", "other")
SENSITIVE_TOPICS = set(TOPICS[:14])
DERIVABLE = {"work_authorization", "sponsorship", "visa_status", "citizenship"}
CERTAIN = {"preferred_name", "first_name", "last_name", "full_name", "email", "phone", "linkedin", "github", "website",
           "country", "city", "resume", "cover_letter", "school", "degree", "discipline"}

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["questions"],
    "properties": {"questions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "asks", "topic", "sensitive", "place", "match"],
        "properties": {
            "id": {"type": "string"}, "asks": {"type": "string"},
            "topic": {"type": "string", "enum": list(TOPICS)}, "sensitive": {"type": "boolean"},
            "place": {"type": "string"},
            "match": {"type": "object", "additionalProperties": False,
                      "required": ["answer_id", "relation", "value", "why"],
                      "properties": {"answer_id": {"type": "string"},
                                     "relation": {"type": "string", "enum": ["same", "derived", "none"]},
                                     "value": {"type": "string"}, "why": {"type": "string"}}},
        }}}},
}

AskFn = Callable[..., Awaitable[Any]]


@dataclass
class QuestionInfo:
    key: str
    label: str
    asks: str = ""
    topic: str = "other"
    sensitive: bool = False
    place: str = ""
    answer_id: str = ""
    relation: str = "none"           # same, derived, none
    value: Any = None
    why: str = ""
    saved_question: str = ""
    refused: str = ""                # why code refused the agent's match, for the log

    def as_dict(self) -> dict:
        return asdict(self)


def job_country(job: Job) -> str:
    return "United States" if in_us(job.location) is True else ""


def matchable(f: Field) -> bool:
    """Fields worth asking the matcher about: questions, not profile fields, files or fields left to you."""
    return (f.visible and not f.rule and f.kind not in ("file", "password")
            and f.category not in CERTAIN and bool((f.label or "").strip()))


def _same_place(a: str, b: str) -> bool:
    return not a or not b or norm(a) == norm(b)


def _as_checkbox(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = norm(value)
    if text in ("true", "yes", "checked", "ticked", "1"):
        return True
    if text in ("false", "no", "unchecked", "0"):
        return False
    return None


def check_match(f: Field, info: QuestionInfo, answer: Optional[Answer], proposed: str) -> tuple[Any, str]:
    """(value to fill, "") if the agent's proposed match may be used, else (None, why not)."""
    if answer is None:
        return None, "not one of your saved answers"
    if answer.scope == "job_specific":
        return None, "that answer was written for one job"
    if family(answer.field_kind) != family(f.kind):
        return None, "that answer is for a different kind of question"
    recorded_topic = getattr(answer, "topic", "") or ""
    if recorded_topic and recorded_topic != info.topic and not (
            info.relation == "derived" and recorded_topic in DERIVABLE and info.topic in DERIVABLE):
        return None, f"that answer was recorded for {recorded_topic.replace('_', ' ')}"
    if not _same_place(getattr(answer, "place", "") or "", info.place):
        return None, f"that answer was for {answer.place}, this question is about {info.place}"
    saved = answer.answer
    if info.relation == "derived":
        if info.topic not in DERIVABLE:
            return None, "only work authorization, sponsorship, visa and citizenship answers may be worked out"
        if not f.options:
            return None, "a worked-out answer is only used on a choice question"
    if f.kind == "checkbox" and not f.options:
        value = _as_checkbox(proposed if info.relation == "derived" else saved)
        return (value, "") if value is not None else (None, "not a yes/no answer")
    if f.options:
        chosen = match_option(proposed, f.options) if proposed else None
        if chosen is None:
            return None, "the proposed answer is not one of the choices"
        if info.relation == "same" and isinstance(saved, str):
            direct = match_option(saved, f.options)
            if direct is not None and direct != chosen:
                return None, f"your saved answer '{saved}' is the choice '{direct}'"
        return chosen, ""
    if info.relation != "same":
        return None, "a text question only takes your saved answer as it is"
    return saved, ""


class QuestionMatcher:
    def __init__(self, paths: Paths, ask: AskFn):
        self.paths = paths
        self.ask = ask

    def _instructions(self) -> str:
        return ((self.paths.prompts / "shared_rules.md").read_text(encoding="utf-8") + "\n\n"
                + (self.paths.prompts / "question_matcher.md").read_text(encoding="utf-8"))

    def request(self, job: Job, reading: Reading, fields: list[Field], answers: AnswerStore) -> dict:
        labels = [f.label for f in reading.fields if f.visible]
        questions = []
        for f in fields:
            index = labels.index(f.label) if f.label in labels else -1
            questions.append({
                "id": f.key, "label": f.label, "section": f.section, "kind": f.kind,
                "options": f.options[:30], "placeholder": f.placeholder, "required": f.required,
                "before": labels[index - 1] if index > 0 else "",
                "after": labels[index + 1] if 0 <= index < len(labels) - 1 else "",
            })
        saved = [{"id": a.id, "question": a.question, "also_asked_as": a.also_asked_as[:3],
                  "answer": clip(str(a.answer), 200) if not isinstance(a.answer, (bool, list)) else a.answer,
                  "topic": getattr(a, "topic", "") or "", "place": getattr(a, "place", "") or ""}
                 for a in answers.all() if a.scope != "job_specific"]
        return {
            "job": {"company": job.company, "title": job.title, "location": job.location, "country": job_country(job)},
            "page": {"title": reading.title, "step": reading.progress, "headings": reading.headings[:8]},
            "questions": questions, "saved_answers": saved[:80], "today": date.today().isoformat(),
        }

    async def read(self, job: Job, reading: Reading, fields: list[Field],
                   answers: AnswerStore) -> tuple[dict[str, QuestionInfo], float]:
        """What each field asks and which saved answer may answer it. Returns ({key: info}, cost)."""
        if not fields:
            return {}, 0.0
        request = self.request(job, reading, fields[:40], answers)
        try:
            reply = await self.ask(name="question_matcher", instructions=self._instructions(),
                                   prompt=json.dumps(request, ensure_ascii=False, indent=1), schema=SCHEMA)
        except AgentError as exc:
            log.warning("The question matcher failed (%s); only exact wording is matched on this page.", exc.kind)
            return {}, 0.0
        cost = getattr(reply, "cost_usd", None) or 0.0
        return self.validate(reply.data, fields, answers), cost

    def validate(self, data: Any, fields: list[Field], answers: AnswerStore) -> dict[str, QuestionInfo]:
        by_key = {f.key: f for f in fields}
        out: dict[str, QuestionInfo] = {}
        for item in (data or {}).get("questions") or [] if isinstance(data, dict) else []:
            if not isinstance(item, dict) or item.get("id") not in by_key or item["id"] in out:
                continue
            f = by_key[item["id"]]
            topic = item.get("topic") if item.get("topic") in TOPICS else "other"
            info = QuestionInfo(
                key=f.key, label=f.label, asks=clip(" ".join(str(item.get("asks") or "").split()), 200), topic=topic,
                sensitive=topic in SENSITIVE_TOPICS or item.get("sensitive") is True,
                place=clip(" ".join(str(item.get("place") or "").split()), 60),
            )
            match = item.get("match") if isinstance(item.get("match"), dict) else {}
            relation = match.get("relation") if match.get("relation") in ("same", "derived") else "none"
            if relation != "none":
                info.relation = relation
                try:
                    answer = answers.get(str(match.get("answer_id") or ""))
                except KeyError:
                    answer = None
                value, refused = check_match(f, info, answer, str(match.get("value") or ""))
                if refused:
                    info.relation, info.refused = "none", refused
                    log.info("Question matcher's match for %r refused: %s", f.label, refused)
                else:
                    info.answer_id, info.value, info.saved_question = answer.id, value, answer.question
                    info.why = clip(" ".join(str(match.get("why") or "").split()), 240)
            out[f.key] = info
        return out


# --- the stand-in for sample-data mode and tests --------------------------------------------------

TOPIC_WORDS = [
    ("work_authorization", r"authori[sz]ed to work|eligib(le|ility) to work|right to work|(permitted|allowed|able) to "
                           r"(legally )?work|work permit|work authori[sz]ation"),
    ("sponsorship", r"sponsor|need a visa|require a visa|visa to work|h-?1b|immigration support"),
    ("citizenship", r"citizen|nationality|permanent resident|green card"),
    ("visa_status", r"visa status|current visa|immigration status|type of visa"),
    ("salary", r"salary|compensation|pay expectation|expected pay|desired pay|hourly rate"),
    ("criminal", r"criminal|convicted|conviction|felony"),
    ("clearance", r"clearance"),
    ("age", r"date of birth|\bage\b|18 years|years of age|how old"),
    ("gender", r"\bgender\b|\bsex\b"),
    ("race_ethnicity", r"\brace\b|ethnic|hispanic|latin"),
    ("veteran", r"veteran|military service"),
    ("disability", r"disabilit"),
    ("sexual_orientation", r"sexual orientation"),
    ("pronouns", r"pronoun"),
    ("start_date", r"start date|available to start|earliest start|when can you start"),
    ("relocation", r"relocat"),
]
PLACES = [("United States", r"united states|\bu\.?s\.?a?\b|america"), ("Canada", r"canada"),
          ("United Kingdom", r"united kingdom|\buk\b|britain")]
JOB_COUNTRY = re.compile(r"country (in which|where) (this|the) (job|role|position)|this country|that country")


def _topic(text: str) -> str:
    lowered = text.lower()
    return next((name for name, pattern in TOPIC_WORDS if re.search(pattern, lowered)), "other")


def _place(text: str, country: str) -> str:
    lowered = text.lower()
    if JOB_COUNTRY.search(lowered):
        return country
    return next((name for name, pattern in PLACES if re.search(pattern, lowered)), "")


def demo_match(request: dict) -> dict:
    """Word rules standing in for the question matcher: only "same" matches, same topic and place."""
    country = (request.get("job") or {}).get("country", "")
    saved = request.get("saved_answers") or []
    out = []
    for q in request.get("questions") or []:
        context = f"{q.get('section', '')} {q['label']}"
        topic, place = _topic(context), _place(q["label"], country)
        match = {"answer_id": "", "relation": "none", "value": "", "why": ""}
        if topic != "other":
            for answer in saved:
                theirs = answer.get("topic") or _topic(answer["question"])
                where = answer.get("place") or _place(answer["question"], "")
                if theirs == topic and _same_place(where, place) and not isinstance(answer["answer"], list):
                    value = answer["answer"]
                    if q.get("options") and isinstance(value, str):
                        value = match_option(value, q["options"]) or ""
                    if value not in ("", None):
                        match = {"answer_id": answer["id"], "relation": "same", "value": str(value),
                                 "why": f"Same question as '{answer['question']}'."}
                        break
        out.append({"id": q["id"], "asks": q["label"], "topic": topic, "sensitive": topic in SENSITIVE_TOPICS,
                    "place": place, "match": match})
    return {"questions": out}
