"""Cover letters and long answers, in your voice and only from your facts (PLAN.md 8.4).

The writing agent drafts; this module checks the draft before anything is put
on a form:

* every number in the draft must appear in your resume, profile, the posting
  or the question;
* every fact the agent says it used must be found in your resume or profile;
* no leftover placeholders such as "[Company]", and nothing over the limit.

A draft that fails is sent back once with the reasons. If it fails again it
is not used, and the field goes on your "needs you" list. Every draft is kept
in the application's drafts folder and marked AI-drafted on the listing.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..agents.runner import AgentError
from ..forms.model import Field
from ..paths import Paths
from .answers import content_words

log = logging.getLogger("jobapp.writer")

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "cannot_answer"]},
        "text": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["status", "text", "facts_used", "notes", "reason"],
}

AskFn = Callable[..., Awaitable[Any]]
NUMBER = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?")
PLACEHOLDER = re.compile(r"\[[^\]]{1,40}\]|\{[^}]{1,40}\}|<[^>]{1,40}>|\bXX+\b|lorem ipsum", re.I)
WORD_LIMIT = re.compile(r"(\d{2,4})\s*(?:words?\b|-word)", re.I)
CHAR_LIMIT = re.compile(r"(\d{2,5})\s*(?:characters?|chars?)\b", re.I)
SLUG = re.compile(r"[^a-z0-9]+")


def _numbers(text: str) -> set[str]:
    return {m.group(0).strip("$%").replace(",", "") for m in NUMBER.finditer(text or "")}


def limits(field: Field, guidance: str) -> tuple[Optional[int], Optional[int]]:
    """(word limit, character limit) from the field and the question's wording."""
    text = f"{field.label} {field.placeholder} {guidance}"
    words = WORD_LIMIT.search(text)
    chars = CHAR_LIMIT.search(text)
    char_limit = int(chars.group(1)) if chars else None
    if field.maxlength and field.maxlength > 0:
        char_limit = min(char_limit or field.maxlength, field.maxlength)
    return (int(words.group(1)) if words else None), char_limit


def tidy(text: str) -> str:
    text = re.sub(r"^[ \t]*#+[ \t]*", "", text or "", flags=re.M)
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"^[ \t]*[-*•][ \t]+", "", text, flags=re.M)
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text.strip())]
    return "\n\n".join(p for p in paragraphs if p)


def supported(claim: str, source_words: set[str]) -> bool:
    words = {w for w in content_words(claim) if len(w) > 2}
    if not words:
        return True
    return len(words & source_words) / len(words) >= 0.6


def problems(draft: dict, *, source_text: str, other_text: str, word_limit: Optional[int],
             char_limit: Optional[int]) -> list[str]:
    """Why a draft cannot be used, or [] if it can."""
    text = draft.get("text") or ""
    out = []
    if not text.strip():
        return ["The draft is empty."]
    allowed = _numbers(source_text) | _numbers(other_text)
    stray = sorted(n for n in _numbers(text) if n not in allowed)
    if stray:
        out.append("These numbers are not in the resume, profile or posting: " + ", ".join(stray[:8]) + ".")
    source_words = content_words(source_text)
    unsupported = [c for c in draft.get("facts_used") or [] if not supported(str(c), source_words)]
    if unsupported:
        out.append("These facts could not be found in the resume or profile: " + "; ".join(unsupported[:5]) + ".")
    holder = PLACEHOLDER.search(text)
    if holder:
        out.append(f"It contains a placeholder: {holder.group(0)}.")
    words = len(text.split())
    if word_limit and words > word_limit:
        out.append(f"It has {words} words; the limit is {word_limit}.")
    if char_limit and len(text) > char_limit:
        out.append(f"It has {len(text)} characters; the limit is {char_limit}.")
    return out


def cover_letter_docx(path: Path, body: str, facts: dict, company: str, title: str) -> Path:
    from docx import Document
    from docx.shared import Pt

    name = facts.get("name", {}) or {}
    contact = facts.get("contact", {}) or {}
    full = name.get("full") or " ".join(p for p in (name.get("first"), name.get("last")) if p)
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    if full:
        heading = doc.add_paragraph()
        run = heading.add_run(full)
        run.bold = True
        run.font.size = Pt(14)
    line = "  |  ".join(p for p in (contact.get("email"), contact.get("phone"),
                                   ", ".join(x for x in (contact.get("city"), contact.get("state")) if x)) if p)
    if line:
        doc.add_paragraph(line)
    doc.add_paragraph(date.today().strftime("%B %d, %Y").replace(" 0", " "))
    doc.add_paragraph(f"Re: {title}, {company}" if title and company else "")
    doc.add_paragraph("Dear Hiring Team,")
    for paragraph in body.split("\n\n"):
        doc.add_paragraph(paragraph)
    doc.add_paragraph("Sincerely,")
    doc.add_paragraph(full)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


