"""Everything behind the "Profile and requirements" screen.

Two agents work here: one reads your resume into a profile, one reads your
requirements into search terms. Both run in the background; the dashboard is
told when they start and finish. What they return is checked by code before it
is saved, and nothing is treated as confirmed until you say so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from pydantic import ValidationError

from ..commands import LOGS_HINT
from ..agents.runner import AgentError, AgentReply
from ..core.events import EventBus
from ..core.jsonfile import JsonCorrupt, read_json, write_json_atomic
from ..core.models import now_iso
from ..paths import Paths
from .models import (
    BuilderOutput, Facts, ProfileFile, ReadingFile, ResumeSource, SearchReading, ToConfirm, agent_schema,
)
from .requirements import (
    BadRequirements, clean_lines, fingerprint, norm, read_requirements, reconcile, write_requirements,
)
from .resume import BadResume, ResumeText, current_resume, extract_text, file_sha256, save_upload, write_resume_text

log = logging.getLogger("jobapp.profile")

# ask(name=..., instructions=..., prompt=..., schema=...) -> AgentReply
AskFn = Callable[..., Awaitable[AgentReply]]


class Busy(Exception):
    """The same kind of work is already running."""


def load_instructions(paths: Paths, name: str) -> str:
    shared = (paths.prompts / "shared_rules.md").read_text(encoding="utf-8")
    own = (paths.prompts / f"{name}.md").read_text(encoding="utf-8")
    return f"{shared.strip()}\n\n{own.strip()}\n"


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def not_in_resume(facts: Facts, resume: ResumeText) -> list[ToConfirm]:
    """Values the agent returned that do not appear in the resume.

    The agent is told to extract only what the resume states. This is the
    check that it did: anything that cannot be found in the text is put in
    front of the user as a question instead of being trusted.
    """
    haystack = _squash(resume.text + " " + " ".join(resume.links))
    digits = re.sub(r"\D", "", resume.text)
    found: list[ToConfirm] = []

    def check(field: str, value: str, label: str, *, as_link: bool = False, as_phone: bool = False) -> None:
        value = (value or "").strip()
        if not value:
            return
        if as_phone:
            # Allow for a country code the resume does not show: up to three leading digits may be extra.
            needle = re.sub(r"\D", "", value)
            ok = any(len(needle[skip:]) >= 7 and needle[skip:] in digits for skip in range(4))
        elif as_link:
            needle = _squash(re.sub(r"^(https?://)?(www\.)?", "", value.lower()).rstrip("/"))
            ok = bool(needle) and needle in haystack
        else:
            ok = _squash(value) in haystack
        if not ok:
            found.append(ToConfirm(field=field, question=f'{label} "{value}" was not found in your resume. Is it right?'))

    check("name.first", facts.name.first, "First name")
    check("name.last", facts.name.last, "Last name")
    check("contact.email", facts.contact.email, "Email")
    check("contact.phone", facts.contact.phone, "Phone number", as_phone=True)
    check("links.linkedin", facts.links.linkedin, "LinkedIn link", as_link=True)
    check("links.github", facts.links.github, "GitHub link", as_link=True)
    check("links.website", facts.links.website, "Website", as_link=True)
    for index, item in enumerate(facts.education):
        check(f"education[{index}].school", item.school, "School")
    for index, item in enumerate(facts.experience):
        check(f"experience[{index}].employer", item.employer, "Employer")
    return found


class ProfileService:
    def __init__(
        self,
        paths: Paths,
        bus: EventBus,
        ask: AskFn,
        check_sign_in: Callable[[], Optional[bool]],
    ):
        self.paths = paths
        self.bus = bus
        self.ask = ask
        self._check_sign_in = check_sign_in
        self.signed_in: Optional[bool] = None
        self.busy = {"profile": False, "requirements": False}
        self.errors = {"resume": "", "profile": "", "requirements": ""}
        self._tasks: set[asyncio.Task] = set()
        self._sha_cache: tuple[Optional[tuple], str] = (None, "")

    # --- files ------------------------------------------------------------------

    @property
    def _profile_path(self) -> Path:
        return self.paths.profile / "profile.json"

    @property
    def _requirements_path(self) -> Path:
        return self.paths.profile / "requirements.md"

    @property
    def _reading_path(self) -> Path:
        return self.paths.profile / "search_profile.json"

    def _load(self, path: Path, model):
        try:
            return model.model_validate(read_json(path))
        except FileNotFoundError:
            return None
        except (JsonCorrupt, ValidationError) as exc:
            log.error("%s could not be read and is being ignored: %s", path.name, exc)
            return None

    def load_profile(self) -> Optional[ProfileFile]:
        return self._load(self._profile_path, ProfileFile)

    def load_reading(self) -> Optional[ReadingFile]:
        return self._load(self._reading_path, ReadingFile)

    def _resume_sha(self, resume: Path) -> str:
        st = resume.stat()
        key = (str(resume), st.st_mtime_ns, st.st_size)
        if self._sha_cache[0] != key:
            self._sha_cache = (key, file_sha256(resume))
        return self._sha_cache[1]

    # --- what the dashboard shows -------------------------------------------------

    def view(self) -> dict:
        resume = current_resume(self.paths.profile)
        resume_info = None
        if resume is not None:
            st = resume.stat()
            resume_info = {
                "file": resume.name,
                "bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        profile = self.load_profile()
        hard, soft = read_requirements(self._requirements_path)
        reading = self.load_reading()
        stale = bool(profile and resume and profile.source.sha256 and profile.source.sha256 != self._resume_sha(resume))
        reading_stale = bool(reading and reading.source_sha256 != fingerprint(hard, soft))
        return {
            "resume": resume_info,
            "facts": profile.facts.model_dump(mode="json") if profile else None,
            "to_confirm": [item.model_dump() for item in profile.to_confirm] if profile else [],
            "read_at": profile.source.read_at if profile else "",
            "confirmed": bool(profile and profile.confirmed),
            "confirmed_at": profile.confirmed_at if profile else None,
            "stale": stale,
            "requirements": {"hard": hard, "soft": soft},
            "reading": reading.reading.model_dump(mode="json") if reading else None,
            "reading_at": reading.read_at if reading else "",
            "reading_confirmed": bool(reading and reading.confirmed and not reading_stale),
            "reading_stale": reading_stale,
            "busy": dict(self.busy),
            "errors": dict(self.errors),
            "ready": bool(
                profile and profile.confirmed and (hard or soft) and reading and reading.confirmed and not reading_stale
            ),
        }

    def _changed(self) -> None:
        self.bus.publish("profile_changed")

    # --- sign-in ------------------------------------------------------------------

    async def refresh_sign_in(self) -> Optional[bool]:
        try:
            self.signed_in = await asyncio.to_thread(self._check_sign_in)
        except Exception as exc:  # noqa: BLE001 - "unknown" is a fine answer
            log.warning("Could not check the Claude sign-in: %s", exc)
            self.signed_in = None
        self._changed()
        return self.signed_in

    def _agent_failed(self, exc: AgentError) -> str:
        if exc.kind == "not_signed_in":
            self.signed_in = False
        return exc.message

    # --- background work ------------------------------------------------------------

    def _start(self, kind: str, work: Callable[[], Awaitable[None]]) -> None:
        if self.busy[kind]:
            raise Busy(kind)
        self.busy[kind] = True
        self.errors[kind] = ""
        self._changed()

        async def run() -> None:
            try:
                await work()
            except AgentError as exc:
                self.errors[kind] = self._agent_failed(exc)
            except (BadResume, BadRequirements) as exc:
                self.errors[kind] = str(exc)
            except ValidationError as exc:
                log.error("Agent output for %s did not have the expected shape: %s", kind, exc)
                self.errors[kind] = AgentError("bad_output").message
            except Exception:  # noqa: BLE001 - never let a background task die silently
                log.exception("Unexpected error in %s work", kind)
                self.errors[kind] = f"Something went wrong. The details are in app.log in {LOGS_HINT}."
            finally:
                self.busy[kind] = False
                self._changed()

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """For tests: wait until background work has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- resume and profile -----------------------------------------------------------

    def save_resume(self, filename: str, data: bytes) -> None:
        """Keep an uploaded resume and start reading it. Raises BadResume or Busy."""
        if self.busy["profile"]:
            raise Busy("profile")
        self.errors["resume"] = ""
        path = save_upload(self.paths.profile, filename, data)
        try:
            write_resume_text(self.paths.profile, extract_text(path))
        except BadResume as exc:
            # The file is kept, but say plainly that it cannot be read.
            self.errors["resume"] = str(exc)
            self._changed()
            raise
        self.start_profile_read()

    def start_profile_read(self) -> None:
        if current_resume(self.paths.profile) is None:
            raise BadResume("There is no resume yet. Upload a PDF or Word file first.")
        self._start("profile", self._read_profile)

    async def _read_profile(self) -> None:
        resume = current_resume(self.paths.profile)
        if resume is None:
            raise BadResume("There is no resume yet. Upload a PDF or Word file first.")
        extracted = extract_text(resume)
        write_resume_text(self.paths.profile, extracted)
        prompt = (
            f"Today's date: {date.today().isoformat()}\n"
            f"Resume file: {resume.name}\n\n"
            f"<resume>\n{extracted.as_markdown()}</resume>"
        )
        reply = await self.ask(
            name="profile_builder",
            instructions=load_instructions(self.paths, "profile_builder"),
            prompt=prompt,
            schema=agent_schema(BuilderOutput),
        )
        self.signed_in = True
        out = BuilderOutput.model_validate(reply.data)
        facts = Facts.model_validate(out.model_dump(exclude={"to_confirm"}))
        questions = [q for q in out.to_confirm if q.question] + not_in_resume(facts, extracted)
        profile = ProfileFile(
            source=ResumeSource(file=resume.name, sha256=file_sha256(resume), read_at=now_iso()),
            confirmed=False,
            facts=facts,
            to_confirm=questions,
        )
        write_json_atomic(self._profile_path, profile.model_dump(mode="json"))
        log.info("Profile read from %s: %d education, %d experience, %d questions",
                 resume.name, len(facts.education), len(facts.experience), len(questions))

    def save_facts(self, raw: Any) -> ProfileFile:
        """Save the details as you edited them, and mark them confirmed."""
        facts = Facts.model_validate(raw)
        profile = self.load_profile() or ProfileFile()
        profile.facts = facts
        profile.confirmed = True
        profile.confirmed_at = now_iso()
        profile.to_confirm = []  # you have looked; the questions are answered
        resume = current_resume(self.paths.profile)
        if resume is not None and not profile.source.sha256:
            profile.source = ResumeSource(file=resume.name, sha256=file_sha256(resume), read_at="")
        write_json_atomic(self._profile_path, profile.model_dump(mode="json"))
        self._changed()
        return profile

    # --- requirements -----------------------------------------------------------------

    def save_requirements(self, hard: Any, soft: Any) -> None:
        """Write your requirements to requirements.md and start reading them."""
        if self.busy["requirements"]:
            raise Busy("requirements")
        hard_lines, soft_lines = clean_lines(hard), clean_lines(soft)
        in_both = {norm(line) for line in hard_lines} & {norm(line) for line in soft_lines}
        if in_both:
            line = next(line for line in hard_lines if norm(line) in in_both)
            raise BadRequirements(f'"{line}" is in both boxes. Keep it in one.')
        write_requirements(self._requirements_path, hard_lines, soft_lines)
        if not hard_lines and not soft_lines:
            write_json_atomic(self._reading_path, ReadingFile(
                source_sha256=fingerprint([], []), read_at=now_iso()).model_dump(mode="json"))
            self.errors["requirements"] = ""
            self._changed()
            return
        self.start_requirements_read()

    def start_requirements_read(self) -> None:
        hard, soft = read_requirements(self._requirements_path)
        if not hard and not soft:
            raise BadRequirements("Write at least one requirement or preference first.")
        self._start("requirements", self._read_requirements)

    def profile_context(self) -> dict:
        """A few facts about the user that the agents get for context."""
        return self._profile_context()

    def _profile_context(self) -> dict:
        profile = self.load_profile()
        if profile is None:
            return {}
        facts = profile.facts
        return {
            "education": [
                {"degree": e.degree, "field": e.field, "end": e.end, "end_is_expected": e.end_is_expected}
                for e in facts.education
            ],
            "recent_titles": [e.title for e in facts.experience[:4] if e.title],
            "skills": facts.skills[:40],
            "location": ", ".join(part for part in (facts.contact.city, facts.contact.state, facts.contact.country) if part),
        }

    def resume_context(self) -> dict:
        """Your skills, positions and projects, for matching jobs to your resume."""
        profile = self.load_profile()
        if profile is None or not profile.confirmed:
            return {}
        facts = profile.facts
        return {
            "skills": [s for s in facts.skills[:60] if s],
            "experience": [{"title": e.title, "employer": e.employer, "highlights": e.highlights[:4]}
                           for e in facts.experience[:6]],
            "projects": [{"name": p.name, "summary": p.summary[:300], "tools": p.tools[:10]}
                         for p in facts.projects[:6]],
        }

    async def _read_requirements(self) -> None:
        hard, soft = read_requirements(self._requirements_path)
        previous = self.load_reading()
        current = {norm(line) for line in hard + soft}
        settled = [line for line in (previous.settled if previous else []) if norm(line) in current]
        prompt = json.dumps(
            {"hard": hard, "soft": soft, "settled": settled, "profile": self._profile_context(),
             "today": date.today().isoformat()},
            indent=1, ensure_ascii=False,
        )
        reply = await self.ask(
            name="requirements_reader",
            instructions=load_instructions(self.paths, "requirements_reader"),
            prompt=prompt,
            schema=agent_schema(SearchReading),
        )
        self.signed_in = True
        reading = reconcile(SearchReading.model_validate(reply.data), hard, soft, settled)
        # If the boxes changed while the agent was working, this is recorded
        # against the lines it actually read, and shows as out of date.
        saved = ReadingFile(source_sha256=fingerprint(hard, soft), read_at=now_iso(), confirmed=False,
                            settled=settled, reading=reading)
        write_json_atomic(self._reading_path, saved.model_dump(mode="json"))
        log.info("Requirements read: %d hard, %d soft, %d questions, %d titles",
                 len(reading.hard), len(reading.soft), len(reading.ambiguous), len(reading.titles))

    def settle(self, text: str, box: str) -> None:
        """You answered a question about one line: it belongs in ``box``."""
        if box not in ("hard", "soft"):
            raise BadRequirements("Choose either the hard or the soft box.")
        if self.busy["requirements"]:
            raise Busy("requirements")
        hard, soft = read_requirements(self._requirements_path)
        key = norm(text or "")
        line = next((item for item in hard + soft if norm(item) == key), None)
        if line is None:
            raise BadRequirements("That line is no longer in your requirements.")
        was_hard = any(norm(item) == key for item in hard)
        moved = was_hard != (box == "hard")
        if moved:
            hard = [item for item in hard if norm(item) != key]
            soft = [item for item in soft if norm(item) != key]
            (hard if box == "hard" else soft).append(line)
            write_requirements(self._requirements_path, hard, soft)
        reading = self.load_reading() or ReadingFile()
        if key not in {norm(item) for item in reading.settled}:
            reading.settled.append(line)
        reading.reading.ambiguous = [item for item in reading.reading.ambiguous if norm(item.text) != key]
        write_json_atomic(self._reading_path, reading.model_dump(mode="json"))
        if moved:
            # The line needs a test or a weight in its new box.
            self.start_requirements_read()
        else:
            self._changed()

    def save_reading(self, patch: Any) -> ReadingFile:
        """Save your corrections to the reading, and mark it confirmed."""
        if not isinstance(patch, dict):
            raise BadRequirements("Expected the corrected reading.")
        if self.busy["requirements"]:
            raise Busy("requirements")
        saved = self.load_reading()
        hard, soft = read_requirements(self._requirements_path)
        if saved is None or saved.source_sha256 != fingerprint(hard, soft):
            raise BadRequirements("Your requirements changed since they were read. Save them so they are read again.")
        current = saved.reading.model_dump()
        for key in ("titles", "keywords", "locations", "types", "pay_floor", "remote_ok"):
            if key in patch:
                current[key] = patch[key]
        weights = patch.get("weights") or {}
        if not isinstance(weights, dict):
            raise BadRequirements("Weights should be given per preference.")
        for item in current["soft"]:
            if item["text"] in weights:
                item["weight"] = weights[item["text"]]
        try:
            reading = SearchReading.model_validate(current)
        except ValidationError as exc:
            raise BadRequirements("Some of the corrected values are not valid.") from exc
        # Confirming says every line is where you want it.
        for item in reading.ambiguous:
            if norm(item.text) not in {norm(line) for line in saved.settled}:
                saved.settled.append(item.text)
        reading.ambiguous = []
        saved.reading = reconcile(reading, hard, soft, saved.settled)
        saved.confirmed = True
        saved.confirmed_at = now_iso()
        write_json_atomic(self._reading_path, saved.model_dump(mode="json"))
        self._changed()
        return saved
