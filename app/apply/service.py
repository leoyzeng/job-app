"""Prepare, Continue and Mark as applied (PLAN.md 8.1 and 8.2).

Prepare opens the application in Job App's browser window, does the certain
parts with plain code (open the form, attach the resume, fill name, email,
phone and links), then hands the page to the application agent for one leg.
When the leg ends the listing says what happens next: "Waiting for input",
"Waiting for page review" or "Waiting for review". Nothing is submitted.

Continue first compares the form with how the agent left it. Every field you
changed is saved to your answers (so it is offered next time) and to this
application's record. Then the agent carries on, never overwriting what you
typed. Mark as applied saves your last changes the same way.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core.events import EventBus
from ..core.lifecycle import NotAllowed
from ..core.models import Job, Need, Status, StatusDetail, now_iso
from ..core.settings import load_settings
from ..core.store import TrackerStore
from ..forms import blockers, changes, click, fill, read_page
from ..forms.certain import certain_values
from ..forms.model import Reading, is_empty
from ..forms.watch import FormWatcher
from ..paths import Paths
from ..profile.resume import current_resume
from ..search.text import clip, looks_closed
from .agent import RunLeg
from .answers import AnswerStore
from .questions import SENSITIVE_TOPICS
from .record import ApplicationRecord
from .tools import Leg, LegContext, build_brief

log = logging.getLogger("jobapp.apply")

RESTARTED = ("Job App was closed while this application was being filled. Click Prepare to carry on; "
             "what was filled is kept.")
THANKS = re.compile(
    r"thank(s| you) for (applying|your application|submitting)|application (has been |was )?(successfully )?"
    r"(submitted|received|sent)|we('ve| have) received your application|you('ve| have) (successfully )?applied"
    r"|successfully (submitted|applied)", re.I)
EVENTS = {"needs_input": "needs_input", "blocked": "needs_input", "page_done": "page_done",
          "ready_for_review": "ready_for_review", "failed": "failed", "posting_closed": "posting_closed"}
DEFAULT_MESSAGES = {
    "needs_input": "Some fields need you. Fill them in Job App's browser window, then click Continue.",
    "page_done": "This page is done. Check it, then click Continue for the next page.",
    "ready_for_review": "The form is filled in. Review it in Job App's browser window and submit it yourself.",
}
REASONS = {Status.WAITING_FOR_INPUT: "continue_after_input",
           Status.WAITING_FOR_PAGE_REVIEW: "continue_after_page_review",
           Status.WAITING_FOR_REVIEW: "continue_in_review"}


class ApplicationRefused(Exception):
    """Prepare or Continue cannot start; the message is for the user."""


class Stop(Exception):
    def __init__(self, event: str, message: str):
        self.event = event
        self.message = message
        super().__init__(message)


@dataclass
class Session:
    job_id: str
    page: Any
    record: ApplicationRecord
    snapshot: Optional[Reading] = None      # the form as the agent left it
    last_same: Optional[Reading] = None     # the watcher's latest reading of that same page
    watcher: Optional[FormWatcher] = None
    saved_values: dict = field(default_factory=dict)


def overlaps(before: Optional[Reading], after: Optional[Reading]) -> bool:
    """True when the two readings are of the same page of the form."""
    if before is None or after is None:
        return False
    keys = {f.key for f in before.fields}
    if not keys:
        return False
    return len(keys & {f.key for f in after.fields}) >= max(1, len(keys) // 2)


class ApplicationService:
    def __init__(self, paths: Paths, store: TrackerStore, bus: EventBus, browsers, profile, run_leg: RunLeg,
                 writer=None, usage=None, matcher=None):
        self.paths = paths
        self.store = store
        self.bus = bus
        self.browsers = browsers
        self.profile = profile
        self.run_leg = run_leg
        self.writer = writer
        self.usage = usage                  # checks the daily usage cap, if given
        self.matcher = matcher              # recognises questions by meaning (Settings), if given
        self.answers = AnswerStore(paths.answers)
        self.sessions: dict[str, Session] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.queue: list[str] = []
        self.prefill_wait = 2.5             # seconds sites get to fill fields from the resume
        self.watch_interval = 4.0
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    # --- bookkeeping ---------------------------------------------------------------------------

    def startup(self) -> None:
        """After a restart nothing is being filled: say so on the listings that were."""
        for job in self.store.all_jobs():
            if job.status in (Status.QUEUED, Status.FILLING):
                try:
                    self.store.move(job.id, "failed", by="app", detail=StatusDetail(message=RESTARTED))
                except (KeyError, NotAllowed):
                    pass

    def _changed(self, job_id: str) -> None:
        self.bus.publish("jobs_changed", job_id=job_id)

    def busy(self, job_id: str) -> bool:
        return job_id in self.tasks or job_id in self.queue

    def _window(self):
        window = getattr(self.browsers, "window", None)
        if window is None:
            raise ApplicationRefused("Autofill needs Job App's browser window, which is not available here.")
        return window

    def _check_ready(self) -> None:
        profile = self.profile.load_profile()
        if profile is None or not profile.confirmed:
            raise ApplicationRefused("Confirm your profile on the Profile screen first. Autofill fills forms from it.")
        if self.usage is not None:
            problem = self.usage.problem()
            if problem:
                raise ApplicationRefused(problem)
        self._window()

    def _pump(self) -> None:
        limit = load_settings(self.paths.settings).max_parallel_applications
        running = sum(1 for job_id in self.tasks)
        while self.queue and running < limit:
            job_id = self.queue.pop(0)
            self._launch(job_id, "prepare")
            running += 1
        for position, job_id in enumerate(self.queue, start=1):
            try:
                job = self.store.get(job_id)
            except KeyError:
                continue
            message = f"Waiting for {'another application' if position == 1 else f'{position} other applications'} to finish."
            if job.status_detail.message != message:
                job.status_detail = StatusDetail(message=message)
                self.store.update(job)
                self._changed(job_id)

    def _launch(self, job_id: str, reason: str) -> None:
        self.loop = asyncio.get_running_loop()
        task = asyncio.create_task(self._run(job_id, reason))
        self.tasks[job_id] = task

        def done(_task: asyncio.Task) -> None:
            if self.tasks.get(job_id) is _task:
                del self.tasks[job_id]
            self._pump()

        task.add_done_callback(done)

    # --- your buttons ---------------------------------------------------------------------------

    def prepare(self, job_id: str) -> Job:
        """Start (or restart, after an error) an application. Raises ApplicationRefused, KeyError, NotAllowed."""
        self._check_ready()
        job = self.store.move(job_id, "prepare", by="user", detail=StatusDetail(message="Starting."))
        self.queue.append(job_id)
        self._pump()
        return self.store.get(job_id)

    def continue_(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        reason = REASONS.get(job.status)
        if reason is None:
            raise NotAllowed("Continue resumes a paused application. This one is not paused.")
        if job_id in self.tasks:
            raise ApplicationRefused("Job App is still working on this application.")
        if reason != "continue_in_review":
            self._check_ready()
        if reason == "continue_after_page_review" and load_settings(self.paths.settings).never_click_next:
            reason = "continue_after_input"      # you moved on yourself; the agent works on the page you are on
        job = self.store.move(job_id, "continue", by="user",
                              detail=StatusDetail(message="Saving your changes and carrying on."))
        self._launch(job_id, reason)
        return job

    async def _cancel(self, job_id: str) -> None:
        if job_id in self.queue:
            self.queue.remove(job_id)
        task = self.tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def mark_applied(self, job_id: str) -> Job:
        self.store.get(job_id)
        await self._cancel(job_id)
        learned: list[dict] = []
        session = self.sessions.get(job_id)
        folder = self.paths.applications / job_id
        try:
            if session is not None:
                learned = await self._learn(self.store.get(job_id), session)
                self._add_wordings(session.record)
            elif (folder / "snapshot.json").exists():
                record = ApplicationRecord(folder)
                learned = await self._learn(self.store.get(job_id), Session(job_id, None, record), reopened=True)
                self._add_wordings(record)
        except Exception:  # noqa: BLE001 - saving answers must not stop you marking it applied
            log.exception("Could not save the last changes of %s", job_id)
        await self._end_session(job_id, close_tab=False)
        message = f"Saved {len(learned)} of your answers from this application." if learned else ""
        job = self.store.move(job_id, "mark_applied", by="user", detail=StatusDetail(message=message))
        return job

    async def skip(self, job_id: str) -> Job:
        self.store.get(job_id)
        await self._cancel(job_id)
        await self._end_session(job_id, close_tab=True)
        return self.store.move(job_id, "skip", by="user")

    async def _end_session(self, job_id: str, *, close_tab: bool) -> None:
        session = self.sessions.pop(job_id, None)
        if session is None:
            return
        if session.watcher is not None:
            await session.watcher.stop()
        if close_tab and session.page is not None:
            try:
                if not session.page.is_closed():
                    await session.page.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        for job_id in list(self.tasks):
            task = self.tasks.get(job_id)
            if task is not None:
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for job_id in list(self.sessions):
            await self._end_session(job_id, close_tab=False)

    async def wait(self, job_id: Optional[str] = None) -> None:
        """Until nothing (or nothing for this job) is queued or being filled."""
        while True:
            tasks = [t for j, t in list(self.tasks.items()) if job_id is None or j == job_id]
            queued = [j for j in self.queue if job_id is None or j == job_id]
            if not tasks and not queued:
                return
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.01)

    # --- one leg ---------------------------------------------------------------------------------

    async def _run(self, job_id: str, reason: str) -> None:
        record: Optional[ApplicationRecord] = None
        try:
            job = self.store.get(job_id)
            settings = load_settings(self.paths.settings)
            if reason == "prepare":
                job = self.store.move(job_id, "start_filling", by="app",
                                      detail=StatusDetail(message="Opening the application in Job App's browser window."))
                self._changed(job_id)
            profile = self.profile.load_profile()
            if profile is None or not profile.confirmed:
                raise Stop("failed", "Confirm your profile on the Profile screen first. Autofill fills forms from it.")
            record = ApplicationRecord(self.paths.applications / job_id)
            if not record.state.started_at:
                record.state.started_at = now_iso()
            session, reopened = await self._session(job, record, settings)
            record.log(f"Leg started: {reason}" + (" (the form was opened again)" if reopened else ""))
            user_changes: list[dict] = []
            if reason != "prepare":
                user_changes = await self._learn(job, session, reopened=reopened)
            if reopened and record.fields:
                await self._restore(session)
            if reason == "prepare":
                await self._first_steps(job, session, profile)
            if reason == "continue_in_review":
                outcome, summary = await self._review_check(job, session)
            else:
                outcome, summary = await self._agent_leg(job, session, settings, profile, reason, user_changes)
            if outcome == "failed":
                raise Stop("failed", summary or "The application could not be filled.")
            await self._after_leg(job, session, settings, outcome, summary, first=reason == "prepare")
        except asyncio.CancelledError:
            if record is not None:
                record.log("Stopped.")
                record.save()
            raise
        except Stop as stop:
            self._stop(job_id, record, stop)
        except Exception as exc:  # noqa: BLE001 - shown on the listing; the app keeps running
            log.exception("Application %s failed", job_id)
            self._stop(job_id, record, Stop("failed", f"Something went wrong while filling ({type(exc).__name__}: "
                                                      f"{clip(str(exc), 160)}). What was filled is kept; click "
                                                      "Prepare to try again."))
        finally:
            self._changed(job_id)

    def _stop(self, job_id: str, record: Optional[ApplicationRecord], stop: Stop) -> None:
        if record is not None:
            record.state.summary = stop.message
            record.log(f"Stopped: {stop.message}")
            record.save()
        try:
            job = self.store.get(job_id)
            if stop.event == "posting_closed":
                self.store.move(job_id, "posting_closed", by="app", detail=StatusDetail(message=stop.message))
            elif job.status in (Status.QUEUED, Status.FILLING):
                self.store.move(job_id, "failed", by="app", detail=StatusDetail(message=stop.message))
        except (KeyError, NotAllowed):
            pass

    async def _settle(self, page) -> None:
        for state, timeout in (("domcontentloaded", 15000), ("networkidle", 6000)):
            try:
                await page.wait_for_load_state(state, timeout=timeout)
            except Exception:  # noqa: BLE001 - busy pages never go idle
                pass

    async def _session(self, job: Job, record: ApplicationRecord, settings) -> tuple[Session, bool]:
        session = self.sessions.get(job.id)
        if session is not None and session.page is not None and not session.page.is_closed():
            return session, False
        await self._end_session(job.id, close_tab=False)
        window = self._window()
        url = job.apply_url or job.url
        try:
            page, problem = await window.open(url, settings.browser.channel)
        except Exception as exc:  # noqa: BLE001 - OpenFailed and the like
            raise Stop("failed", str(exc) or "Job App's browser window could not be opened.") from exc
        if problem:
            raise Stop("failed", f"The application page did not load ({clip(problem, 160)}). Check the link, then "
                                 "click Prepare to try again.")
        await self._settle(page)
        session = Session(job.id, page, record)
        session.snapshot = record.load_reading("snapshot.json")
        self.sessions[job.id] = session
        reopened = bool(record.fields)
        if reopened:
            record.state.page = 1
        record.state.url = page.url
        return session, reopened

    async def _restore(self, session: Session) -> None:
        """Put back what was filled before the form was opened again."""
        record = session.record
        reading = await read_page(session.page)
        values: dict[str, Any] = {}
        for item in record.fields:
            f = reading.field(item.key)
            if f is None or not f.empty or f.rule or is_empty(item.value) or item.value == "(entered by you)":
                continue
            if f.kind == "file":
                if item.source == "resume":
                    resume = current_resume(self.paths.profile)
                    if resume is not None:
                        values[f.key] = str(resume)
                elif item.source == "ai_draft" and (record.drafts / "cover_letter.docx").exists():
                    values[f.key] = str(record.drafts / "cover_letter.docx")
                continue
            values[f.key] = item.value
        if values:
            results = await fill(session.page, values, allowed_files=[self.paths.profile, record.drafts])
            ok = sum(1 for r in results if r.status == "ok")
            record.log(f"Put back {ok} of {len(values)} earlier values after the form was opened again.")

    async def _body_text(self, page) -> str:
        try:
            return await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 4000)")
        except Exception:  # noqa: BLE001
            return ""

    async def _first_steps(self, job: Job, session: Session, profile) -> None:
        """The certain parts, done by plain code before the agent's first leg (PLAN.md 8.1)."""
        page, record = session.page, session.record
        reading = await read_page(page)
        if not any(f.visible and f.kind != "file" for f in reading.fields):
            if looks_closed(await self._body_text(page)):
                raise Stop("posting_closed", "The posting says it is closed or no longer takes applications.")
            opener = next((b for b in reading.buttons if b.role == "open_application" and not b.disabled), None)
            if opener is not None and load_settings(self.paths.settings).never_click_next:
                record.log(f"Did not click '{opener.text}': your settings say Job App never clicks Apply or Next.")
                opener = None
            if opener is not None:
                try:
                    await click(page, opener.key, allow=("open_application",))
                    await self._settle(page)
                    reading = await read_page(page)
                    record.log(f"Opened the form with '{opener.text}'.")
                except Exception as exc:  # noqa: BLE001 - the agent gets the page as it is
                    record.log(f"Could not open the form: {exc}")
        record.state.url = page.url
        if blockers(reading):
            record.save()
            return
        resume = current_resume(self.paths.profile)
        planned, _kept = certain_values(reading, profile.facts, resume)
        uploads = [p for p in planned if p.source == "resume"]
        if uploads:
            before = reading
            results = await fill(page, {p.key: p.value for p in uploads}, allowed_files=[self.paths.profile])
            for p, r in zip(uploads, results):
                record.upsert(p.key, label=p.label, kind="file", category="resume", value=Path(p.value).name,
                              source="resume", filled_by="code", verified=r.status == "ok")
            await asyncio.sleep(self.prefill_wait)
            await self._settle(page)
            reading = await read_page(page)
            prefilled = 0
            for f in reading.fields:
                old = before.field(f.key)
                if f.kind != "file" and not f.empty and (old is None or old.empty):
                    record.upsert(f.key, label=f.label, kind=f.kind, category=f.category, options=f.options,
                                  required=f.required, value=f.value, source="site_prefill", filled_by="site")
                    prefilled += 1
            record.log("Attached your resume" + (f"; the site filled {prefilled} field(s) from it." if prefilled else "."))
            planned, _kept = certain_values(reading, profile.facts, resume)
        typed = [p for p in planned if p.source != "resume"]
        if typed:
            results = await fill(page, {p.key: p.value for p in typed})
            for p, r in zip(typed, results):
                f = reading.field(p.key)
                record.upsert(p.key, label=p.label, kind=f.kind if f else "", category=f.category if f else "",
                              required=bool(f and f.required), value=r.now if r.status == "ok" else p.value,
                              source="profile", filled_by="code", verified=r.status == "ok")
            ok = sum(1 for r in results if r.status == "ok")
            record.log(f"Filled {ok} field(s) from your profile.")
        record.save()

    async def _agent_leg(self, job: Job, session: Session, settings, profile, reason: str,
                         user_changes: list[dict]) -> tuple[str, str]:
        record = session.record
        record.state.legs += 1
        ctx = LegContext(job=job, page=session.page, record=record, answers=self.answers, settings=settings,
                         facts=profile.facts, resume=current_resume(self.paths.profile),
                         profile_folder=self.paths.profile, reason=reason, site_notes=self.paths.data / "site_notes",
                         writer=self.writer if settings.write_long_answers else None,
                         matcher=self.matcher if settings.match_questions_by_meaning else None,
                         on_event=lambda text: None)
        leg = Leg(ctx)
        brief = build_brief(ctx, user_changes)
        job = self.store.get(job.id)
        job.status_detail = StatusDetail(message="The agent is filling in the form.")
        self.store.update(job)
        self._changed(job.id)
        result = await self.run_leg(leg, brief)
        record.state.cost_usd = round(record.state.cost_usd + (result.cost_usd or 0.0), 4)
        record.save()
        if leg.outcome is None:
            if result.error is not None:
                return "failed", result.error.message + " What was filled is kept."
            return "failed", "The agent stopped before it finished the page. What was filled is kept; click Prepare to try again."
        if result.error is not None:
            record.log(f"The agent ended with an error after finishing: {result.error.kind}")
        return leg.outcome, leg.summary

    async def _review_check(self, job: Job, session: Session) -> tuple[str, str]:
        """Continue on a finished form: save your edits, look for errors, change nothing."""
        reading = await read_page(session.page)
        self._add_wordings(session.record)
        errors = [f.label or "(no label)" for f in reading.fields if f.visible and f.error]
        empty = [f.label or "(no label)" for f in reading.fields
                 if f.visible and f.required and f.empty and f.rule != "user"]
        parts = ["Your changes are saved."]
        if errors:
            parts.append("The page shows errors at: " + "; ".join(errors[:6]) + ".")
        if empty:
            parts.append("Still empty and required: " + "; ".join(empty[:6]) + ".")
        if not errors and not empty:
            parts.append("Nothing is missing that Job App can see. Submit it yourself when you are happy with it.")
        return "ready_for_review", " ".join(parts)

    async def _after_leg(self, job: Job, session: Session, settings, outcome: str, summary: str, *,
                         first: bool) -> None:
        record = session.record
        try:
            snapshot = await read_page(session.page)
            record.save_reading("snapshot.json", snapshot)
            session.snapshot = session.last_same = snapshot
            session.saved_values = snapshot.values()
            record.state.url = session.page.url
        except Exception as exc:  # noqa: BLE001 - the tab may have been closed
            log.warning("Could not read the form after the leg: %s", exc)
        message = summary or DEFAULT_MESSAGES.get(outcome, "")
        if not summary and outcome == "page_done" and settings.never_click_next:
            message = "This page is filled. Check it, click Next yourself, then click Continue."
        if first and settings.browser.mode == "own_chrome":
            message += (" (Autofill works in Job App's browser window, so the form is there, not in your Chrome.)")
        record.state.summary = message
        record.save()
        needs = [Need(label=n.label, page=n.page, reason=n.reason if n.required else f"Optional. {n.reason}".strip())
                 for n in record.state.needs]
        current = self.store.get(job.id)
        current.application.folder = f"applications/{job.id}"
        current.application.page = record.state.page
        current.application.cost_usd = record.state.cost_usd
        self.store.update(current)
        try:
            self.store.move(job.id, EVENTS[outcome], by="app", detail=StatusDetail(message=message, needs=needs))
        except NotAllowed:
            return
        record.log(f"Listing now: {EVENTS[outcome]}.")
        self._watch(session)

    # --- learning from you -------------------------------------------------------------------------

    def _watch(self, session: Session) -> None:
        if session.watcher is not None or session.page is None:
            return

        async def on_reading(reading: Reading) -> None:
            if overlaps(session.snapshot, reading):
                session.last_same = reading
                values = reading.values()
                if values != session.saved_values:
                    session.saved_values = values
                    session.record.save_reading("latest.json", reading)
            if not session.record.state.looks_submitted:
                text = f"{reading.title} {' '.join(reading.headings)} {await self._body_text(session.page)}"
                if THANKS.search(text):
                    self._looks_submitted(session)

        session.watcher = FormWatcher(session.page, interval=self.watch_interval, on_reading=on_reading)
        session.watcher.start()

    def _looks_submitted(self, session: Session) -> None:
        session.record.state.looks_submitted = True
        session.record.log("The page says the application was sent.")
        session.record.save()
        try:
            job = self.store.get(session.job_id)
        except KeyError:
            return
        setattr(job.application, "looks_submitted", True)
        self.store.update(job)
        self._changed(session.job_id)

    async def _learn(self, job: Job, session: Session, *, reopened: bool = False) -> list[dict]:
        """Save what you changed since the agent stopped, to the record and to your answers."""
        record = session.record
        before = session.snapshot or record.load_reading("snapshot.json")
        if before is None:
            return []
        after: Optional[Reading] = None
        if not reopened and session.page is not None and not session.page.is_closed():
            try:
                now = await read_page(session.page)
            except Exception:  # noqa: BLE001
                now = None
            after = now if overlaps(before, now) else session.last_same
            if now is not None and not overlaps(before, now) and (
                    any(f.visible for f in now.fields) or any(b.role in ("next", "submit") for b in now.buttons)):
                # You moved on to another page of the form yourself.
                if record.state.page not in record.state.pages_done:
                    record.state.pages_done.append(record.state.page)
                record.state.page += 1
                record.log(f"You moved on to page {record.state.page} of the form; your changes on the earlier page "
                           "were taken from the last reading of it.")
        if after is None:
            after = record.load_reading("latest.json") if reopened or session.page is None else session.last_same
        if after is None:
            return []
        out = []
        for change in changes(before, after):
            f = after.field(change.key)
            if f is None:
                continue
            existing = record.field(change.key)
            base = dict(label=f.label, kind=f.kind, category=f.category, options=f.options[:40], required=f.required,
                        filled_by="user", user_changed=True, source="user")
            if f.rule == "never":
                record.upsert(change.key, **base, value="(entered by you)")
                continue
            if f.rule == "user" or f.kind == "file":
                record.upsert(change.key, **base, value=change.after if f.kind != "file" else "(your file)")
                record.drop_need(key=change.key)
                continue
            by_app = existing is not None and existing.filled_by in ("agent", "code", "site") and not is_empty(existing.value)
            if existing is not None and existing.source == "ai_draft" and isinstance(change.after, str) and change.after:
                # Your edit of a draft: kept next to the draft, and (as an answer) a sample of your writing.
                name = (getattr(existing, "draft", "") or "answer.md").replace(".md", "")
                record.save_draft(f"{name}.your_version.md", f"# {f.label}\n\n{change.after}\n")
                record.log(f"You edited the AI draft of '{f.label}'; your version is kept as a sample of your writing.")
            record.upsert(change.key, **base, value=change.after)
            record.drop_need(key=change.key)
            read = record.state.questions.get(change.key) or {}
            sensitive = f.category.startswith("sensitive:") or bool(read.get("sensitive"))
            topic = read.get("topic") if read.get("topic") not in (None, "", "other") else (
                f.category.split(":", 1)[1] if f.category.split(":", 1)[-1] in SENSITIVE_TOPICS else "")
            answer, what = self.answers.learn(f.label, f.kind, f.options, change.after,
                                              source="user_correction" if by_app else "user", job_id=job.id,
                                              sensitive=sensitive, company=job.company, topic=topic,
                                              place=read.get("place", "") if topic else "")
            if answer is not None:
                record.upsert(change.key, answer_id=answer.id)
            out.append({"label": f.label, "change": change.change,
                        "value": "(answered)" if sensitive else clip(str(change.after), 200),
                        "now_saved_as": answer.id if answer else None, "learned": what})
            record.log(f"You {change.change} '{f.label}'" + (f"; saved to your answers ({what})." if answer else "."))
        for need in list(record.state.needs):
            if need.key:
                f = after.field(need.key)
                if f is not None and not f.empty:
                    record.drop_need(key=need.key)
            elif need.kind == "task":
                record.drop_need(label=need.label)
        record.save()
        return out

    def _add_wordings(self, record: ApplicationRecord) -> None:
        """You left a "similar question" answer as it was: remember this wording for it."""
        for item in record.fields:
            if item.source == "saved_answer" and item.match in ("similar", "same_meaning") and item.answer_id \
                    and not item.user_changed:
                self.answers.add_wording(item.answer_id, item.label)

    # --- what the dashboard shows ----------------------------------------------------------------------

    def application_view(self, job_id: str) -> Optional[dict]:
        folder = self.paths.applications / job_id
        if not folder.exists():
            return None
        session = self.sessions.get(job_id)
        record = session.record if session is not None else ApplicationRecord(folder)
        fields = []
        for item in record.fields:
            fields.append({
                "label": item.label or "(no label)", "value": item.value, "source": item.source,
                "filled_by": item.filled_by, "verified": item.verified, "user_changed": item.user_changed,
                "required": item.required, "page": item.page, "category": item.category,
                "answer_id": item.answer_id, "match": item.match, "draft": getattr(item, "draft", ""),
            })
        drafts = []
        if record.drafts.exists():
            for path in sorted(record.drafts.iterdir()):
                entry = {"name": path.name}
                if path.suffix == ".md":
                    entry["text"] = path.read_text(encoding="utf-8")[:6000]
                drafts.append(entry)
        return {
            "state": record.state.model_dump(mode="json"), "fields": fields, "drafts": drafts,
            "events": record.events(20), "folder": str(folder), "open": session is not None,
            "busy": self.busy(job_id),
        }
