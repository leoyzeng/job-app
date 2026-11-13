"""Checkpoint 7: sign-in walls, several applications at once, the daily usage cap, the daily search,
and adding the page open in Job App's browser window."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from app.core.events import EventBus
from app.core.models import Settings
from app.core.usage import Usage, entry_cost
from app.search.schedule import DailySearch
from app.search.service import SearchRefused

from test_apply import add_practice_job, on_page, press, wait_for


def write_usage(paths, cost, day=None):
    paths.logs.mkdir(parents=True, exist_ok=True)
    at = (day or datetime.now().astimezone()).isoformat(timespec="seconds")
    with open(paths.logs / "agents.log", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": at, "agent": "search_agent", "ok": True,
                                 "attempts": [{"structured": True, "cost_usd": cost}]}) + "\n")


# --- the daily usage cap ----------------------------------------------------------------------


def test_usage_adds_up_todays_runs_only(tmp_path):
    log = tmp_path / "agents.log"
    today = datetime.now().astimezone()
    lines = [
        {"at": today.isoformat(), "agent": "application_agent", "cost_usd": 0.4},
        {"at": today.isoformat(), "agent": "fit_evaluator", "attempts": [{"cost_usd": 0.1}, {"cost_usd": 0.05}]},
        {"at": "2020-01-01T09:00:00+00:00", "agent": "search_agent", "cost_usd": 5},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    settings = Settings()
    usage = Usage(log, lambda: settings)
    assert usage.today() == 0.55
    assert usage.problem() == ""
    settings.budget_usd.per_day = 0.5
    assert "daily cap of $0.50" in usage.problem()
    settings.budget_usd.per_day = 0
    assert usage.problem() == "" and usage.view() == {"today_usd": 0.55, "cap_usd": 0, "reached": False}
    assert entry_cost({"attempts": [{"error": "timeout"}]}) == 0.0


def test_the_daily_cap_stops_searches_and_applications(running):
    running.ready_profile()
    running.add()
    job = running.state()["jobs"][0]
    write_usage(running.paths, 11.0)
    state = running.state()
    assert state["usage"]["reached"] is True and state["usage"]["today_usd"] == 11.0
    status, body, _ = running.call("POST", "/api/search")
    assert status == 409 and "daily cap of $5.00" in body["message"]
    status, body, _ = running.call("POST", f"/api/jobs/{job['id']}/prepare")
    assert status == 409 and "daily cap" in body["message"]
    # Raising the cap lets them start again.
    running.call("PUT", "/api/settings", {"budget_usd": {"per_day": 20}})
    assert running.state()["usage"]["reached"] is False
    status, body, _ = running.call("POST", "/api/search")
    assert status == 202


def test_the_top_bar_shows_todays_usage(open_dashboard, running):
    write_usage(running.paths, 1.25)
    page = open_dashboard()
    page.wait_for_selector("#usage:not([hidden])")
    assert page.inner_text("#usage") == "Today: about $1.25 of $5.00"
    write_usage(running.paths, 9.0)
    running.call("PUT", "/api/settings", {"budget_usd": {"per_day": 10}})   # any change makes the page refresh
    page.wait_for_selector("#usage[data-tone=red]")
    assert page.inner_text("#usage") == "Daily cap reached ($10.00)"


def test_limits_and_the_daily_search_are_set_in_settings(open_dashboard, running):
    page = open_dashboard()
    page.click("#btn-settings")
    page.select_option("#set-parallel", "2")
    page.wait_for_selector("#settings-saved >> text=Saved.")
    page.fill("#set-per-day", "4.5")
    page.dispatch_event("#set-per-day", "change")
    page.fill("#set-daily", "07:30")
    page.dispatch_event("#set-daily", "change")
    page.wait_for_function("document.querySelector('#set-daily').getAttribute('aria-invalid') === null")
    deadline = time.time() + 5
    while time.time() < deadline:
        saved = json.loads(running.paths.settings.read_text(encoding="utf-8"))
        if saved["search"]["daily_at"] == "07:30" and saved["budget_usd"]["per_day"] == 4.5:
            break
        time.sleep(0.1)
    assert saved["max_parallel_applications"] == 2
    assert saved["budget_usd"]["per_day"] == 4.5 and saved["search"]["daily_at"] == "07:30"
    for bad in ({"search": {"daily_at": "7:30"}}, {"max_parallel_applications": 9}, {"budget_usd": {"per_day": -1}}):
        assert running.call("PUT", "/api/settings", bad)[0] == 422


# --- the daily search ---------------------------------------------------------------------------


class FakeSearcher:
    def __init__(self, refuse=""):
        self.started = 0
        self.refuse = refuse

    def start(self):
        if self.refuse:
            raise SearchRefused(self.refuse)
        self.started += 1


def run(coro):
    """asyncio.run in a thread of its own: the browser tests keep an event loop in this one."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result(30)


