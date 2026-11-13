"""The dashboard, driven the way you would use it, in a real (headless) browser."""

from __future__ import annotations

import json

import pytest

from app.core.demo import sample_jobs
from app.core.store import TrackerStore

@pytest.fixture
def page(open_dashboard):
    page = open_dashboard()
    # A first visit opens the profile screen; these tests are about the job list.
    page.click(".view-link[data-view=jobs]")
    page.wait_for_selector("#list .empty h2")
    return page


def add_job(page, url="https://jobs.example.com/acme/1", company="Acme Robotics", title="Robotics Intern", **more):
    page.click("#btn-add")
    page.fill("#form-add [name=url]", url)
    page.fill("#form-add [name=company]", company)
    page.fill("#form-add [name=title]", title)
    for name, value in more.items():
        page.fill(f"#form-add [name={name}]", value)
    page.click("#form-add [type=submit]")


def tab_count(page, tab):
    return int(page.inner_text(f".tab[data-tab={tab}] .count"))


def test_first_run_is_empty_and_says_what_to_do(page):
    assert page.inner_text("#list .empty h2") == "No new jobs"
    assert "Search for new jobs, or add one by pasting its link" in page.inner_text("#list .empty p")
    # Search needs a confirmed profile first, and says so.
    page.click("#btn-search")
    page.wait_for_selector("#toasts >> text=Finish the Profile and requirements screen first")


def test_add_skip_undo_and_apply(page, running):
    add_job(page, location="Boston, MA", pay="$40 an hour", description="Motion planning on ROS 2.")
    row = page.locator(".job")
    row.wait_for()
    assert row.locator(".job-title").inner_text() == "Robotics Intern"
    facts = row.locator(".job-facts").inner_text()
    for expected in ("Acme Robotics", "Boston, MA", "$40 an hour", "Added by you", "jobs.example.com"):
        assert expected in facts
    assert row.locator("a").get_attribute("href") == "https://jobs.example.com/acme/1"
    assert [b.inner_text() for b in row.locator(".job-actions button").all()] == [
        "Skip", "Prepare", "Continue", "Mark as applied",
    ]
    assert tab_count(page, "new") == 1

    # Prepare needs a confirmed profile first: it says so and changes nothing.
    row.locator("[data-action=prepare]").click()
    page.wait_for_selector("#toasts >> text=Confirm your profile")
    assert running.state()["jobs"][0]["status"] == "new"
    # Continue does not apply to a job that was never started.
    assert row.locator("[data-action=continue]").is_disabled()

    # Skip removes it from the list and records it ...
    row.locator("[data-action=skip]").click()
    page.wait_for_selector("#list .empty h2")
    assert tab_count(page, "new") == 0 and tab_count(page, "hidden") == 1
    on_disk = json.loads(running.paths.tracker.read_text(encoding="utf-8"))
    assert [j["status"] for j in on_disk["jobs"].values()] == ["skipped"]

    # ... and Undo brings it back.
    page.click("#toasts button")
    page.wait_for_selector(".job")
    assert tab_count(page, "new") == 1 and tab_count(page, "hidden") == 0

    # Mark as applied moves it to Applied.
    page.click(".job [data-action=applied]")
    page.wait_for_selector("#list .empty h2")
    assert tab_count(page, "applied") == 1
    page.click(".tab[data-tab=applied]")
    assert "Applied" in page.inner_text(".job .job-status")


def test_add_form_explains_problems(page):
    add_job(page, url="acme.com/jobs", company="", title="")
    page.wait_for_selector("#form-add [aria-invalid=true]")
    assert page.inner_text(".field-error[data-for=url]") == "Enter a full link that starts with https://"
    assert page.locator("#dlg-add").evaluate("d => d.open")  # the form stays open with what was typed
    # A link whose page cannot be read: the form asks for the details.
    page.fill("#form-add [name=url]", "https://jobs.example.com/gone/1")
    page.click("#form-add [type=submit]")
    page.wait_for_selector(".field-error[data-for=company]:has-text('Enter the company name.')")
    assert page.inner_text(".field-error[data-for=title]") == "Enter the job title."
    assert "no longer exists" in page.inner_text("#add-error")
    page.click("#dlg-add [data-close]")

    add_job(page)
    page.wait_for_selector(".job")
    add_job(page, url="https://www.jobs.example.com/acme/1?utm_source=x")
    page.wait_for_selector("#add-error:not([hidden])")
    assert "already in your tracker" in page.inner_text("#add-error")
    assert page.locator(".job").count() == 1


def test_settings_toggles_are_saved(page, running):
    page.click("#btn-settings")
    pause, write = page.locator("#set-pause"), page.locator("#set-write")
    assert not pause.is_checked() and write.is_checked()
    assert "Your own Claude account" in page.inner_text("#about")
    page.locator("label.switch-row", has=pause).click()
    page.wait_for_function("document.querySelector('#settings-saved').textContent === 'Saved.'")
    saved = json.loads(running.paths.settings.read_text(encoding="utf-8"))
    assert saved["pause_after_each_page"] is True and saved["write_long_answers"] is True
    page.click("#dlg-settings [data-close]")
    page.reload()
    page.click("#btn-settings")
    assert page.locator("#set-pause").is_checked()


