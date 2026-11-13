"""The search, used the way you would: from the dashboard, in a real (headless) browser."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def ready_page(open_dashboard, running):
    running.ready_profile()
    page = open_dashboard()
    if page.get_attribute(".view-link[data-view=jobs]", "aria-current") != "true":
        page.click(".view-link[data-view=jobs]")
    page.wait_for_selector("#list .empty h2")
    return page


def test_search_from_the_dashboard_and_read_the_report(ready_page, running):
    page = ready_page
    running.web.delay = 0.15  # slow enough to see it running
    page.click("#list .empty .btn-main")
    strip = page.locator("#search-strip")
    strip.locator("text=Searching").wait_for()
    assert page.locator("#btn-search").is_disabled() and page.inner_text("#btn-search") == "Searching"
    assert strip.locator("#btn-stop-search").is_visible()

    page.wait_for_selector("#toasts >> text=Search finished. Listed 4 new jobs.", timeout=30_000)
    assert page.locator(".job").count() == 4
    assert page.inner_text("#btn-search") == "Search for new jobs"
    assert "Search finished" in strip.inner_text() and "Listed 4 new jobs" in strip.inner_text()

    # The listings carry their verdicts.
    robot = page.locator(".job", has_text="Robotics Software Intern, Summer 2027")
    assert "Match 9" in robot.inner_text() and "Found via Kestrel Robotics careers (Greenhouse)" in robot.inner_text()
    assert "resume match" in robot.inner_text()
    robot.locator("[data-action=details]").click()
    assert "Meets: Must be an internship or entry level" in robot.inner_text()

    strip.locator("text=See what was checked").click()
    report = page.locator("#search")
    report.locator(".tally").wait_for()
    tally = report.locator(".tally").inner_text()
    assert "Postings found\n11" in tally and "Listed\n4" in tally
    rejected = report.locator("[data-outcome=rejected]").inner_text()
    assert "Did not meet a hard requirement (1)" in rejected and "Asks for 5+ years of experience." in rejected
    assert "Robotics Intern at Quayside Robotics" in report.locator("[data-outcome=closed]").inner_text()
    report.locator("summary", has_text="Screened out by title or place (3)").click()
    assert "Outside the US (1)" in report.inner_text()
    report.locator("summary", has_text="How the search was planned").click()
    assert "Suggested companies" in report.inner_text() and "Kestrel Robotics, Northwind AI, Tidewater" in report.inner_text()
    # The result strip is not repeated on the screen that shows the report.
    assert page.locator("#search-strip").is_hidden()


def test_search_refused_until_the_profile_is_confirmed(open_dashboard, running):
    page = open_dashboard()
    page.click(".view-link[data-view=jobs]")
    page.click("#btn-search")
    page.wait_for_selector("#toasts >> text=Finish the Profile and requirements screen first")
    assert page.locator("#search-strip").is_hidden()


def test_a_running_search_can_be_stopped(ready_page, running):
    page = ready_page
    running.web.delay = 0.4
    page.click("#btn-search")
    page.locator("#btn-stop-search").click()
    page.wait_for_selector("#toasts >> text=Search stopped.", timeout=30_000)
    assert "Search stopped" in page.inner_text("#search-strip")


def test_watchlist_and_sources_are_saved_from_the_search_screen(ready_page, running):
    page = ready_page
    page.click(".view-link[data-view=search]")
    page.fill("#watchlist", "Kestrel Robotics\nNobody Inc\n\nKestrel Robotics")
    page.click("#btn-save-watchlist")
    page.wait_for_selector("#toasts >> text=Saved 2 companies.")
    saved = json.loads(running.paths.settings.read_text(encoding="utf-8"))
    assert saved["search"]["watchlist"] == ["Kestrel Robotics", "Nobody Inc"]
    assert "Not looked up yet" in page.inner_text("#search")

    page.locator("label.switch-row", has=page.locator("#src-web")).click()
    page.locator("label.switch-row", has=page.locator("#set-strict")).click()
    page.fill("#set-max-new", "10")
    page.press("#set-max-new", "Tab")
    page.wait_for_function("document.querySelector('#set-max-new').value === '10'")
    page.wait_for_timeout(300)
    saved = json.loads(running.paths.settings.read_text(encoding="utf-8"))["search"]
    assert saved["sources"] == {"web": False} and saved["strict_hard_requirements"] is True
    assert saved["max_new_per_search"] == 10

    page.fill("#set-max-new", "0")
    page.press("#set-max-new", "Tab")
    page.wait_for_selector("#toasts >> text=greater than or equal to 1")

    page.click("#btn-search")
    page.wait_for_selector("#search .tally", timeout=30_000)
    page.wait_for_function("document.querySelector('#search').innerText.includes('Greenhouse board found')")
    text = page.inner_text("#search")
    assert "Greenhouse board found. 4 open jobs when last read." in text
    assert "No board found on Greenhouse, Lever or Ashby" in text


def test_adding_a_link_reads_it_and_shows_the_check(ready_page, running):
    page = ready_page
    page.click("#btn-add")
    page.fill("#form-add [name=url]", "https://jobs.ashbyhq.com/tidewater/cccccccc-3333-4333-8333-cccccccccccc")
    page.click("#form-add [type=submit]")
    page.wait_for_selector("#toasts >> text=Added Autonomy Engineering Intern at Tidewater.")
    row = page.locator(".job")
    row.locator("text=Fails your requirements").wait_for()
    assert "Must be an internship or entry level" in row.inner_text()


def test_jobs_that_could_not_be_verified_say_so(ready_page, running):
    page = ready_page

    def unsure(request):
        return {"results": [{"key": p["key"], "hard": [{"requirement": h["text"], "result": "unknown", "evidence": ""}
                                                       for h in request["hard"]],
                             "soft_score": 50, "soft_hits": [], "soft_misses": [], "concerns": [], "summary": "s",
                             "closed": False, "scam_signs": []} for p in request["postings"]]}

    running.agents.override["fit_evaluator"] = unsure
    page.click("#btn-search")
    page.wait_for_selector("#toasts >> text=Search finished.", timeout=30_000)
    note = page.locator(".job .job-note").first
    assert note.inner_text() == "Could not verify Must be an internship or entry level; Must be in the United States"
