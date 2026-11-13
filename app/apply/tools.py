"""The application agent's tools (PLAN.md 8.3). The agent can do nothing except call these.

Every limit the plan promises is enforced here, in code, so it holds even if
the agent misreads its instructions or a web page tries to mislead it:

* nothing is ever submitted: there is no submit tool, and ``click`` refuses
  final-submit buttons and buttons whose purpose is unclear;
* passwords, payment details and ID numbers are never filled; agreements and
  signatures are left for you (the form engine refuses them);
* sensitive questions are only filled with your own saved answer to the
  question, and only with that exact answer;
* what you typed is never overwritten;
* long text is only written by the writing agent, and only when you allow it;
* files come only from your profile folder and this application's drafts;
* the agent cannot claim a page is finished while a required field is empty
  and not on the "needs you" list.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from ..core.models import Job, Settings, now_iso
from ..forms import FillError, fill, read_page
from ..forms import click as engine_click
from ..forms.certain import certain_values
from ..forms.fill import match_option, norm
from ..forms.model import Field, Reading, is_empty
from ..profile.models import Facts
from .answers import AnswerStore
from .questions import SENSITIVE_TOPICS, QuestionInfo, QuestionMatcher, matchable
from .record import ApplicationRecord, NeedItem

log = logging.getLogger("jobapp.apply")

OUTCOMES = ("needs_input", "page_done", "ready_for_review", "blocked", "failed")
PURPOSES = {"open_form": ("open_application",), "next_page": ("next",), "add_entry": ("add",),
            "expand": ("other",), "dismiss": ("other",)}
DISMISS = re.compile(r"\b(reject( all)?|decline|necessary only|essential only|only necessary|close|dismiss|no,? thanks|not now|got it)\b", re.I)
EXPAND = re.compile(r"\b(show more|see more|expand|more details|read more)\b", re.I)
FILL_SOURCES = ("profile", "resume", "saved_answer", "inferred")
LONG_TEXT = 300


@dataclass
class LegContext:
    job: Job
    page: Any                         # the Playwright page of this application's tab
    record: ApplicationRecord
    answers: AnswerStore
    settings: Settings
    facts: Facts
    resume: Optional[Path]
    profile_folder: Path
    reason: str                       # prepare, continue_after_input, continue_after_page_review, continue_in_review
    site_notes: Path
    writer: Optional[Callable[..., Awaitable[dict]]] = None
    matcher: Optional[QuestionMatcher] = None     # recognises questions by meaning, when switched on
    on_event: Callable[[str], None] = lambda text: None
    extra: dict = field(default_factory=dict)


def _clip_options(options: list[str], limit: int = 60) -> list[str]:
    if len(options) <= limit:
        return options
    return options[:limit] + [f"... and {len(options) - limit} more"]


DEGREE_WORDS = (("doctor", ("doctor", "ph.d", "phd")), ("master", ("master",)), ("bachelor", ("bachelor",)),
                ("associate", ("associate",)))


def degree_option(degree: str, options: list[str]) -> Optional[str]:
    """The option for the same level of degree ("Master of Science" -> "Master's Degree"), if exactly one fits."""
    lowered = degree.lower()
    for _name, words in DEGREE_WORDS:
        if any(w in lowered for w in words):
            fits = [o for o in options if any(w in o.lower() for w in words)]
            return fits[0] if len(fits) == 1 else None
    return None


class Leg:
    """One stretch of the agent's work, from Prepare or Continue to the next pause."""

    def __init__(self, ctx: LegContext):
        self.ctx = ctx
        self.outcome: Optional[str] = None
        self.summary = ""
        self.reading: Optional[Reading] = None
        self.next_clicks = 0
        self.calls: list[str] = []
        self._suggested: dict[str, dict] = {}
        self._questions: dict[str, QuestionInfo] = {}
        for key, saved in (ctx.record.state.questions or {}).items():
            try:
                self._questions[key] = QuestionInfo(**saved)
            except TypeError:
                continue

    # --- helpers ---------------------------------------------------------------------------

    def _event(self, text: str) -> None:
        self.ctx.record.log(text)
        self.ctx.on_event(text)

    async def _read(self) -> Reading:
        self.reading = await read_page(self.ctx.page)
        self._overlay(self.reading)
        return self.reading

    def _overlay(self, reading: Reading) -> None:
        """A question the matcher read as sensitive is treated as sensitive, however it is worded."""
        for f in reading.fields:
            info = self._questions.get(f.key)
            if info is not None and info.sensitive and not f.rule and not f.category.startswith("sensitive:"):
                f.category = "sensitive:" + (info.topic if info.topic in SENSITIVE_TOPICS else "other")

    async def _match_questions(self, reading: Reading) -> None:
        """Ask the question matcher about the questions on this page it has not read yet."""
        if self.ctx.matcher is None:
            return
        todo = [f for f in reading.fields if matchable(f) and f.key not in self._questions]
        if not todo:
            return
        infos, cost = await self.ctx.matcher.read(self.ctx.job, reading, todo, self.ctx.answers)
        record = self.ctx.record
        record.state.cost_usd = round(record.state.cost_usd + cost, 4)
        for f in todo:
            info = infos.get(f.key) or QuestionInfo(key=f.key, label=f.label)
            self._questions[f.key] = info
            record.state.questions[f.key] = info.as_dict()
        matched = sum(1 for f in todo if self._questions[f.key].relation != "none")
        sensitive = sum(1 for f in todo if self._questions[f.key].sensitive)
        record.save()
        if infos:
            self._event(f"Question matcher read {len(todo)} question(s): {matched} matched to your saved answers, "
                        f"{sensitive} treated as sensitive.")
        self._overlay(reading)

    def _certain(self, reading: Reading) -> dict[str, Any]:
        planned, _kept = certain_values(reading, self.ctx.facts, self.ctx.resume)
        return {p.key: p.value for p in planned if p.source != "resume"}

    def _suggestion(self, f: Field, certain: dict[str, Any]) -> Optional[dict]:
        record = self.ctx.record.field(f.key)
        if record is not None and record.user_changed and not is_empty(record.value):
            return {"value": record.value, "source": "user", "note": "You entered this earlier in this application."}
        if f.key in certain:
            return {"value": certain[f.key], "source": "profile"}
        candidates = self._candidates(f)
        info = self._questions.get(f.key)
        if info is not None and info.relation in ("same", "derived") and info.answer_id and not any(
                c.match == "exact" for c in candidates):
            suggestion = {"value": info.value, "source": "saved_answer", "answer_id": info.answer_id,
                          "match": "same_meaning" if info.relation == "same" else "derived",
                          "saved_question": info.saved_question, "why": info.why}
            if f.category.startswith("sensitive:"):
                suggestion["sensitive"] = True
            return suggestion
        if not candidates:
            return self._education_suggestion(f)
        best = candidates[0]
        saved = best.answer.answer
        value = saved
        note = ""
        if f.options and f.kind in ("select", "radio", "choice", "combobox") and isinstance(saved, str):
            value = match_option(saved, f.options)
            if value is None:
                note = f"Your saved answer was '{saved}', which matches none of these choices."
        suggestion = {"value": value, "source": "saved_answer", "answer_id": best.answer.id, "match": best.match}
        if best.match == "similar":
            suggestion["saved_question"] = best.answer.question
        if best.answer.sensitivity == "sensitive":
            suggestion["sensitive"] = True
        if note:
            suggestion["note"] = note
        return suggestion

    def _candidates(self, f: Field) -> list:
        """Saved answers that may be used for this field. A sensitive question, or a saved answer you marked
        sensitive, only ever matches the same question: "similar" wording is not enough."""
        found = self.ctx.answers.candidates(f.label, f.kind, company=self.ctx.job.company)
        sensitive_field = f.category.startswith("sensitive:")
        return [c for c in found if c.match == "exact" or not (sensitive_field or c.answer.sensitivity == "sensitive")]

    def _education_suggestion(self, f: Field) -> Optional[dict]:
        """School, degree and discipline from the first (most recent) education entry in the resume."""
        if f.category not in ("school", "degree", "discipline") or not self.ctx.facts.education:
            return None
        entry = self.ctx.facts.education[0]
        wanted = {"school": entry.school, "degree": entry.degree, "discipline": entry.field}[f.category]
        if not wanted:
            return None
        value: Optional[str] = wanted
        if f.options:
            if f.category == "degree":
                value = degree_option(wanted, f.options)
            else:
                value = next((o for o in f.options if norm(o) == norm(wanted)), None)
            if value is None:
                return None
        return {"value": value, "source": "resume", "note": "From the most recent education entry in the resume."}

    def _field_view(self, f: Field, certain: dict[str, Any]) -> dict:
        view: dict[str, Any] = {"id": f.key, "label": f.label or "(no label)", "kind": f.kind}
        if f.required:
            view["required"] = True
        if f.section:
            view["section"] = f.section
        if f.options:
            view["options"] = _clip_options(f.options)
        elif f.kind == "combobox":
            view["options"] = "not listed until opened; give the value you want and the tool picks the matching choice"
        info = self._questions.get(f.key)
        if info is not None and info.asks and norm(info.asks) != norm(f.label):
            view["asks"] = info.asks
        if not f.empty:
            view["value"] = f.value
            record = self.ctx.record.field(f.key)
            if record is not None and record.source:
                view["value_source"] = record.source
        if f.error:
            view["error"] = f.error
        if f.maxlength:
            view["max_length"] = f.maxlength
        if f.rule == "never":
            view["rule"] = "never filled: " + f.rule_reason
        elif f.rule == "user":
            view["rule"] = "left for the user: " + f.rule_reason
        elif f.category.startswith("sensitive:"):
            view["sensitive"] = f.category.split(":", 1)[1]
        elif f.category != "other":
            view["category"] = f.category
        if not f.visible and f.kind != "file":
            view["hidden"] = True
        suggestion = self._suggestion(f, certain)
        if suggestion is not None:
            view["suggested"] = suggestion
            self._suggested[f.key] = suggestion
        return view

    def _may_click(self, role: str, text: str) -> bool:
        if role in ("submit", "back", "upload"):
            return False
        if role == "next":
            return self._next_allowed() is None
        if role == "open_application":
            return not self.ctx.settings.never_click_next and not self._password_page()
        if role == "add":
            return True
        return bool(DISMISS.search(text) or EXPAND.search(text))

    PASSWORD_PAGE = ("This page has a password field: it is a sign-in or account page. Job App does not fill it "
                     "or click past it. Call need_user asking the user to sign in or create the account, "
                     "then end the leg with needs_input.")

    def _password_page(self, reading: Optional[Reading] = None) -> bool:
        reading = reading or self.reading
        return bool(reading and any(f.visible and f.kind == "password" for f in reading.fields))

    def _next_allowed(self) -> Optional[str]:
        """None if the agent may click Next now, else why not."""
        if self._password_page():
            return self.PASSWORD_PAGE
        if self.ctx.settings.never_click_next:
            return ("Your settings say Job App never clicks Next, Continue, Review or Apply. Finish this page and "
                    "end the leg with page_done; the user moves on to the next page.")
        page = self.ctx.record.state.page
        blocking = [n.label for n in self.ctx.record.required_needs() if n.page == page]
        if blocking:
            return "Fields on this page still need the user: " + "; ".join(blocking[:6]) + ". End the leg with needs_input."
        if self.ctx.settings.pause_after_each_page:
            if self.ctx.reason == "continue_after_page_review" and self.next_clicks == 0:
                return None
            return "'Pause after each page' is on: finish this page and end the leg with page_done."
        return None

    # --- the tools ------------------------------------------------------------------------------

    async def get_page(self) -> dict:
        self.calls.append("get_page")
        reading = await self._read()
        await self._match_questions(reading)
        certain = self._certain(reading)
        self._suggested = {}
        notices = []
        if reading.captcha:
            notices.append("A CAPTCHA is on this page. Only the user can solve it.")
        visible = [f for f in reading.fields if f.visible]
        if any(f.kind == "password" for f in visible):
            notices.append("This page asks for a password: it is a sign-in or account page. Only the user can do this.")
        if not visible and any(b.role == "open_application" for b in reading.buttons):
            notices.append("No form fields yet: this looks like the job description. Open the form with click, purpose open_form.")
        needs = [n.label for n in self.ctx.record.state.needs]
        return {
            "url": reading.url, "title": reading.title, "step": reading.progress or None,
            "page_number": self.ctx.record.state.page, "notices": notices,
            "fields": [self._field_view(f, certain) for f in reading.fields[:150]],
            "buttons": [{"id": b.key, "text": b.text, "kind": b.role,
                         "you_may_click": self._may_click(b.role, b.text) and not b.disabled}
                        for b in reading.buttons[:40]],
            "needs_you": needs,
        }

    async def fill_fields(self, items: list[dict]) -> dict:
        self.calls.append("fill_fields")
        reading = await self._read()
        if self._password_page(reading):
            self._event("Did not fill anything: the page asks for a password.")
            return {"ok": False, "message": self.PASSWORD_PAGE}
        await self._match_questions(reading)
        refused, to_fill, meta = [], {}, {}
        for item in items if isinstance(items, list) else []:
            key = str(item.get("id") or "")
            value = item.get("value")
            source = str(item.get("source") or "")
            reason = " ".join(str(item.get("reason") or "").split())[:300]
            f = reading.field(key)
            if f is None:
                refused.append({"id": key, "status": "refused", "message": "There is no field with that id on the page now."})
                continue
            problem = self._fill_problem(f, value, source, item)
            if problem:
                refused.append({"id": key, "label": f.label, "status": "refused", "message": problem})
                if f.rule:
                    self.ctx.record.add_need(NeedItem(label=f.label, key=f.key, reason=f.rule_reason,
                                                      page=self.ctx.record.state.page,
                                                      required=f.required and f.rule == "never"))
                continue
            to_fill[key] = value
            meta[key] = (f, source, reason, str(item.get("answer_id") or ""))
        results = await fill(self.ctx.page, to_fill, allowed_files=[self.ctx.profile_folder, self.ctx.record.drafts]) if to_fill else []
        out = list(refused)
        for result in results:
            f, source, reason, answer_id = meta[result.key]
            ok = result.status == "ok"
            match = self._suggested.get(f.key, {}).get("match", "") if source == "saved_answer" else ""
            self.ctx.record.upsert(f.key, label=f.label, kind=f.kind, category=f.category, options=f.options,
                                   required=f.required, value=result.now if ok else result.wanted, source=source,
                                   answer_id=answer_id, match=match, filled_by="agent", verified=ok, reason=reason,
                                   page=self.ctx.record.state.page)
            if ok and source == "saved_answer" and answer_id:
                self.ctx.answers.used(answer_id)
            if ok and match in ("same_meaning", "derived"):
                saved_question = self._suggested.get(f.key, {}).get("saved_question", "")
                label = f.label.rstrip(" :?*")
                note = (f"Answered \"{label}\" with your saved answer to \"{saved_question.rstrip(' ?')}\""
                        + (", worked out from it" if match == "derived" else "") + ". Check it.")
                if note not in self.ctx.record.state.review_notes:
                    self.ctx.record.state.review_notes.append(note)
            if ok:
                self.ctx.record.drop_need(key=f.key)
            out.append({"id": result.key, "label": result.label, "status": result.status, "now": result.now,
                        "message": result.message})
        self.ctx.record.save()
        filled = sum(1 for r in results if r.status == "ok")
        self._event(f"Agent filled {filled} field(s); {len(out) - filled} not filled.")
        return {"results": out}

    def _fill_problem(self, f: Field, value: Any, source: str, item: dict) -> str:
        if f.rule:
            return f"{f.rule_reason} Call need_user for it."
        if f.kind == "file":
            return "Use upload_document for file fields."
        record = self.ctx.record.field(f.key)
        if source == "user":
            if record is not None and record.user_changed and norm(record.value) == norm(value):
                return ""
            return "source user is only for putting back a value the user entered in this application."
        if source not in FILL_SOURCES:
            return f"source must be one of {', '.join(FILL_SOURCES)}. Long text goes through draft_long_answer."
        if record is not None and record.user_changed and not is_empty(f.value) and norm(f.value) != norm(value):
            return "The user entered this value. Leave it as it is."
        if f.kind == "textarea" and isinstance(value, str) and len(value) > LONG_TEXT and source != "saved_answer":
            return "Long written answers come from draft_long_answer, not from you."
        suggestion = self._suggested.get(f.key) or {}
        info = self._questions.get(f.key)
        if source == "saved_answer" and info is not None and info.answer_id and info.relation in ("same", "derived") \
                and str(item.get("answer_id") or "") == info.answer_id:
            if value != info.value and norm(value) != norm(info.value):
                return f"Use the suggested value exactly: {info.value!r}."
            return ""
        if source == "saved_answer":
            answer_id = str(item.get("answer_id") or "")
            ids = {c.answer.id: c for c in self._candidates(f)}
            if answer_id not in ids:
                return "answer_id must be one of the saved answers offered for this field in get_page."
            saved = ids[answer_id].answer.answer
            allowed = {norm(saved)}
            if isinstance(saved, str) and f.options:
                mapped = match_option(saved, f.options)
                if mapped:
                    allowed.add(norm(mapped))
            if norm(value) not in allowed and value != saved:
                return "A saved answer must be used exactly as the user gave it."
        if f.category.startswith("sensitive:") and source != "saved_answer":
            return ("This is a sensitive question. It is only ever answered with the user's own saved answer. "
                    "If there is none, call need_user (required) or leave it blank (optional).")
        if source == "profile" and suggestion.get("source") == "profile" and norm(suggestion.get("value")) != norm(value):
            pass  # a profile fact formatted for the field is fine
        return ""

    async def upload_document(self, id: str, document: str) -> dict:  # noqa: A002 - the tool's own parameter name
        self.calls.append("upload_document")
        reading = await self._read()
        if self._password_page(reading):
            return {"ok": False, "message": self.PASSWORD_PAGE}
        f = reading.field(id)
        if f is None or f.kind != "file":
            return {"ok": False, "message": "There is no file field with that id on the page now."}
        if document == "resume":
            path = self.ctx.resume
            if path is None:
                return {"ok": False, "message": "There is no resume in the profile folder."}
            source = "resume"
        elif document == "cover_letter":
            drafts = sorted(self.ctx.record.drafts.glob("cover_letter*.docx")) if self.ctx.record.drafts.exists() else []
            if not drafts:
                return {"ok": False, "message": "No cover letter has been drafted. Use draft_long_answer on this field."}
            path, source = drafts[-1], "ai_draft"
        else:
            return {"ok": False, "message": "document must be resume or cover_letter."}
        results = await fill(self.ctx.page, {id: str(path)}, allowed_files=[self.ctx.profile_folder, self.ctx.record.drafts])
        result = results[0]
        ok = result.status == "ok"
        self.ctx.record.upsert(id, label=f.label, kind="file", category=f.category, value=path.name, source=source,
                               filled_by="agent", verified=ok, page=self.ctx.record.state.page)
        self.ctx.record.save()
        self._event(f"Attached {path.name} to '{f.label}'." if ok else f"Could not attach {path.name}: {result.message}")
        return {"ok": ok, "message": result.message or f"Attached {path.name}."}

    async def draft_long_answer(self, id: str, guidance: str = "") -> dict:  # noqa: A002
        self.calls.append("draft_long_answer")
        if not self.ctx.settings.write_long_answers:
            return {"ok": False, "message": "Writing long answers is switched off in Settings. Call need_user for this field."}
        if self.ctx.writer is None:
            return {"ok": False, "message": "The writing agent is not available. Call need_user for this field."}
        reading = await self._read()
        f = reading.field(id)
        if f is None:
            return {"ok": False, "message": "There is no field with that id on the page now."}
        if f.rule or f.category.startswith("sensitive:"):
            return {"ok": False, "message": "This field is not one the writing agent answers. Call need_user."}
        record = self.ctx.record.field(f.key)
        if record is not None and record.user_changed and not is_empty(f.value):
            return {"ok": False, "message": "The user wrote this answer. Leave it as it is."}
        if f.kind not in ("textarea", "text", "file"):
            return {"ok": False, "message": "Only text boxes and cover letter uploads can be drafted."}
        result = await self.ctx.writer(self, f, " ".join(str(guidance or "").split())[:600])
        if result.get("status") != "ok":
            reason = result.get("reason") or "The writing agent could not answer it truthfully from your material."
            self.ctx.record.add_need(NeedItem(label=f.label, key=f.key, reason=reason, page=self.ctx.record.state.page,
                                              required=f.required))
            self.ctx.record.save()
            return {"ok": False, "message": f"Not drafted: {reason} It is on the needs-you list."}
        if f.kind == "file":
            path = Path(result["path"])
            fills = await fill(self.ctx.page, {id: str(path)}, allowed_files=[self.ctx.record.drafts])
            value = path.name
        else:
            text = result["text"]
            fills = await fill(self.ctx.page, {id: text})
            value = text
        ok = fills[0].status == "ok"
        self.ctx.record.upsert(f.key, label=f.label, kind=f.kind, category=f.category, required=f.required,
                               value=value, source="ai_draft", filled_by="agent", verified=ok,
                               page=self.ctx.record.state.page, draft=result.get("draft_file", ""),
                               facts_used=result.get("facts_used", []), notes=result.get("notes", ""))
        label = f.label.rstrip(" :")
        note = f"AI-drafted: {label}{'' if label[-1:] in '?.!' else '.'} Read it before you submit."
        if note not in self.ctx.record.state.review_notes:
            self.ctx.record.state.review_notes.append(note)
        self.ctx.record.drop_need(key=f.key)
        self.ctx.record.save()
        self._event(f"Drafted '{f.label}'" + ("" if ok else f" but it did not take: {fills[0].message}"))
        return {"ok": ok, "message": fills[0].message or "Drafted and filled in. It is marked AI-drafted for the user."}

    async def click(self, id: str, purpose: str) -> dict:  # noqa: A002
        self.calls.append("click")
        if purpose not in PURPOSES:
            return {"ok": False, "message": f"purpose must be one of {', '.join(PURPOSES)}."}
        reading = await self._read()
        button = reading.button(id)
        if button is None:
            return {"ok": False, "message": "There is no button with that id on the page now."}
        if button.role == "submit":
            self._event(f"Refused to click '{button.text}': it may submit the application.")
            return {"ok": False, "message": "This button may submit the application. Job App never submits. "
                                          "If the form is complete, end the leg with ready_for_review."}
        if purpose not in ("dismiss", "expand") and self._password_page(reading):
            self._event(f"Refused to click '{button.text}': the page asks for a password.")
            return {"ok": False, "message": self.PASSWORD_PAGE}
        if button.role not in PURPOSES[purpose]:
            return {"ok": False, "message": f"'{button.text}' is not a button for {purpose}. If you cannot tell what "
                                          "it does, ask the user with need_user."}
        if purpose == "dismiss" and not DISMISS.search(button.text):
            return {"ok": False, "message": "Only buttons that decline or close a banner can be dismissed."}
        if purpose == "expand" and not EXPAND.search(button.text):
            return {"ok": False, "message": "Only buttons that show more of the page can be expanded."}
        if purpose == "next_page":
            why_not = self._next_allowed()
            if why_not:
                return {"ok": False, "message": why_not}
        if purpose == "open_form" and self.ctx.settings.never_click_next:
            return {"ok": False, "message": "Your settings say Job App never clicks Apply or Next. Call need_user "
                                          "asking the user to open the application form, then end with needs_input."}
        allow = ("next", "add", "open_application", "other")
        try:
            await engine_click(self.ctx.page, id, allow=allow)
        except FillError as exc:
            return {"ok": False, "message": str(exc)}
        try:
            await self.ctx.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001 - busy pages never go idle
            pass
        if purpose == "next_page":
            self.next_clicks += 1
            state = self.ctx.record.state
            if state.page not in state.pages_done:
                state.pages_done.append(state.page)
            state.page += 1
            self.ctx.record.save()
        self._event(f"Clicked '{button.text}' ({purpose.replace('_', ' ')}).")
        return {"ok": True, "message": f"Clicked '{button.text}'. Call get_page to see the result."}

    async def need_user(self, label: str, reason: str, required: bool = True, id: str = "") -> dict:  # noqa: A002
        self.calls.append("need_user")
        label = " ".join(str(label or "").split())[:200]
        if not label:
            return {"ok": False, "message": "Give the label of the field or a short name for the task."}
        reading = self.reading or await self._read()
        f = reading.field(id) if id else None
        self.ctx.record.add_need(NeedItem(label=f.label if f else label, key=id if f else "",
                                          reason=" ".join(str(reason or "").split())[:300],
                                          page=self.ctx.record.state.page, required=bool(required),
                                          kind="field" if f else "task"))
        self.ctx.record.save()
        return {"ok": True}

    def _never_selector(self) -> str:
        keys = [f.key for f in (self.reading.fields if self.reading else []) if f.rule == "never"]
        return ", ".join(["input[type=password]", "input[autocomplete^=cc-]"]
                         + [f'[data-jobapp-key="{k}"]' for k in keys])

    async def screenshot(self) -> bytes:
        """The visible page. Password, payment and ID fields are blacked out, whatever is in them."""
        self.calls.append("screenshot")
        await self._read()
        mask = [self.ctx.page.locator(self._never_selector())]
        return await self.ctx.page.screenshot(type="jpeg", quality=55, full_page=False, mask=mask)

    async def inspect_element(self, id: str) -> dict:  # noqa: A002
        self.calls.append("inspect_element")
        await self._read()
        locator = self.ctx.page.locator(f'[data-jobapp-key="{id}"], [data-jobapp-button="{id}"]').first
        try:
            # A copy of the element, with any value in a password, payment or ID field blanked.
            html = await locator.evaluate(
                "(e, never) => { const p = e.closest('.field, li, fieldset, [data-automation-id^=formField]') || e.parentElement;"
                " const copy = (p || e).cloneNode(true);"
                " const blank = (x) => { x.removeAttribute('value'); x.textContent = x.tagName === 'INPUT' ? '' : x.textContent; };"
                " if (copy.matches && copy.matches(never)) blank(copy);"
                " copy.querySelectorAll(never).forEach(blank);"
                " return copy.outerHTML; }", self._never_selector(), timeout=4000)
        except Exception:  # noqa: BLE001
            return {"ok": False, "message": "That element is not on the page now (it may be inside a frame)."}
        html = re.sub(r"\s+", " ", html)
        return {"ok": True, "html": html[:3000] + (" [...]" if len(html) > 3000 else "")}

    async def save_site_note(self, note: str) -> dict:
        self.calls.append("save_site_note")
        text = " ".join(str(note or "").split())[:400]
        if not text:
            return {"ok": False}
        host = (urlsplit(self.ctx.page.url).hostname or "site").lower()
        path = self.ctx.site_notes / f"{re.sub(r'[^a-z0-9.-]', '_', host)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else [f"# Notes about {host}"]
        if f"- {text}" not in lines:
            lines.append(f"- {text}")
        path.write_text("\n".join(lines[:1] + lines[1:][-40:]) + "\n", encoding="utf-8")
        return {"ok": True}

    async def end_leg(self, outcome: str, summary: str) -> dict:
        self.calls.append("end_leg")
        if outcome not in OUTCOMES:
            return {"ok": False, "message": f"outcome must be one of {', '.join(OUTCOMES)}."}
        reading = await self._read()
        record = self.ctx.record
        needs_keys = {n.key for n in record.state.needs if n.key}
        for f in reading.fields:
            if f.rule == "user" and f.visible:
                note = f"For you to read and tick or sign: {f.label}"
                if note not in record.state.review_notes:
                    record.state.review_notes.append(note)
        if outcome in ("page_done", "ready_for_review"):
            missing = [f.label or "(no label)" for f in reading.fields
                       if f.required and f.visible and f.empty and f.rule != "user" and f.key not in needs_keys]
            if missing:
                return {"ok": False, "message": "These required fields are empty and not on the needs-you list: "
                        + "; ".join(missing[:10]) + ". Fill them or call need_user for each."}
            has_next = any(b.role == "next" and not b.disabled for b in reading.buttons)
            if record.required_needs() and outcome == "ready_for_review":
                return {"ok": False, "message": "Required items are on the needs-you list. End with needs_input."}
            if outcome == "ready_for_review" and has_next:
                return {"ok": False, "message": "There is a Next button, so this is not the last page."}
            if outcome == "page_done":
                stops = self.ctx.settings.pause_after_each_page or self.ctx.settings.never_click_next
                if not stops:
                    return {"ok": False, "message": "'Pause after each page' is off: click Next, or end with "
                                                    "ready_for_review on the last page."}
                if not has_next:
                    outcome = "ready_for_review"
        if outcome == "needs_input" and not record.required_needs():
            return {"ok": False, "message": "Nothing required is on the needs-you list. Call need_user first, or use "
                                            "another outcome."}
        self.outcome = outcome
        self.summary = " ".join(str(summary or "").split())[:900]
        record.state.summary = self.summary
        record.save()
        self._event(f"Leg ended: {outcome}. {self.summary}")
        return {"ok": True}