def test_page_updates_by_itself_when_the_tracker_changes(page, running):
    # A change made outside this page (as the agent will make later).
    running.add(company="Outside Change Co", title="Controls Intern", url="https://jobs.example.com/outside/9")
    page.wait_for_selector(".job")
    assert "Outside Change Co" in page.inner_text(".job")


def test_every_status_is_shown_in_plain_words(page, running):
    TrackerStore(running.paths.tracker).replace_all(sample_jobs())
    page.reload()
    page.wait_for_selector(".job")
    assert {tab: tab_count(page, tab) for tab in ("new", "in_progress", "waiting", "applied", "hidden")} == {
        "new": 3, "in_progress": 2, "waiting": 4, "applied": 1, "hidden": 2,
    }
    # New jobs are ordered by how well they match.
    assert page.locator(".job .job-title").first.inner_text().startswith("Robotics Software Intern")

    page.click(".tab[data-tab=waiting]")
    text = page.inner_text("#list")
    for expected in (
        "Waiting for input", "Salary expectation", "Will you now or in the future require sponsorship?",
        "Waiting for page review", "Waiting for review", "Problem",
    ):
        assert expected in text, expected
    # The highlighter is reserved for "this needs you".
    marked = [el.inner_text() for el in page.locator(".job-status .hl").all()]
    assert sorted(marked) == ["Waiting for input", "Waiting for page review", "Waiting for review"]

    page.click(".tab[data-tab=in_progress]")
    assert "Autofilling" in page.inner_text("#list") and "Queued" in page.inner_text("#list")

    page.click(".tab[data-tab=hidden]")
    assert [b.inner_text() for b in page.locator(".job").first.locator(".job-actions button").all()] == [
        "Move back to New", "Mark as applied",
    ]

    page.click(".tab[data-tab=new]")
    page.locator(".job").first.locator("[data-action=details]").click()
    extra = page.inner_text(".job-extra")
    assert "Your hard requirements" in extra and "Meets" in extra and "History" in extra


# --- where postings open -------------------------------------------------------------


def test_browser_choice_is_saved_from_settings(page, running):
    page.click("#btn-settings")
    dedicated = page.locator("#set-browser input[value=dedicated]")
    own = page.locator("#set-browser input[value=own_chrome]")
    assert dedicated.is_checked() and not own.is_checked()
    assert "Applications open in" not in page.inner_text("#about")  # the choice itself replaced this row
    page.locator("label.choice-row", has=page.locator("input[value=own_chrome]")).click()
    page.wait_for_function(
        "document.querySelector('#settings-saved').textContent === 'Saved. Postings now open in your Chrome.'")
    saved = json.loads(running.paths.settings.read_text(encoding="utf-8"))
    assert saved["browser"]["mode"] == "own_chrome"
    page.click("#dlg-settings [data-close]")
    page.reload()
    page.click("#btn-settings")
    assert page.locator("#set-browser input[value=own_chrome]").is_checked()


def test_settings_say_when_chrome_is_missing(page, running):
    running.browsers.chrome_found = False
    page.reload()
    page.click("#btn-settings")
    assert "Chrome was not found" in page.inner_text("#own-chrome-help")


def test_clicking_the_posting_link_opens_it_where_settings_say(page, running):
    add_job(page)
    link = page.locator(".job a[data-action=open]")
    link.wait_for()
    assert link.get_attribute("aria-label") == "Open the posting on jobs.example.com in Job App's browser window"
    pages_before = len(page.context.pages)
    link.click()
    page.wait_for_selector("#toasts >> text=Opened in Job App's browser window.")
    # The dashboard asked Job App to open it; it did not open a tab of its own.
    assert running.browsers.opened == [("https://jobs.example.com/acme/1", "dedicated", "chrome")]
    assert len(page.context.pages) == pages_before

    running.call("PUT", "/api/settings", {"browser": {"mode": "own_chrome"}})
    page.reload()
    page.click(".view-link[data-view=jobs]")
    page.locator(".job a[data-action=open]").click()
    page.wait_for_selector("#toasts >> text=Opened in your Chrome.")
    assert running.browsers.opened[-1][1] == "own_chrome"


def test_when_job_apps_window_fails_your_chrome_is_offered(page, running):
    add_job(page)
    running.browsers.fail_in = {"dedicated"}
    page.locator(".job a[data-action=open]").click()
    offer = page.locator("#toasts button", has_text="Open in your Chrome")
    offer.wait_for()
    assert "could not be opened" in page.inner_text("#toasts")
    offer.click()
    page.wait_for_selector("#toasts >> text=Opened in your Chrome.")
    assert running.browsers.opened == [("https://jobs.example.com/acme/1", "own_chrome", "chrome")]
    # The one-off choice did not change the setting.
    assert running.state()["settings"]["browser"]["mode"] == "dedicated"
