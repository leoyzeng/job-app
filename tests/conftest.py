"""Shared test setup: a real Job App server on a spare port, in a temp folder."""

from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.apply.agent import rule_based_agent  # noqa: E402
from app.browser.manager import OpenFailed, Opened  # noqa: E402
from app.paths import Paths  # noqa: E402
from app.profile.demo import demo_ask  # noqa: E402
from app.search.demo import demo_web_agent, sample_web  # noqa: E402
from app.server.app import COOKIE, create_app  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class StandInAgents:
    """Takes the place of Claude in tests. Records what it was asked, and can be told to fail."""

    def __init__(self):
        self.calls = []
        self.fail_with = None        # an exception to raise instead of answering
        self.signed_in = True
        self.override = {}           # agent name -> data to return instead of the default
        self.cost = None             # cost to report for each run, to test budgets

    async def ask(self, *, name, instructions, prompt, schema):
        self.calls.append({"name": name, "instructions": instructions, "prompt": prompt, "schema": schema})
        if self.fail_with is not None:
            raise self.fail_with
        reply = await demo_ask(name=name, instructions=instructions, prompt=prompt, schema=schema)
        if name in self.override:
            value = self.override[name]
            reply.data = value(json.loads(prompt)) if callable(value) else value
        if self.cost is not None:
            reply.cost_usd = self.cost
        return reply

    def check_sign_in(self):
        return self.signed_in

    async def web_agent(self, *, prompt, instructions, schema, read_page):
        self.calls.append({"name": "search_agent", "instructions": instructions, "prompt": prompt, "schema": schema})
        if self.fail_with is not None:
            raise self.fail_with
        reply = await demo_web_agent(prompt=prompt, instructions=instructions, schema=schema, read_page=read_page)
        if "search_agent" in self.override:
            reply.data = self.override["search_agent"]
        if self.cost is not None:
            reply.cost_usd = self.cost
        return reply


class StandInApplyAgent:
    """Takes the place of the application agent: the rule-based stand-in, with a record of every leg."""

    def __init__(self):
        self.legs = []               # {"reason", "brief", "outcome", "calls"}
        self.override = None         # an async (leg, brief) function to run instead
        self.cost = 0.0

    async def __call__(self, leg, brief):
        entry = {"reason": leg.ctx.reason, "brief": brief}
        self.legs.append(entry)
        run = self.override or rule_based_agent
        result = await run(leg, brief)
        result.cost_usd = result.cost_usd or self.cost
        entry.update(outcome=leg.outcome, calls=list(leg.calls), summary=leg.summary)
        return result


class StandInBrowsers:
    """Takes the place of the real browsers in tests: records what would have opened."""

    def __init__(self):
        self.opened = []             # (url, mode, channel)
        self.fail_in = set()         # modes that raise OpenFailed
        self.chrome_found = True
        self.closed = False

    def info(self):
        return {"window_open": False, "window_browser": None, "chrome_found": self.chrome_found}

    async def open(self, url, *, mode, channel="chrome"):
        if mode in self.fail_in:
            raise OpenFailed("Job App's browser window could not be opened. Run the check command to see why.")
        self.opened.append((url, mode, channel))
        where = "Job App's browser window" if mode == "dedicated" else "your Chrome"
        return Opened(where=mode, message=f"Opened in {where}.")

    async def close(self):
        self.closed = True


