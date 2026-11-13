"""data/answers.json: your answers to application questions, learned as you go (PLAN.md 4.4).

Every time you fill or change a field and click Continue (or Mark as
applied), your answer is saved with the question's wording. Next time the
same question comes up, the agent is offered your answer. A change to
something the agent entered replaces the old answer, which is kept in the
answer's history, so the same mistake is not made twice.

* Sensitive answers (work authorization, sponsorship, salary, demographics
  and the like) are only reused exactly as you gave them.
* Job-specific answers ("Why do you want to work at Acme?") are not reused in
  other applications; they are kept as samples of your writing.
* Passwords, ID numbers, payment details and agreement boxes are never saved.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.jsonfile import JsonCorrupt, read_json, write_json_atomic
from ..core.models import now_iso

log = logging.getLogger("jobapp.answers")

STOP = {
    "a", "an", "the", "you", "your", "are", "is", "do", "does", "did", "have", "has", "be", "to", "of", "in",
    "for", "on", "at", "or", "and", "with", "this", "that", "will", "would", "can", "please", "if", "any",
    "we", "our", "us", "i", "my", "me", "what", "which", "how", "currently", "now", "e", "g", "eg",
}
JOB_SPECIFIC = re.compile(
    r"\bwhy (do you want|are you interested|would you like|do you wish)|\bwhy (this|our) (role|company|team|position)"
    r"|what (excites|interests|attracts|draws) you|\binterest(ed)? in (joining|working)|tell us why")
CHOICE_KINDS = {"select", "radio", "combobox", "choice", "checkbox", "multiselect", "checkbox_group"}


def norm_question(text: str) -> str:
    text = re.sub(r"\(required\)|\*|✱", " ", str(text or "").lower())
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", text).split())


def content_words(text: str) -> set[str]:
    return {w for w in norm_question(text).split() if w not in STOP and len(w) > 1}


def family(kind: str) -> str:
    return "choice" if kind in CHOICE_KINDS else ("file" if kind == "file" else "text")


class HistoryItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    answer: Any = None
    at: str = ""
    source: str = ""
    job: str = ""


class Answer(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    question: str
    also_asked_as: list[str] = Field(default_factory=list)
    field_kind: str = "text"
    options_seen: list[str] = Field(default_factory=list)
    answer: Any = ""
    source: str = "user"                 # user, user_correction, profile
    sensitivity: str = "normal"          # normal, sensitive
    scope: str = "global"                # global, job_specific
    topic: str = ""                      # what it is about, from the question matcher (e.g. work_authorization)
    place: str = ""                      # the country it is about, when it is about one
    times_used: int = 0
    first_seen: dict = Field(default_factory=dict)
    updated_at: str = Field(default_factory=now_iso)
    history: list[HistoryItem] = Field(default_factory=list)

    def wordings(self) -> list[str]:
        return [self.question] + self.also_asked_as


class AnswersFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    next_id: int = 1
    answers: list[Answer] = Field(default_factory=list)


class Candidate(BaseModel):
    answer: Answer
    match: str            # exact or similar
    score: float


class AnswerStore:
    def __init__(self, path: Path):
        self.path = path
        self._mtime: Optional[float] = None
        self.data = AnswersFile()
        self._load()

    def _load(self) -> None:
        try:
            self.data = AnswersFile.model_validate(read_json(self.path))
            self._mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.data = AnswersFile()
        except (JsonCorrupt, ValidationError) as exc:
            log.error("answers.json could not be read: %s", exc)
            raise

    def _fresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            self._load()

    def save(self) -> None:
        write_json_atomic(self.path, self.data.model_dump(mode="json"))
        self._mtime = self.path.stat().st_mtime

    # --- reading -----------------------------------------------------------------------------

    def all(self) -> list[Answer]:
        self._fresh()
        return list(self.data.answers)

    def get(self, answer_id: str) -> Answer:
        self._fresh()
        for answer in self.data.answers:
            if answer.id == answer_id:
                return answer
        raise KeyError(answer_id)

    def candidates(self, question: str, kind: str, *, company: str = "") -> list[Candidate]:
        """Saved answers that may answer this question, best first. The agent decides whether "similar" ones fit."""
        self._fresh()
        wanted = norm_question(question)
        words = content_words(question)
        if not wanted:
            return []
        out: list[Candidate] = []
        for answer in self.data.answers:
            if answer.scope == "job_specific":
                continue
            if family(answer.field_kind) != family(kind):
                continue
            best, match = 0.0, ""
            for wording in answer.wordings():
                if norm_question(wording) == wanted:
                    best, match = 1.0, "exact"
                    break
                theirs = content_words(wording)
                if not theirs or not words:
                    continue
                score = len(words & theirs) / len(words | theirs)
                if score > best:
                    best, match = score, "similar"
            if match == "exact" or (match == "similar" and best >= 0.5):
                out.append(Candidate(answer=answer, match=match, score=round(best, 2)))
        out.sort(key=lambda c: (c.match != "exact", -c.score))
        return out[:3]

    def writing_samples(self, limit: int = 4) -> list[dict]:
        """Long answers you wrote or edited, newest first: samples of your voice for the writing agent."""
        self._fresh()
        samples = [a for a in self.data.answers
                   if isinstance(a.answer, str) and len(a.answer) >= 120 and a.source in ("user", "user_correction")]
        samples.sort(key=lambda a: a.updated_at, reverse=True)
        return [{"question": a.question, "answer": a.answer} for a in samples[:limit]]

    # --- learning ----------------------------------------------------------------------------

    def _new_id(self) -> str:
        answer_id = f"a_{self.data.next_id:04d}"
        self.data.next_id += 1
        return answer_id

    def learn(self, question: str, kind: str, options: list[str], value: Any, *, source: str, job_id: str,
              sensitive: bool, company: str = "", topic: str = "", place: str = "") -> tuple[Optional[Answer], str]:
        """Save your answer to a question. Returns (answer, "created" | "updated" | "corrected" | "same" | "skipped")."""
        self._fresh()
        if value in (None, "", []) or not norm_question(question):
            return None, "skipped"
        scope = "global"
        text = norm_question(question)
        if JOB_SPECIFIC.search(text) or (company and norm_question(company) and norm_question(company) in text):
            scope = "job_specific"
        existing = next((a for a in self.data.answers
                         if family(a.field_kind) == family(kind) and a.scope == scope
                         and any(norm_question(w) == text for w in a.wordings())
                         and (scope == "global" or a.first_seen.get("job") == job_id)), None)
        now = now_iso()
        if existing is not None:
            if topic and not existing.topic:
                existing.topic, existing.place = topic, place or existing.place
            if existing.answer == value:
                self.save()
                return existing, "same"
            existing.history.append(HistoryItem(answer=existing.answer, at=existing.updated_at, source=existing.source,
                                                job=existing.first_seen.get("job", "")))
            existing.history = existing.history[-10:]
            existing.answer = value
            existing.source = source
            existing.updated_at = now
            existing.options_seen = list(dict.fromkeys(existing.options_seen + list(options or [])))[:40]
            if sensitive:
                existing.sensitivity = "sensitive"
            self.save()
            return existing, "corrected" if source == "user_correction" else "updated"
        answer = Answer(
            id=self._new_id(), question=" ".join(str(question).split())[:500], field_kind=kind,
            options_seen=list(options or [])[:40], answer=value, source=source,
            sensitivity="sensitive" if sensitive else "normal", scope=scope, topic=topic, place=place,
            first_seen={"job": job_id, "at": now[:10]}, updated_at=now,
        )
        self.data.answers.append(answer)
        self.save()
        return answer, "created"

    def used(self, answer_id: str) -> None:
        try:
            answer = self.get(answer_id)
        except KeyError:
            return
        answer.times_used += 1
        self.save()

    def add_wording(self, answer_id: str, question: str) -> None:
        """A new wording for an answer, added once you have seen it used and left it unchanged."""
        try:
            answer = self.get(answer_id)
        except KeyError:
            return
        if norm_question(question) and all(norm_question(w) != norm_question(question) for w in answer.wordings()):
            answer.also_asked_as.append(" ".join(question.split())[:500])
            self.save()

    # --- your edits on the Answers screen ---------------------------------------------------------

    def edit(self, answer_id: str, *, answer: Any = None, sensitivity: Optional[str] = None,
             scope: Optional[str] = None) -> Answer:
        item = self.get(answer_id)
        if answer is not None and answer != item.answer:
            item.history.append(HistoryItem(answer=item.answer, at=item.updated_at, source=item.source))
            item.history = item.history[-10:]
            item.answer = answer
            item.source = "user"
        if sensitivity in ("normal", "sensitive"):
            item.sensitivity = sensitivity
        if scope in ("global", "job_specific"):
            item.scope = scope
        item.updated_at = now_iso()
        self.save()
        return item

    def delete(self, answer_id: str) -> None:
        self.get(answer_id)
        self.data.answers = [a for a in self.data.answers if a.id != answer_id]
        self.save()
