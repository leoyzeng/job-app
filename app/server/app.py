"""The local web server behind the dashboard.

It listens on this computer only, and every data request must carry the session
cookie that is handed out when the dashboard is opened by `jobapp run`. That
keeps other programs and web pages from pressing the dashboard's buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..commands import cmd
from .. import CHECKPOINT, __version__
from ..apply.agent import RunLeg
from ..apply.service import ApplicationRefused, ApplicationService
from ..apply.questions import QuestionMatcher
from ..apply.writer import Writer
from ..browser.manager import MODES, Browsers, OpenFailed
from ..forms.practice import STYLES as PRACTICE_STYLES
from ..forms.practice import PracticeService
from ..core import lifecycle
from ..core.dedupe import BadUrl, job_id_for, make_dedupe_keys, normalize_url
from ..core.events import EventBus
from ..core.lifecycle import NotAllowed
from ..core.models import Details, Job, Source, Status
from ..core.settings import BadSettings, load_settings, update_settings
from ..core.store import DuplicateJob, TrackerStore, TrackerUnreadable
from ..core.ranking import order_new
from ..core.usage import Usage
from ..paths import Paths
from ..profile.requirements import BadRequirements
from ..profile.resume import MAX_BYTES, BadResume
from ..profile.service import AskFn, Busy, ProfileService
from ..search.http import FetchError
from ..search.pages import is_restricted
from ..search.schedule import DailySearch
from ..search.service import SearchRefused, SearchService
from ..search.text import clip

log = logging.getLogger("jobapp.server")

COOKIE = "jobapp_session"

# Parts of the plan that are not built yet. The dashboard shows the button and
# says plainly why it does nothing. Each entry is removed as its checkpoint lands.
NOT_BUILT: dict[str, str] = {}

LOCKED_PAGE = f"""<!doctype html><meta charset="utf-8"><title>Job App</title>
<body style="font:16px/1.5 Bahnschrift,'Segoe UI',system-ui,sans-serif;margin:12vh auto;max-width:34rem;padding:0 1.5rem;color:#17202b">
<h1 style="font-size:1.4rem">This page is not connected to Job App</h1>
<p>Start Job App with <b>{cmd('run')}</b> in the Job App folder. It opens the dashboard for you.</p>
<p>If Job App is already running, close this window and use the one it opened.</p></body>"""


def load_or_create_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


# --- what the dashboard asks for ----------------------------------------------


class NewJob(BaseModel):
    url: str
    company: str = ""
    title: str = ""
    location: str = ""
    pay: str = ""
    description: str = ""

    @field_validator("url", "company", "title", "location", "pay", mode="before")
    @classmethod
    def _tidy(cls, value: Any) -> Any:
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("description", mode="before")
    @classmethod
    def _keep_paragraphs(cls, value: Any) -> Any:
        # Unlike the one-line fields, a description keeps its line breaks.
        if not isinstance(value, str):
            return value
        lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n").split("\n")]
        text = "\n".join(lines).strip()
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text

    @field_validator("url")
    @classmethod
    def _real_link(cls, value: str) -> str:
        if len(value) > 2000:
            raise ValueError("That link is too long.")
        try:
            normalize_url(value)
        except BadUrl as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("company")
    @classmethod
    def _company(cls, value: str) -> str:
        if len(value) > 200:
            raise ValueError("Keep the company name under 200 characters.")
        return value

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        if len(value) > 200:
            raise ValueError("Keep the job title under 200 characters.")
        return value

    @field_validator("location", "pay")
    @classmethod
    def _short(cls, value: str) -> str:
        if len(value) > 200:
            raise ValueError("Keep this under 200 characters.")
        return value

    @field_validator("description")
    @classmethod
    def _not_a_novel(cls, value: str) -> str:
        if len(value) > 6000:
            raise ValueError("Keep the description under 6,000 characters.")
        return value


def _field_errors(exc: ValidationError) -> dict[str, str]:
    out: dict[str, str] = {}
    for err in exc.errors():
        field = str(err["loc"][0]) if err["loc"] else "form"
        message = err["msg"]
        if message.startswith("Value error, "):
            message = message[len("Value error, "):]
        if err["type"] == "missing":
            message = "This is needed."
        elif err["type"].endswith("_type"):
            message = "This should be text."
        out.setdefault(field, message)
    return out


# --- the app --------------------------------------------------------------------


class Gatekeeper:
    """Refuses requests that do not come from the dashboard on this computer."""

    def __init__(self, app, token: str, allowed_hosts: Optional[set[str]]):
        self.app = app
        self.token = token
        self.allowed_hosts = allowed_hosts

    def _signed_in(self, request: Request) -> bool:
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), self.token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if self.allowed_hosts is not None:
            host = request.headers.get("host", "")
            if host not in self.allowed_hosts:
                # A page on another site pretending this server is its own.
                await PlainTextResponse("Unknown host.", 400)(scope, receive, send)
                return
            origin = request.headers.get("origin")
            if origin and origin not in {f"http://{h}" for h in self.allowed_hosts}:
                await PlainTextResponse("Requests from other sites are not accepted.", 403)(scope, receive, send)
                return
        path = scope["path"]
        if path.startswith("/api/") and path != "/api/health" and not self._signed_in(request):
            response = JSONResponse(
                {"error": "not_connected", "message": f"Start Job App with '{cmd('run')}' and use the window it opens."},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _Static(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        # Always re-check with the server, so an updated dashboard is picked
        # up without clearing the browser cache.
        response.headers["Cache-Control"] = "no-cache"
        return response


def _real_search(paths: Paths) -> dict:
    """The web reader, web search agent and hidden browser used outside tests and sample-data mode."""
    from ..search.agents import real_web_agent
    from ..search.http import SafeWeb
    from ..search.pages import HeadlessReader

    return {
        "web": SafeWeb(),
        "web_agent": real_web_agent(paths, lambda: load_settings(paths.settings)),
        "headless_factory": lambda settings: HeadlessReader(settings.browser.channel),
    }


def _real_agents(paths: Paths):
    """The agent caller and sign-in check used outside tests and sample-data mode."""
    from ..agents.auth import agents_ready
    from ..agents.runner import ask_structured

    async def ask(*, name: str, instructions: str, prompt: str, schema: dict):
        settings = load_settings(paths.settings)
        if name == "question_matcher":
            return await ask_structured(
                paths, settings, name=name, instructions=instructions, prompt=prompt, schema=schema,
                model=settings.models.search, timeout=150, max_turns=3, max_budget_usd=0.3, fallback=True,
            )
        if name == "writing_agent":
            # Drafts are worth the stronger model; one retry is done by the writer itself.
            return await ask_structured(
                paths, settings, name=name, instructions=instructions, prompt=prompt, schema=schema,
                model=settings.models.writer, timeout=300, max_turns=4, max_budget_usd=0.6, fallback=True,
            )
        return await ask_structured(
            paths, settings, name=name, instructions=instructions, prompt=prompt, schema=schema,
            model=settings.models.search,
        )

    def check_sign_in() -> Optional[bool]:
        status = agents_ready(paths.config, load_settings(paths.settings))
        return bool(status["signed_in"]) if status.get("known") else None

    return ask, check_sign_in


def create_app(
    paths: Paths,
    *,
    token: str,
    allowed_hosts: Optional[set[str]] = None,
    ask: Optional[AskFn] = None,
    check_sign_in=None,
    browsers: Optional[Browsers] = None,
    search: Optional[dict] = None,
    apply_agent: Optional[RunLeg] = None,
    run_schedule: bool = True,
) -> Starlette:
    store = TrackerStore(paths.tracker)
    # Opens postings (and, from checkpoint 4, applications) in the browser the
    # settings choose. Tests pass in one that opens nothing real.
    browsers = browsers or Browsers(paths.browser_profile)
    bus = EventBus()
    not_built = dict(NOT_BUILT)
    if ask is None or check_sign_in is None:
        real_ask, real_check = _real_agents(paths)
        ask, check_sign_in = ask or real_ask, check_sign_in or real_check
    profile = ProfileService(paths, bus, ask, check_sign_in)
    # Searching: the web reader, web agent and hidden browser can be swapped
    # for made-up ones (sample-data mode and tests).
    search_deps = search if search is not None else _real_search(paths)
    # Today's agent usage, against the daily cap in Settings.
    usage = Usage(paths.logs / "agents.log", lambda: load_settings(paths.settings))
    searcher = SearchService(paths, store, bus, profile, ask, web=search_deps["web"],
                             web_agent=search_deps.get("web_agent"),
                             headless_factory=search_deps.get("headless_factory"), usage=usage)
    daily = DailySearch(paths, searcher, lambda: load_settings(paths.settings), bus)
    # Trying the form engine on a practice form, in Job App's browser window.
    practice = PracticeService(paths, browsers, bus, profile.load_profile)
    # Filling applications: the application agent can be swapped for the
    # rule-based stand-in (sample-data mode and tests).
    if apply_agent is None:
        from ..apply.agent import real_application_agent

        apply_agent = real_application_agent(paths, lambda: load_settings(paths.settings))
    applications = ApplicationService(paths, store, bus, browsers, profile, apply_agent, writer=Writer(paths, ask),
                                      usage=usage, matcher=QuestionMatcher(paths, ask))
    applications.startup()

    def job_view(job: Job) -> dict:
        data = job.to_json()
        data["group"] = lifecycle.group_of(job)
        data["status_since"] = job.status_since
        actions = {}
        for name, move in (
            ("skip", "skip"), ("prepare", "prepare"), ("continue", "continue"),
            ("applied", "mark_applied"), ("restore", "restore"),
        ):
            # applicable: the move makes sense for this job right now.
            # enabled: it is applicable and the feature behind it exists.
            applicable = lifecycle.can(job, move)
            enabled, reason = applicable, ""
            if name in ("prepare", "continue") and "autofill" in not_built:
                enabled, reason = False, not_built["autofill"]
            if name == "continue" and applications.busy(job.id):
                enabled, reason = False, "Job App is still working on this application."
            actions[name] = {"applicable": applicable, "enabled": enabled, "reason": reason}
        data["actions"] = actions
        data["judging"] = job.id in searcher.judging
        data["busy"] = applications.busy(job.id)
        return data

    def state_view() -> dict:
        jobs = store.all_jobs()
        settings_now = load_settings(paths.settings)
        # The New tab's order: hard requirements, then soft preferences, then resume match, companies spread out.
        ranked = order_new([j for j in jobs if lifecycle.group_of(j) == "new"],
                           resume_match=settings_now.search.resume_match)
        rank = {job.id: index for index, job in enumerate(ranked)}
        counts = {group: 0 for group in ("new", "in_progress", "waiting", "applied", "hidden")}
        for job in jobs:
            counts[lifecycle.group_of(job)] += 1
        notices = []
        if update_available:
            notices.append(f"Job App {update_available['tag']} is available (you have {__version__}). "
                           "To update, run 'git pull' in the Job App folder, then "
                           f"'{cmd('setup')}', and start Job App again.")
        if store.recovered_from_backup:
            notices.append(
                "The tracker file was damaged, so the previous saved version was restored. "
                "The damaged file is kept in the data folder."
            )
        return {
            "app": {
                "name": "Job App",
                "version": __version__,
                "checkpoint": CHECKPOINT,
                "demo": paths.demo,
                "not_built": not_built,
                "data_folder": str(paths.root),
                "commands": {name: cmd(name) for name in ("run", "signin", "check", "setup", "forget")},
            },
            "jobs": [{**job_view(job), "rank": rank.get(job.id)} for job in jobs],
            "counts": counts,
            "settings": settings_now.model_dump(mode="json"),
            "profile": profile.view(),
            "agents": {"signed_in": profile.signed_in},
            "browser": browsers.info(),
            "search": searcher.view(),
            "practice": practice.view(),
            "watchlist": searcher.boards_view(),
            "answers": {"count": len(applications.answers.all())},
            "usage": usage.view(),
            "schedule": daily.view(),
            "notices": notices,
        }

    def problem(status: int, error: str, message: str, **extra) -> JSONResponse:
        return JSONResponse({"error": error, "message": message, **extra}, status_code=status)

    async def read_body(request: Request) -> Any:
        try:
            return json.loads(await request.body() or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # --- pages ----------------------------------------------------------------

    async def index(request: Request):
        offered = request.query_params.get("t")
        if offered is not None:
            if not hmac.compare_digest(offered, token):
                return HTMLResponse(LOCKED_PAGE, status_code=401)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 365)
            return response
        if not hmac.compare_digest(request.cookies.get(COOKIE, ""), token):
            return HTMLResponse(LOCKED_PAGE, status_code=401)
        return FileResponse(paths.ui / "index.html", headers={"Cache-Control": "no-store"})

    async def open_data_folder(request: Request):
        from ..window import open_folder

        if not await asyncio.to_thread(open_folder, paths.root):
            return problem(500, "not_opened", f"Could not open the folder. It is {paths.root}")
        return JSONResponse({"ok": True, "folder": str(paths.root)})

    async def diagnostics(request: Request):
        from ..core.diagnostics import collect
        from ..window import find_chrome, find_edge

        jobs = store.all_jobs()
        counts = {group: 0 for group in ("new", "in_progress", "waiting", "applied", "hidden")}
        for job in jobs:
            counts[lifecycle.group_of(job)] += 1
        text = await asyncio.to_thread(
            collect, settings=load_settings(paths.settings), logs=paths.logs, signed_in=profile.signed_in,
            counts=counts, browsers={"Chrome": find_chrome() is not None, "Edge": find_edge() is not None},
            demo=paths.demo,
        )
        return JSONResponse({"text": text})

    async def health(request: Request):
        return JSONResponse({"ok": True, "app": "job-app", "version": __version__, "demo": paths.demo})

    # --- data -----------------------------------------------------------------

    async def get_state(request: Request):
        try:
            return JSONResponse(state_view())
        except TrackerUnreadable as exc:
            log.error("Tracker unreadable: %s", exc)
            return problem(500, "tracker_unreadable", str(exc))

    async def add_job(request: Request):
        body = await read_body(request)
        if not isinstance(body, dict):
            return problem(422, "invalid", "Fill in the form and try again.", fields={})
        try:
            new = NewJob.model_validate(body)
        except ValidationError as exc:
            return problem(422, "invalid", "Some details need fixing.", fields=_field_errors(exc))
        # Read the posting itself (PLAN.md 7.2, steps 3 to 7 for one link):
        # it fills in what you left empty, and the fit evaluator needs it.
        posting, read_note = None, ""
        if is_restricted(new.url):
            read_note = "This job board does not allow automated reading, so the posting was not read."
        else:
            try:
                posting = await searcher.read_link(new.url)
            except FetchError as exc:
                read_note = ("The posting's page no longer exists." if exc.kind == "not_found"
                             else f"The posting could not be read: {exc}")
        if posting is not None:
            posting.company = searcher.known_company(posting) or posting.company
            team = next((n[6:] for n in posting.notes if n.startswith("Team: ")), "")
        company = new.company or (posting.company if posting else "")
        title = new.title or (posting.title if posting else "")
        if not company or not title:
            fields = {}
            if not company:
                fields["company"] = "Enter the company name."
            if not title:
                fields["title"] = "Enter the job title."
            message = (read_note + " Fill in the details yourself.") if read_note else "Some details need fixing."
            return problem(422, "invalid", message.strip(), fields=fields)
        location = new.location or (posting.location if posting else "")
        job = Job(
            id=job_id_for(new.url),
            company=company[:200],
            title=title[:200],
            location=location[:200],
            url=new.url,
            apply_url=(posting.apply_url if posting and posting.apply_url else new.url),
            source=Source(kind="manual", found_via="added by you"),
            posted_at=(posting.posted_at or None) if posting else None,
            details=Details(pay=new.pay or (posting.pay if posting else ""),
                            type=posting.type if posting else "", remote=posting.remote if posting else "",
                            team=team if posting else ""),
            description_summary=new.description or (clip(posting.description, 600) if posting else ""),
            dedupe_keys=make_dedupe_keys(new.url, company, title, location),
        )
        if posting is not None and posting.url != new.url:
            # The same posting reached under its own address counts as the same job.
            try:
                job.dedupe_keys = list(dict.fromkeys(
                    job.dedupe_keys + make_dedupe_keys(posting.url, company, title, location)))
            except BadUrl:
                pass
        try:
            job = store.add(job)
        except DuplicateJob as dup:
            existing = dup.existing
            label = {
                Status.SKIPPED: "you skipped it", Status.APPLIED: "you applied to it",
                Status.CLOSED: "the posting closed", Status.NEW: "it is under New",
            }.get(existing.status, "it is in progress")
            return problem(
                409, "duplicate",
                f"{existing.title} at {existing.company} is already in your tracker: {label}.",
                job_id=existing.id, group=lifecycle.group_of(existing),
            )
        log.info("Added job %s: %s, %s (read: %s)", job.id, job.company, job.title, posting is not None)
        judging = False
        if posting is not None:
            searcher.remember_link(job.id, posting)
            if searcher.can_judge():
                searcher.start_judging(job.id, posting, keep_summary=bool(new.description))
                judging = True
        bus.publish("jobs_changed", job_id=job.id)
        return JSONResponse({"job": job_view(job), "read": posting is not None, "read_note": read_note,
                             "judging": judging}, status_code=201)

    async def job_action(request: Request):
        job_id = request.path_params["job_id"]
        action = request.path_params["action"]
        moves = {"skip": "skip", "restore": "restore", "applied": "mark_applied",
                 "prepare": "prepare", "continue": "continue"}
        if action not in moves:
            return problem(404, "unknown_action", "There is no such button.")
        if action in ("prepare", "continue") and "autofill" in not_built:
            return problem(409, "not_built", not_built["autofill"])
        try:
            if action == "prepare":
                job = applications.prepare(job_id)
            elif action == "continue":
                job = applications.continue_(job_id)
            elif action == "applied":
                job = await applications.mark_applied(job_id)
            elif action == "skip":
                job = await applications.skip(job_id)
            else:
                job = store.move(job_id, moves[action], by="user")
        except KeyError:
            return problem(404, "unknown_job", "That job is no longer in the tracker.")
        except NotAllowed as exc:
            return problem(409, "not_allowed", str(exc))
        except ApplicationRefused as exc:
            return problem(409, "refused", str(exc))
        log.info("Job %s: %s -> %s", job.id, action, job.status.value)
        bus.publish("jobs_changed", job_id=job.id)
        return JSONResponse({"job": job_view(job)})

    async def current_page(request: Request):
        """The page open in Job App's browser window, for "Add the page open in Job App's browser window"."""
        reader = getattr(browsers, "current_url", None)
        if reader is None:
            return problem(409, "unavailable", "Job App's browser window is not available here.")
        try:
            url = await reader()
        except Exception as exc:  # noqa: BLE001 - the window may have just been closed
            log.warning("Could not read the open page: %s", exc)
            url = None
        if not url:
            return problem(404, "no_page", "No web page is open in Job App's browser window. Open the posting there "
                                           "first.")
        return JSONResponse({"url": url})

    async def application_details(request: Request):
        job_id = request.path_params["job_id"]
        try:
            store.get(job_id)
        except KeyError:
            return problem(404, "unknown_job", "That job is no longer in the tracker.")
        return JSONResponse({"application": applications.application_view(job_id)})

    async def draft_file(request: Request):
        job_id, name = request.path_params["job_id"], request.path_params["name"]
        folder = (paths.applications / job_id / "drafts").resolve()
        path = (folder / name).resolve()
        if path.parent != folder or not path.is_file():
            return problem(404, "not_found", "There is no such draft.")
        return FileResponse(path, filename=path.name)

    # --- your saved answers ---------------------------------------------------------------

    async def list_answers(request: Request):
        return JSONResponse({"answers": [a.model_dump(mode="json") for a in applications.answers.all()]})

    async def edit_answer(request: Request):
        body = await read_body(request)
        if not isinstance(body, dict):
            return problem(422, "invalid", "Nothing to change.")
        answer = body.get("answer")
        if answer is not None and not isinstance(answer, (str, bool, list)):
            return problem(422, "invalid", "An answer is text, yes/no, or a list of choices.")
        if isinstance(answer, str):
            answer = answer.strip()
            if not answer:
                return problem(422, "invalid", "An answer cannot be empty. Delete it instead.")
            if len(answer) > 10000:
                return problem(422, "invalid", "That answer is too long.")
        try:
            item = applications.answers.edit(request.path_params["answer_id"], answer=answer,
                                             sensitivity=body.get("sensitivity"), scope=body.get("scope"))
        except KeyError:
            return problem(404, "unknown_answer", "That answer is no longer saved.")
        bus.publish("answers_changed")
        return JSONResponse({"answer": item.model_dump(mode="json")})

    async def delete_answer(request: Request):
        try:
            applications.answers.delete(request.path_params["answer_id"])
        except KeyError:
            return problem(404, "unknown_answer", "That answer is no longer saved.")
        bus.publish("answers_changed")
        return JSONResponse({"deleted": True})

    async def open_posting(request: Request):
        job_id = request.path_params["job_id"]
        try:
            job = store.get(job_id)
        except KeyError:
            return problem(404, "unknown_job", "That job is no longer in the tracker.")
        body = await read_body(request)
        settings = load_settings(paths.settings)
        mode = settings.browser.mode
        if isinstance(body, dict) and body.get("in") is not None:
            # A one-off choice, such as "open it in my Chrome instead" after
            # Job App's window could not be opened. The setting is unchanged.
            if body["in"] not in MODES:
                return problem(422, "invalid", "There is no such browser choice.")
            mode = body["in"]
        try:
            opened = await browsers.open(job.url, mode=mode, channel=settings.browser.channel)
        except OpenFailed as exc:
            log.warning("Could not open posting %s in %s: %s", job.id, mode, exc)
            return problem(502, "open_failed", str(exc), where=mode)
        except Exception as exc:  # noqa: BLE001 - reported, the app keeps running
            log.exception("Opening posting %s failed", job.id)
            return problem(500, "open_failed", f"The posting could not be opened. ({type(exc).__name__})", where=mode)
        log.info("Opened posting %s in %s%s", job.id, opened.where, "" if opened.loaded else " (did not load)")
        return JSONResponse({
            "opened": {"where": opened.where, "message": opened.message, "loaded": opened.loaded},
            "browser": browsers.info(),
        })

    # --- search -----------------------------------------------------------------------

    async def start_search(request: Request):
        try:
            searcher.start()
        except SearchRefused as exc:
            return problem(409, "refused", str(exc))
        return JSONResponse({"search": searcher.view()}, status_code=202)

    async def stop_search(request: Request):
        searcher.stop()
        return JSONResponse({"search": searcher.view()})

    async def search_report(request: Request):
        return JSONResponse({"report": searcher.latest_report(), "watchlist": searcher.boards_view()})

    # --- practice form ------------------------------------------------------------------

    async def start_practice(request: Request):
        body = await read_body(request)
        style = body.get("style") if isinstance(body, dict) else None
        if style not in PRACTICE_STYLES:
            return problem(422, "invalid", "Choose one of the practice forms.")
        if not hasattr(browsers, "window"):
            return problem(409, "unavailable", "The practice form needs Job App's browser window, which is not available here.")
        try:
            practice.start(style, f"{request.url.scheme}://{request.url.netloc}")
        except RuntimeError as exc:
            return problem(409, "busy", str(exc))
        return JSONResponse({"practice": practice.view()}, status_code=202)

    async def check_practice(request: Request):
        try:
            await practice.check()
        except RuntimeError as exc:
            return problem(409, "not_ready", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Checking the practice form failed")
            return problem(500, "failed", f"The practice form could not be read ({type(exc).__name__}).")
        return JSONResponse({"practice": practice.view()})

    async def put_settings(request: Request):
        body = await read_body(request)
        try:
            settings = update_settings(paths.settings, body)
        except BadSettings as exc:
            return problem(422, "invalid", "That setting could not be saved.", fields=exc.problems)
        log.info("Settings changed: %s", sorted(body))
        bus.publish("settings_changed")
        return JSONResponse({"settings": settings.model_dump(mode="json")})

    # --- profile and requirements ------------------------------------------------

    BUSY = "Still working on the last request. Wait for it to finish."

    def done() -> JSONResponse:
        return JSONResponse({"profile": profile.view(), "agents": {"signed_in": profile.signed_in}})

    async def put_resume(request: Request):
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BYTES:
            return problem(413, "too_large", "That file is larger than 10 MB. Export a smaller copy and try again.")
        data = await request.body()
        try:
            profile.save_resume(request.query_params.get("name", ""), data)
        except BadResume as exc:
            return problem(422, "invalid", str(exc))
        except Busy:
            return problem(409, "busy", BUSY)
        return done()

    async def read_resume(request: Request):
        try:
            profile.start_profile_read()
        except BadResume as exc:
            return problem(422, "invalid", str(exc))
        except Busy:
            return problem(409, "busy", BUSY)
        return done()

    async def put_facts(request: Request):
        body = await read_body(request)
        if not isinstance(body, dict):
            return problem(422, "invalid", "Fill in your details and try again.")
        try:
            profile.save_facts(body)
        except ValidationError as exc:
            log.info("Profile details rejected: %s", exc)
            return problem(422, "invalid", "Some details are not in the expected form. Check them and try again.")
        return done()

    async def put_requirements(request: Request):
        body = await read_body(request)
        if not isinstance(body, dict):
            return problem(422, "invalid", "Write your requirements and try again.")
        try:
            profile.save_requirements(body.get("hard", []), body.get("soft", []))
        except BadRequirements as exc:
            return problem(422, "invalid", str(exc))
        except Busy:
            return problem(409, "busy", BUSY)
        return done()

    async def read_requirements_again(request: Request):
        try:
            profile.start_requirements_read()
        except BadRequirements as exc:
            return problem(422, "invalid", str(exc))
        except Busy:
            return problem(409, "busy", BUSY)
        return done()

    async def settle_line(request: Request):
        body = await read_body(request)
        if not isinstance(body, dict):
            return problem(422, "invalid", "Choose a box for the line.")
        try:
            profile.settle(str(body.get("text", "")), str(body.get("box", "")))
        except BadRequirements as exc:
            return problem(422, "invalid", str(exc))
        except Busy:
            return problem(409, "busy", BUSY)
        return done()

    async def put_reading(request: Request):
        try:
            profile.save_reading(await read_body(request))
        except BadRequirements as exc:
            return problem(422, "invalid", str(exc))
        except Busy:
            return problem(409, "busy", BUSY)
        return done()

    async def recheck_agents(request: Request):
        await profile.refresh_sign_in()
        return done()

    async def events(request: Request):
        queue = bus.subscribe()

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": still here\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/state", get_state),
        Route("/api/jobs", add_job, methods=["POST"]),
        Route("/api/jobs/{job_id}/open", open_posting, methods=["POST"]),
        Route("/api/jobs/{job_id}/application", application_details),
        Route("/api/jobs/{job_id}/drafts/{name}", draft_file),
        Route("/api/browser/current", current_page),
        Route("/api/answers", list_answers),
        Route("/api/answers/{answer_id}", edit_answer, methods=["PUT"]),
        Route("/api/answers/{answer_id}", delete_answer, methods=["DELETE"]),
        Route("/api/jobs/{job_id}/{action}", job_action, methods=["POST"]),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/search", start_search, methods=["POST"]),
        Route("/api/search/stop", stop_search, methods=["POST"]),
        Route("/api/search/report", search_report),
        Route("/api/practice", start_practice, methods=["POST"]),
        Route("/api/practice/check", check_practice, methods=["POST"]),
        Route("/api/profile/resume", put_resume, methods=["PUT"]),
        Route("/api/profile/read", read_resume, methods=["POST"]),
        Route("/api/profile/facts", put_facts, methods=["PUT"]),
        Route("/api/requirements", put_requirements, methods=["PUT"]),
        Route("/api/requirements/read", read_requirements_again, methods=["POST"]),
        Route("/api/requirements/settle", settle_line, methods=["POST"]),
        Route("/api/requirements/reading", put_reading, methods=["PUT"]),
        Route("/api/agents/recheck", recheck_agents, methods=["POST"]),
        Route("/api/data/open", open_data_folder, methods=["POST"]),
        Route("/api/diagnostics", diagnostics),
        Route("/api/events", events),
        Mount("/static", app=_Static(directory=str(paths.ui)), name="static"),
        # The practice forms hold no data, so they need no session cookie:
        # Job App's browser window opens them like any other page.
        Mount("/practice", app=_Static(directory=str(paths.ui / "practice")), name="practice"),
    ]

    update_available: dict = {}

    async def look_for_updates():
        """Once a day while Job App runs, if Settings allows it."""
        from ..core import updates

        while True:
            if not paths.demo and load_settings(paths.settings).check_for_updates:
                found = await asyncio.to_thread(updates.check, paths.data / "update_check.json", paths.program)
                update_available.clear()
                update_available.update(found or {})
                if found:
                    bus.publish("settings_changed")
            await asyncio.sleep(6 * 60 * 60)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Find out in the background whether the agents are signed in, so the
        # dashboard can say so before you try to use one.
        check = asyncio.create_task(profile.refresh_sign_in())
        updates = asyncio.create_task(look_for_updates()) if run_schedule else None
        if run_schedule:
            daily.start()
        yield
        check.cancel()
        if updates is not None:
            updates.cancel()
        await daily.close()
        await searcher.close()
        await applications.close()
        await practice.close()
        await browsers.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.profile = profile
    app.state.store = store
    app.state.bus = bus
    app.state.paths = paths
    app.state.not_built = not_built
    app.state.browsers = browsers
    app.state.search = searcher
    app.state.practice = practice
    app.state.applications = applications
    app.state.usage = usage
    app.state.daily = daily
    return Gatekeeper(app, token, allowed_hosts)  # type: ignore[return-value]