@dataclass
class Running:
    base: str
    host: str
    token: str
    paths: Paths
    agents: StandInAgents = None
    browsers: StandInBrowsers = None
    apply_agent: StandInApplyAgent = None
    web: object = None
    app: object = None

    def call(self, method, path, body=None, *, signed_in=True, headers=None, timeout=5):
        """Returns (status, parsed body, response headers)."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if signed_in:
            request.add_header("Cookie", f"{COOKIE}={self.token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as err:
            response = err
        raw = response.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return response.status, parsed, response.headers

    def upload(self, name, data: bytes):
        request = urllib.request.Request(
            self.base + "/api/profile/resume?" + urllib.parse.urlencode({"name": name}), data=data, method="PUT")
        request.add_header("Cookie", f"{COOKIE}={self.token}")
        request.add_header("Content-Type", "application/octet-stream")
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as err:
            response = err
        return response.status, json.loads(response.read().decode("utf-8"))

    def wait_idle(self, timeout=10):
        """Wait until no agent is working, then return the profile part of the state."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            view = self.state()["profile"]
            if not any(view["busy"].values()):
                return view
            time.sleep(0.05)
        raise AssertionError("agents were still busy")

    def add(self, **fields):
        body = {"url": "https://jobs.example.com/acme/1", "company": "Acme Robotics", "title": "Robotics Intern"}
        body.update(fields)
        return self.call("POST", "/api/jobs", body)

    def state(self):
        return self.call("GET", "/api/state")[1]

    def ready_profile(self, *, resume: bool = False):
        """Give the app the confirmed sample profile and requirements, as if the user had finished that screen."""
        from app.profile.demo import seed_demo_profile

        seed_demo_profile(self.paths, with_resume=resume)

    def search(self, timeout=30):
        """Start a search, wait for it to finish, and return the state."""
        status, body, _ = self.call("POST", "/api/search")
        assert status == 202, body
        return self.wait_search(timeout)

    def wait_search(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.state()
            if not state["search"]["running"] and not state["search"]["judging"]:
                return state
            time.sleep(0.05)
        raise AssertionError("the search did not finish")

    def report(self):
        return self.call("GET", "/api/search/report")[1]["report"]


def start_app(tmp_path, *, real_browsers=False):
    """A real Job App server on a spare port, in a temp folder. Returns (Running, stop)."""
    import uvicorn

    root = tmp_path / "job_app"
    shutil.copytree(REPO / "ui", root / "ui")
    shutil.copytree(REPO / "prompts", root / "prompts")
    paths = Paths(root)
    paths.ensure()
    token = "test-token-" + "x" * 32
    port = free_port()
    host = f"127.0.0.1:{port}"
    agents = StandInAgents()
    if real_browsers:
        from app.browser.manager import Browsers
        from app.core.settings import update_settings

        update_settings(paths.settings, {"browser": {"channel": "chromium"}})
        browsers = Browsers(paths.browser_profile, headless=True)
    else:
        browsers = StandInBrowsers()
    web = sample_web()
    apply_agent = StandInApplyAgent()
    app = create_app(paths, token=token, allowed_hosts={host, f"localhost:{port}"},
                     ask=agents.ask, check_sign_in=agents.check_sign_in, browsers=browsers,
                     search={"web": web, "web_agent": agents.web_agent, "headless_factory": None},
                     apply_agent=apply_agent)
    # Sites get less time to read the resume, and the watcher looks more often, so tests run quickly.
    app.app.state.applications.prefill_wait = 0.3
    app.app.state.applications.watch_interval = 0.4
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False,
                       timeout_graceful_shutdown=3)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "test server did not start"
    running = Running(base=f"http://{host}", host=host, token=token, paths=paths, agents=agents, browsers=browsers,
                      web=web, app=app, apply_agent=apply_agent)

    def stop():
        server.should_exit = True
        thread.join(timeout=10)

    return running, stop


@pytest.fixture
def running(tmp_path):
    running, stop = start_app(tmp_path)
    yield running
    stop()


@pytest.fixture
def running_with_browser(tmp_path):
    """Like running, with Job App's browser window really opened (headless)."""
    pytest.importorskip("playwright.async_api")
    running, stop = start_app(tmp_path, real_browsers=True)
    yield running
    stop()


# --- sample files -------------------------------------------------------------------

RESUME_LINES = [
    "Sam Rivera",
    "555-0142   sam.rivera@example.com   github.com/samrivera-example",
    "EDUCATION",
    "Three Rivers University   Expected December 2027",
    "Master of Science in Electrical and Computer Engineering",
    "Lakeshore Institute of Technology   September 2022 - May 2026",
    "Bachelor of Science in Computer Engineering   GPA: 3.8/4.0",
    "EXPERIENCE",
    "Halden Robotics - Robotics Software Intern   May 2025 - August 2025",
    "Wrote a ROS 2 node that cut arm calibration time from 40 minutes to 6.",
    "Lakeshore Autonomy Lab - Undergraduate Research Assistant",
    "Trained a PyTorch model to detect curbs from lidar scans.",
    "SKILLS",
    "Python, C++, ROS 2, PyTorch, Linux, Git, STM32, MATLAB",
]


def make_pdf(lines) -> bytes:
    """A small, valid one-page PDF with the given lines of text."""
    def esc(text):
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream = "BT /F1 11 Tf 50 760 Td 14 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def make_docx(lines) -> bytes:
    import io

    import docx

    document = docx.Document()
    for line in lines:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def resume_pdf() -> bytes:
    return make_pdf(RESUME_LINES)


@pytest.fixture
def resume_docx() -> bytes:
    return make_docx(RESUME_LINES)


# --- a real browser for the dashboard tests ---------------------------------------------


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:  # no test browser on this machine
            pytest.skip(f"No browser available for the dashboard tests: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def open_dashboard(browser, running):
    """Returns a function that opens the dashboard as `jobapp run` would."""
    context = browser.new_context(viewport={"width": 1120, "height": 900})
    errors = []

    def open_page():
        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(f"{running.base}/?t={running.token}")
        page.wait_for_selector("#views .view-link")
        return page

    yield open_page
    context.close()
    # A 401, 409 or 422 the page asked for on purpose shows up as a console
    # "Failed to load resource" line; anything else is a real script error.
    real = [e for e in errors if "Failed to load resource" not in e]
    assert real == [], real