def test_the_daily_search_runs_once_a_day_after_its_time(tmp_path):
    from app.paths import Paths

    paths = Paths(tmp_path)
    paths.ensure()
    settings = Settings()
    now = {"t": datetime(2026, 11, 12, 7, 0).astimezone()}
    searcher = FakeSearcher()
    daily = DailySearch(paths, searcher, lambda: settings, EventBus(), clock=lambda: now["t"])
    assert run(daily.tick()) is None                 # no time set
    settings.search.daily_at = "07:30"
    assert run(daily.tick()) is None                 # not yet
    now["t"] = datetime(2026, 11, 12, 9, 15).astimezone()  # started late: it runs now
    assert run(daily.tick()) == "started" and searcher.started == 1
    assert run(daily.tick()) is None                 # once a day
    assert daily.view()["last_day"] == "2026-11-12"
    now["t"] = datetime(2026, 11, 13, 7, 31).astimezone()
    daily.searcher = FakeSearcher(refuse="Finish the Profile and requirements screen first.")
    assert run(daily.tick()) == "not started: Finish the Profile and requirements screen first."
    assert run(daily.tick()) is None


# --- sign-in walls --------------------------------------------------------------------------------

SIGN_IN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sign in - Careers</title></head>
<body><main id="main"><h1>Sign in to apply</h1>
<form id="signin"><label for="email">Email Address</label><input id="email" type="email">
<label for="password">Password</label><input id="password" type="password">
<button type="button" id="go">Sign In</button></form></main>
<script>
document.getElementById("go").addEventListener("click", () => {
  if (!document.getElementById("password").value) return;
  document.getElementById("main").innerHTML = `<h1>My Information</h1><form>
    <label for="first">First Name</label><input id="first" aria-required="true">
    <label for="last">Last Name</label><input id="last" aria-required="true">
    <label for="mail">Email</label><input id="mail" type="email" aria-required="true">
    <button type="submit">Submit application</button></form>`;
});
</script></body></html>"""


def test_a_sign_in_wall_waits_for_you_and_then_carries_on(running_with_browser):
    running = running_with_browser
    running.ready_profile(resume=True)
    (running.paths.root / "ui" / "practice" / "signin_test.html").write_text(SIGN_IN_PAGE, encoding="utf-8")
    job_id = add_practice_job(running, "signin_test")
    press(running, job_id, "prepare")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_input"
    assert job["status_detail"]["needs"][0]["label"].startswith("Sign in or create your account on this site")

    async def type_in(page):
        assert await page.locator("#email").input_value() == ""      # nothing is filled on a sign-in page
        await page.fill("#email", "sam.rivera@example.com")
        await page.fill("#password", "correct horse battery staple")

    async def sign_in(page):
        await page.click("#go")
        await page.wait_for_selector("#first")
    on_page(running, job_id, type_in)
    time.sleep(1.2)          # the watcher reads the page while you type
    on_page(running, job_id, sign_in)
    press(running, job_id, "continue")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_review", job["status_detail"]

    async def values(page):
        return [await page.locator(s).input_value() for s in ("#first", "#last", "#mail")]
    assert on_page(running, job_id, values) == ["Sam", "Rivera", "sam.rivera@example.com"]
    record = (running.paths.applications / job_id / "fields.json").read_text(encoding="utf-8")
    assert '"label": "Password"' in record and "(entered by you)" in record
    # The password is in no file Job App wrote: not the record, not the readings, not your answers.
    for path in running.paths.data.rglob("*"):
        if path.is_file() and "browser_profile" not in path.parts:
            assert b"correct horse" not in path.read_bytes(), path


# --- several at once, and adding the open page ---------------------------------------------------------


def test_several_applications_can_be_filled_at_once(running_with_browser):
    running = running_with_browser
    running.ready_profile(resume=True)
    running.call("PUT", "/api/settings", {"max_parallel_applications": 2})
    first = add_practice_job(running, "lever")
    second = add_practice_job(running, "ashby", company="Second Co", n=2)
    press(running, first, "prepare")
    body = press(running, second, "prepare")
    assert body["job"]["status_detail"]["message"] == "Starting."
    for job_id in (first, second):
        assert wait_for(running, job_id)["status"] in ("waiting_for_input", "waiting_for_review")
    pages = running.app.app.state.applications.sessions
    assert pages[first].page is not pages[second].page


def test_the_page_open_in_job_apps_window_can_be_added(running_with_browser):
    running = running_with_browser
    assert running.call("GET", "/api/browser/current")[0] == 404
    running.ready_profile()
    status, _, _ = running.call("POST", "/api/practice", {"style": "lever"})
    assert status == 202
    deadline = time.time() + 30
    while running.state()["practice"]["status"] == "working":
        assert time.time() < deadline
        time.sleep(0.1)
    status, body, _ = running.call("GET", "/api/browser/current")
    assert status == 200 and body["url"] == f"{running.base}/practice/lever.html"


def test_without_job_apps_window_there_is_no_open_page(running):
    status, body, _ = running.call("GET", "/api/browser/current")
    assert status == 409 and "not available" in body["message"]


@pytest.mark.parametrize("value", ["", "23:59", "00:00"])
def test_daily_search_times_that_are_accepted(running, value):
    assert running.call("PUT", "/api/settings", {"search": {"daily_at": value}})[0] == 200