class Writer:
    """Called by the application agent's draft_long_answer tool."""

    def __init__(self, paths: Paths, ask: AskFn):
        self.paths = paths
        self.ask = ask

    def _material(self, leg, field: Field, guidance: str) -> tuple[dict, str, str]:
        ctx = leg.ctx
        resume_md = self.paths.profile / "resume.md"
        resume_text = resume_md.read_text(encoding="utf-8")[:20000] if resume_md.exists() else ""
        facts = ctx.facts.model_dump(mode="json")
        posting_file = self.paths.data / "postings" / f"{ctx.job.id}.md"
        posting = posting_file.read_text(encoding="utf-8")[:12000] if posting_file.exists() else ""
        if not posting:
            posting = f"{ctx.job.title} at {ctx.job.company}. {ctx.job.description_summary}"
        samples = ctx.answers.writing_samples(4)
        own = self.paths.profile / "writing_samples"
        if own.is_dir():
            for path in sorted(own.glob("*.txt")) + sorted(own.glob("*.md")):
                samples.append({"question": path.stem, "answer": path.read_text(encoding="utf-8")[:3000]})
        cover = field.kind == "file" or field.category == "cover_letter" or "cover letter" in field.label.lower()
        words, chars = limits(field, guidance)
        request = {
            "kind": "cover_letter" if cover else "long_answer",
            "question": "Cover letter" if field.kind == "file" else field.label,
            "section": field.section,
            "limit": {"words": words, "characters": chars},
            "job": {"company": ctx.job.company, "title": ctx.job.title, "posting": posting},
            "resume": resume_text,
            "profile": facts,
            "writing_samples": samples[:6],
            "guidance": guidance,
        }
        source_text = resume_text + "\n" + json.dumps(facts, ensure_ascii=False)
        other_text = posting + "\n" + field.label + "\n" + guidance + "\n" + ctx.job.title
        return request, source_text, other_text

    async def __call__(self, leg, field: Field, guidance: str) -> dict:
        request, source_text, other_text = self._material(leg, field, guidance)
        instructions = ((self.paths.prompts / "shared_rules.md").read_text(encoding="utf-8") + "\n\n"
                        + (self.paths.prompts / "writing_agent.md").read_text(encoding="utf-8"))
        words, chars = request["limit"]["words"], request["limit"]["characters"]
        feedback: list[str] = []
        draft: dict = {}
        for attempt in range(2):
            prompt = json.dumps(request, ensure_ascii=False, indent=1)
            if feedback:
                prompt += ("\n\nYour previous draft could not be used: " + " ".join(feedback)
                           + " Write it again within the rules, or return cannot_answer.")
            try:
                reply = await self.ask(name="writing_agent", instructions=instructions, prompt=prompt,
                                       schema=WRITE_SCHEMA)
            except AgentError as exc:
                return {"status": "failed", "reason": exc.message}
            cost = getattr(reply, "cost_usd", None) or 0.0
            leg.ctx.record.state.cost_usd += cost
            draft = reply.data if isinstance(reply.data, dict) else {}
            if draft.get("status") == "cannot_answer":
                return {"status": "cannot_answer",
                        "reason": " ".join(str(draft.get("reason") or "").split())[:300]
                        or "Your resume and profile do not answer this question."}
            draft["text"] = tidy(str(draft.get("text") or ""))
            feedback = problems(draft, source_text=source_text, other_text=other_text, word_limit=words,
                                char_limit=chars)
            if not feedback:
                break
            log.info("Draft for %r rejected (attempt %d): %s", field.label, attempt + 1, feedback)
        if feedback:
            return {"status": "rejected", "reason": "The drafts did not pass the fact check (" + " ".join(feedback)[:300]
                    + ") so none was used."}
        record = leg.ctx.record
        slug = SLUG.sub("_", (request["question"] or "answer").lower()).strip("_")[:50] or "answer"
        facts_used = [str(f) for f in draft.get("facts_used") or []][:12]
        notes = " ".join(str(draft.get("notes") or "").split())[:400]
        body = (f"# {request['question']}\n\n{draft['text']}\n\n---\nAI-drafted for {leg.ctx.job.company}, "
                f"{leg.ctx.job.title}. Facts used:\n" + "".join(f"- {f}\n" for f in facts_used)
                + (f"\nNotes: {notes}\n" if notes else ""))
        draft_file = record.save_draft(f"{slug}.md", body)
        result = {"status": "ok", "text": draft["text"], "draft_file": draft_file.name, "facts_used": facts_used,
                  "notes": notes}
        if field.kind == "file":
            path = cover_letter_docx(record.drafts / "cover_letter.docx", draft["text"], request["profile"],
                                     leg.ctx.job.company, leg.ctx.job.title)
            result["path"] = str(path)
        return result


def demo_draft(request: dict) -> dict:
    """The writing agent's stand-in for sample-data mode and tests: plain sentences from the profile."""
    profile = request.get("profile") or {}
    job = request.get("job") or {}
    experience = (profile.get("experience") or [{}])[0]
    education = (profile.get("education") or [{}])[0]
    highlight = (experience.get("highlights") or [""])[0]
    if not highlight:
        return {"status": "cannot_answer", "text": "", "facts_used": [], "notes": "",
                "reason": "The profile has no experience to draw on."}
    question = (request.get("question") or "").lower()
    if "lead" in question and "team" in question:
        return {"status": "cannot_answer", "text": "", "facts_used": [], "notes": "",
                "reason": "Nothing in your resume shows leading a team."}
    sentences = [
        f"I am studying {education.get('field', '')} at {education.get('school', '')}.",
        f"As a {experience.get('title', '')} at {experience.get('employer', '')}, I {highlight[0].lower()}{highlight[1:]}",
        f"I would bring the same habits to the {job.get('title', '')} role at {job.get('company', '')}.",
    ]
    text = " ".join(" ".join(s.split()) for s in sentences)
    limit = (request.get("limit") or {}).get("characters")
    if limit and len(text) > limit:
        text = sentences[1][:limit].rsplit(" ", 1)[0]
    return {"status": "ok", "text": text, "facts_used": [highlight], "notes": "Drafted by the sample writer.",
            "reason": ""}