# --- the run brief (AGENT_BRIEFS_DRAFT.md section 7) ---------------------------------------------


def compact_facts(facts: Facts) -> dict:
    data = facts.model_dump(mode="json")
    return {
        "name": data.get("name"), "contact": data.get("contact"), "links": data.get("links"),
        "education": data.get("education"),
        "experience": data.get("experience"),
        "projects": (data.get("projects") or [])[:8],
        "skills": (data.get("skills") or [])[:60],
        "certifications": data.get("certifications") or [],
    }


def build_brief(ctx: LegContext, user_changes: list[dict]) -> dict:
    record = ctx.record
    site = (urlsplit(ctx.page.url).hostname or "").lower()
    notes_path = ctx.site_notes / f"{re.sub(r'[^a-z0-9.-]', '_', site)}.md"
    site_notes = notes_path.read_text(encoding="utf-8")[-2000:] if notes_path.exists() else ""
    return {
        "job": {"id": ctx.job.id, "company": ctx.job.company, "title": ctx.job.title, "url": ctx.job.url,
                "found_via": ctx.job.source.found_via},
        "settings": {"pause_after_each_page": ctx.settings.pause_after_each_page,
                     "never_click_next": ctx.settings.never_click_next,
                     "write_long_answers": ctx.settings.write_long_answers},
        "leg": {"reason": ctx.reason, "number": record.state.legs},
        "record": {
            "pages_done": record.state.pages_done, "current_page": record.state.page,
            "fields_filled": [
                {"label": f.label, "value": "(answered)" if f.category.startswith("sensitive:") else f.value,
                 "source": f.source}
                for f in record.fields if not is_empty(f.value)][-60:],
        },
        "user_changes": user_changes,
        "needs_user": [{"label": n.label, "reason": n.reason, "required": n.required} for n in record.state.needs],
        "profile": compact_facts(ctx.facts),
        "site_notes": site_notes,
        "today": now_iso()[:10],
    }


def as_tool_text(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def screenshot_content(data: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/jpeg"}
