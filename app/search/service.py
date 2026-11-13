"""What the server uses for searching: start and stop a search, report on it, and read one pasted link."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable, Optional

from pydantic import ValidationError

from ..commands import cmd
from ..agents.runner import AgentError
from ..core.events import EventBus
from ..core.jsonfile import JsonCorrupt, read_json
from ..core.models import HardResult, Match, Settings, now_iso
from ..core.settings import load_settings
from ..core.store import TrackerStore
from ..paths import Paths
from ..profile.service import ProfileService
from . import pages, sources
from .agents import AskFn, WebAgentFn, evaluate, match_fields
from .boards import BoardStore
from .http import FetchError, Web
from .models import Posting
from .run import SearchContext, SearchRun, legit_note, save_posting_text, search_dir
from .text import clip

log = logging.getLogger("jobapp.search")


class SearchRefused(Exception):
    """A search cannot start. The message is written for the user."""


class SearchService:
    def __init__(
        self,
        paths: Paths,
        tracker: TrackerStore,
        bus: EventBus,
        profile: ProfileService,
        ask: AskFn,
        *,
        web: Web,
        web_agent: Optional[WebAgentFn],
        headless_factory: Optional[Callable[[Settings], Optional[pages.HeadlessReader]]] = None,
        usage=None,
    ):
        self.paths = paths
        self.tracker = tracker
        self.bus = bus
        self.profile = profile
        self.ask = ask
        self.web = web
        self.web_agent = web_agent
        self.headless_factory = headless_factory
        self.usage = usage                 # the daily usage cap, if given
        self.current: Optional[SearchRun] = None
        self._task: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self.judging: set[str] = set()   # job ids added by link that are being judged

    # --- reports ------------------------------------------------------------------------

    def _latest_path(self):
        folder = search_dir(self.paths)
        if not folder.is_dir():
            return None
        files = sorted(folder.glob("*.json"))
        return files[-1] if files else None

    def latest_report(self) -> Optional[dict]:
        if self.current is not None and self.current.status == "running":
            return self.current.report()
        path = self._latest_path()
        if path is None:
            return None
        try:
            return read_json(path)
        except (JsonCorrupt, OSError, ValueError):
            return None

    def view(self) -> dict:
        running = self.current is not None and self.current.status == "running"
        last = None
        if self.current is not None and not running:
            last = self.current.view()
        else:
            report = self.latest_report() if not running else None
            if report:
                last = {k: report.get(k) for k in ("id", "status", "message", "started_at", "finished_at", "counts",
                                                   "cost_usd")}
        return {
            "running": running,
            "run": self.current.view() if running else None,
            "last": last,
            "judging": sorted(self.judging),
        }

    def boards_view(self) -> list[dict]:
        settings = load_settings(self.paths.settings)
        return BoardStore(self.paths.data / "company_boards.json").view(settings.search.watchlist)

    # --- running a search ------------------------------------------------------------------

    def start(self) -> SearchRun:
        if self.current is not None and self.current.status == "running":
            raise SearchRefused("A search is already running.")
        view = self.profile.view()
        if not view["ready"]:
            raise SearchRefused("Finish the Profile and requirements screen first. "
                                "The search uses your confirmed requirements.")
        if self.profile.signed_in is False:
            raise SearchRefused(f"The agents are not signed in to Claude. Run '{cmd('signin')}', then try again.")
        if self.usage is not None and self.usage.problem():
            raise SearchRefused(self.usage.problem())
        settings = load_settings(self.paths.settings)
        use = {"boards": True, "lists": True, "web": True, **settings.search.sources}
        if not any(use.values()):
            raise SearchRefused("Every source is switched off. Turn one on in the Search screen.")
        reading = self.profile.load_reading().reading
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        context = SearchContext(
            paths=self.paths, tracker=self.tracker, bus=self.bus, web=self.web, ask=self.ask,
            web_agent=self.web_agent,
            headless=self.headless_factory(settings) if self.headless_factory else None,
            settings=settings, reading=reading, profile=self.profile.profile_context(),
            resume=(self.profile.resume_context() or None) if settings.search.resume_match else None,
        )
        run = SearchRun(context, run_id)
        self.current = run
        log.info("Search %s started", run_id)

        async def go():
            await run.run()
            if run.status == "failed" and "not signed in" in run.message:
                self.profile.signed_in = False

        self._task = asyncio.create_task(go())
        self.bus.publish("search_changed")
        return run

    def stop(self) -> bool:
        if self.current is None or self.current.status != "running":
            return False
        self.current.stop_requested = True
        self.current.message = "Stopping after the current step"
        self.bus.publish("search_changed")
        return True

    async def close(self) -> None:
        if self.current is not None and self.current.status == "running":
            self.current.stop_requested = True
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        for task in list(self._tasks):
            task.cancel()

    async def wait(self) -> None:
        """For tests: wait for the running search and any link judging to finish."""
        while (self._task is not None and not self._task.done()) or self._tasks:
            await asyncio.gather(*([self._task] if self._task else []), *list(self._tasks), return_exceptions=True)

    # --- one pasted link -----------------------------------------------------------------------

    async def read_link(self, url: str) -> Posting:
        """Read a posting the user pasted. Raises FetchError."""
        settings = load_settings(self.paths.settings)
        headless = self.headless_factory(settings) if self.headless_factory else None
        try:
            return await asyncio.wait_for(pages.read_posting(self.web, url, headless=headless), timeout=60)
        except asyncio.TimeoutError as exc:
            raise FetchError("timeout", url, "The page took too long to read.") from exc
        finally:
            if headless is not None:
                await headless.close()

    def remember_link(self, job_id: str, posting: Posting) -> None:
        save_posting_text(self.paths, job_id, posting)
        store = BoardStore(self.paths.data / "company_boards.json")
        if store.learn_from_url(posting.url, posting.company) is not None:
            store.save()

    def known_company(self, posting: Posting) -> str:
        """The company name the boards file has for the posting's board, if any."""
        found = sources.identify(posting.url)
        if not found:
            return ""
        board = BoardStore(self.paths.data / "company_boards.json").get(sources.board_for(found).key)
        return board.company if board and board.company else ""

    def can_judge(self) -> bool:
        return bool(self.profile.view()["ready"]) and self.profile.signed_in is not False

    def start_judging(self, job_id: str, posting: Posting, keep_summary: bool) -> None:
        """Judge a job added by link against the requirements, in the background."""
        self.judging.add(job_id)

        async def work():
            started = time.monotonic()
            try:
                reading = self.profile.load_reading().reading
                use_resume = load_settings(self.paths.settings).search.resume_match
                verdicts, _reply = await evaluate(self.ask, self.paths, reading, self.profile.profile_context(),
                                                  [("p1", posting)],
                                                  resume=(self.profile.resume_context() or None) if use_resume else None)
                verdict = verdicts.get("p1")
                if verdict is None:
                    return
                job = self.tracker.get(job_id)
                legit = legit_note(posting)
                notes = [n for n in posting.notes if not n.startswith("Team: ")] + ([legit[1]] if legit[1] else [])
                if verdict.closed:
                    notes.append("The posting says it is closed.")
                job.match = Match(
                    hard=[HardResult(**h) for h in verdict.hard], **match_fields(verdict),
                    concerns=list(dict.fromkeys(verdict.concerns + notes)),
                    unverified=[h["requirement"] for h in verdict.unknown],
                    failed=[h["requirement"] for h in verdict.failed], checked_at=now_iso(),
                )
                if not keep_summary and verdict.summary:
                    job.description_summary = verdict.summary
                self.tracker.update(job)
                log.info("Judged %s added by link in %.1fs", job_id, time.monotonic() - started)
            except AgentError as exc:
                if exc.kind == "not_signed_in":
                    self.profile.signed_in = False
                log.warning("Could not judge %s: %s", job_id, exc)
            except KeyError:
                pass  # removed meanwhile
            except Exception:  # noqa: BLE001
                log.exception("Judging %s failed", job_id)
            finally:
                self.judging.discard(job_id)
                self.bus.publish("jobs_changed", job_id=job_id)

        task = asyncio.create_task(work())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def description_for(posting: Posting) -> str:
    return clip(posting.description, 600)
