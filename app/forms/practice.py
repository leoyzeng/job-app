"""Trying the form engine on a practice form, from the dashboard.

Opens one of the practice forms in Job App's browser window, fills the fields
plain code can fill with certainty from your confirmed profile, and lists
what it left and why. After you change things in the window, "Check what I
changed" reads the form again and lists your changes. This is the first and
last step of every application (PLAN.md 8.1 and 8.2), without the agent in
between. Prepare on a listing does the whole thing, agent included.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..core.events import EventBus
from ..core.models import now_iso
from ..core.settings import load_settings
from ..paths import Paths
from ..profile.models import ProfileFile
from ..profile.resume import current_resume
from .certain import certain_values
from .compare import changes
from .fill import fill
from .model import Reading
from .read import read_page
from .watch import FormWatcher

log = logging.getLogger("jobapp.forms")

STYLES = {
    "greenhouse": "Greenhouse style",
    "lever": "Lever style",
    "ashby": "Ashby style",
    "workday": "Workday style, first step",
    "embedded": "Company page with the form in a frame",
}


# Values that exercise every kind of field on each practice form (used by the check command and the tests).
# "{resume}" is replaced with the path of a file that may be attached.
PRACTICE_VALUES = {
    "greenhouse": {
        "First Name": "Sam", "Last Name": "Rivera", "Email": "sam@example.com", "Country": "United States",
        "Phone": "412-555-0142", "Location (City)": "Pittsburgh, PA", "Resume/CV": "{resume}",
        "LinkedIn Profile": "https://linkedin.com/in/sam", "How did you hear about this job?": "Company website",
        "Are you legally authorized to work in the United States?": "Yes",
        "Why are you interested in Practice Robotics?": "I like robots.\nAnd this team.",
        "School": "Carnegie Mellon University", "Degree": "Master's", "Discipline": "Electrical Engineering",
        "End date month": "December", "End date year": "2027", "Gender": "Decline To Self Identify",
    },
    "lever": {
        "Resume/CV": "{resume}", "Full name": "Sam Rivera", "Email": "sam@example.com", "Phone": "412-555-0142",
        "Are you at least 18 years of age?": "Yes", "Which teams interest you?": ["Perception", "Controls"],
        "Expected graduation year": "2027", "Earliest start date": "2027-05-17",
        "Additional information": "Hello there.", "Race": "Asian",
    },
    "ashby": {
        "Name": "Sam Rivera", "Email": "sam@example.com", "Phone Number": "4125550142", "Resume": "{resume}",
        "Where are you located?": "Pittsburgh, PA", "Are you currently enrolled in a degree program?": "Yes",
        "Which of these have you used?": ["ROS or ROS 2", "C++"], "When can you start?": "05/17/2027",
        "Portfolio or personal website": "https://samrivera.example.com",
    },
    "workday": {
        "How Did You Hear About Us?": "Career Fair", "Have you previously been employed by Practice Robotics?": "No",
        "First Name": "Sam", "Last Name": "Rivera", "City": "Pittsburgh", "State": "Pennsylvania",
        "Email Address": "sam@example.com", "Phone Device Type": "Mobile", "Phone Number": "4125550142",
    },
}
PRACTICE_VALUES["embedded"] = PRACTICE_VALUES["greenhouse"]


def practice_values(style: str, resume: str) -> dict:
    return {label: (resume if value == "{resume}" else value) for label, value in PRACTICE_VALUES[style].items()}


def _why_left(field) -> str:
    if field.rule:
        return field.rule_reason
    if field.category.startswith("sensitive:"):
        return "A sensitive question: only ever answered with your own saved answer."
    if field.kind == "file":
        return "Only your resume is attached by plain code."
    return "For the application agent (when you click Prepare on a listing) or for you."


class PracticeService:
    def __init__(self, paths: Paths, browsers, bus: EventBus, load_profile):
        self.paths = paths
        self.browsers = browsers
        self.bus = bus
        self.load_profile = load_profile
        self.status = "idle"         # idle, working, ready, error
        self.style = ""
        self.message = ""
        self.filled: list[dict] = []
        self.kept: list[dict] = []
        self.left: list[dict] = []
        self.found: list[dict] = []   # your changes, from the last check
        self.checked_at = ""
        self.page = None
        self.snapshot: Optional[Reading] = None
        self.watcher: Optional[FormWatcher] = None
        self._task: Optional[asyncio.Task] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def view(self) -> dict:
        return {
            "styles": STYLES, "status": self.status, "style": self.style, "message": self.message,
            "filled": self.filled, "kept": self.kept, "left": self.left, "changes": self.found,
            "checked_at": self.checked_at,
        }

    def _changed(self) -> None:
        self.bus.publish("practice_changed")

    def start(self, style: str, base_url: str) -> None:
        if style not in STYLES:
            raise ValueError("There is no such practice form.")
        if self.status == "working":
            raise RuntimeError("The practice form is still being filled.")
        self.status, self.style, self.message = "working", style, "Opening the practice form in Job App's browser window"
        self.filled, self.kept, self.left, self.found, self.checked_at = [], [], [], [], ""
        self._changed()
        self.loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(f"{base_url.rstrip('/')}/practice/{style}.html"))

    async def _run(self, url: str) -> None:
        try:
            await self._stop_watching()
            settings = load_settings(self.paths.settings)
            page, problem = await self.browsers.window.open(url, settings.browser.channel)
            if problem:
                raise RuntimeError(f"The practice form did not load ({problem}).")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:  # noqa: BLE001 - a page that keeps talking is still usable
                pass
            self.page = page
            self.message = ""
            reading = await read_page(page)
            profile: Optional[ProfileFile] = self.load_profile()
            if profile is None or not profile.confirmed:
                self.message = ("Your profile is not confirmed yet, so nothing was filled. Confirm it on the Profile "
                                "and requirements screen, then try again.")
                planned, kept = [], []
            else:
                planned, kept = certain_values(reading, profile.facts, current_resume(self.paths.profile))
            results = await fill(page, {p.key: p.value for p in planned}, allowed_files=[self.paths.profile])
            by_key = {r.key: r for r in results}
            self.filled = [{"label": p.label, "value": p.value if p.source != "resume" else p.value.split("\\")[-1].split("/")[-1],
                            "status": by_key[p.key].status, "message": by_key[p.key].message} for p in planned]
            self.kept = kept
            self.snapshot = await read_page(page)
            done = {p.key for p in planned} | {k["key"] for k in kept}
            self.left = [{"label": f.label or "(no label)", "kind": f.kind, "required": f.required,
                          "why": _why_left(f)}
                         for f in self.snapshot.fields if f.key not in done and f.empty]
            self.watcher = FormWatcher(page, interval=4.0)
            self.watcher.start()
            ok = sum(1 for f in self.filled if f["status"] == "ok")
            self.status = "ready"
            if not self.message:
                self.message = (f"Filled {ok} of {len(self.snapshot.fields)} fields from your profile. "
                                "Change anything you like in Job App's browser window, then check what you changed.")
            log.info("Practice %s: filled %d, left %d", self.style, ok, len(self.left))
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            log.exception("Practice form failed")
            self.status = "error"
            self.message = str(exc) if str(exc) else type(exc).__name__
        finally:
            self._changed()

    async def check(self) -> list[dict]:
        if self.snapshot is None or self.status != "ready":
            raise RuntimeError("Open a practice form first.")
        if self.page is None or self.page.is_closed():
            latest = self.watcher.latest if self.watcher else None
            if latest is None:
                raise RuntimeError("The practice tab was closed before anything could be read.")
            now = latest
            note = "The practice tab was closed, so this is what it showed a few seconds before."
        else:
            now = await read_page(self.page)
            note = ""
        self.found = [c.model_dump(mode="json") for c in changes(self.snapshot, now)]
        self.checked_at = now_iso()
        self.message = note or ("No changes found." if not self.found else
                                f"You changed {len(self.found)} field{'s' if len(self.found) != 1 else ''}.")
        self._changed()
        return self.found

    async def _stop_watching(self) -> None:
        if self.watcher is not None:
            await self.watcher.stop()
            self.watcher = None

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        await self._stop_watching()

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
