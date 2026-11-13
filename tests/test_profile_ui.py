"""The Profile and requirements screen, driven in a real (headless) browser with stand-in agents."""

from __future__ import annotations

from app.commands import cmd  # noqa: E402

import json

from conftest import make_pdf, RESUME_LINES


def test_first_visit_opens_the_profile_screen(open_dashboard):
    page = open_dashboard()
    page.wait_for_selector("#profile .step")
    assert page.get_attribute(".view-link[data-view=profile]", "aria-current") == "true"
    assert "to do" in page.inner_text(".view-link[data-view=profile]")
    assert [el.inner_text().split("\n")[-1] for el in page.locator(".step-title").all()] == [
        "Your resume", "About you", "What you are looking for",
    ]
    assert "No resume yet" in page.inner_text(".step[data-step='1']")
    assert page.locator("#btn-search").is_hidden() and page.locator("#btn-add").is_hidden()
    # Once on the jobs screen, the choice is remembered.
    page.click(".view-link[data-view=jobs]")
    page.wait_for_selector("#list .empty h2")
    page.reload()
    page.wait_for_selector("#list .empty h2")


def test_upload_review_and_confirm(open_dashboard, running, tmp_path):
    page = open_dashboard()
    resume = tmp_path / "Sam Rivera.pdf"
    resume.write_bytes(make_pdf(RESUME_LINES))
    page.set_input_files("#resume-file", str(resume))

    # The details arrive unconfirmed, with the things to check marked.
    page.wait_for_selector("[data-bind='name.first']")
    assert page.input_value("[data-bind='name.first']") == "Sam"
    assert page.input_value("[data-bind='education.0.school']") == "Three Rivers University"
    assert page.is_checked("[data-bind='education.0.end_is_expected']")
    assert page.input_value("[data-bind='skills']") == "Python, C++, ROS 2, PyTorch, Linux, Git, STM32, MATLAB"
    about = page.inner_text(".step[data-step='2']")
    assert "Waiting for you to confirm" in about and "Is 555-0142 still the best number" in about
    assert "resume.pdf" in page.inner_text(".step[data-step='1']")

    # Fix a field, remove a job, add a project, then confirm.
    page.fill("[data-bind='contact.phone']", "555-0199")
    page.locator("[data-entry='experience-1'] .entry-remove").click()
    page.click("[data-add=projects]")
    page.fill("[data-bind='projects.1.name']", "Balancing robot")
    page.fill("[data-bind='projects.1.tools']", "STM32, C")
    assert page.input_value("[data-bind='contact.phone']") == "555-0199"   # typing survived the redraws
    page.click("#btn-save-facts")
    page.wait_for_function("document.querySelector(\".step[data-step='2']\").innerText.includes('Confirmed')")

    saved = json.loads((running.paths.profile / "profile.json").read_text(encoding="utf-8"))
    assert saved["confirmed"] is True and saved["to_confirm"] == []
    assert saved["facts"]["contact"]["phone"] == "555-0199"
    assert [e["employer"] for e in saved["facts"]["experience"]] == ["Halden Robotics"]
    assert saved["facts"]["projects"][1] == {"name": "Balancing robot", "summary": "", "tools": ["STM32", "C"]}


def test_typing_is_not_lost_when_the_page_updates_itself(open_dashboard, running):
    page = open_dashboard()
    page.click("#btn-manual")
    page.fill("[data-bind='name.first']", "Sam")
    page.fill("[data-bind='req.hard']", "Must be an intern")
    page.focus("[data-bind='req.hard']")
    # Something else changes in the app while you are typing.
    running.add(company="Outside Change Co")
    running.call("PUT", "/api/settings", {"pause_after_each_page": True})
    page.wait_for_timeout(400)
    assert page.input_value("[data-bind='name.first']") == "Sam"
    assert page.input_value("[data-bind='req.hard']") == "Must be an intern"
    assert page.evaluate("document.activeElement.dataset.bind") == "req.hard"


def test_requirements_questions_and_confirmation(open_dashboard, running):
    page = open_dashboard()
    page.fill("[data-bind='req.hard']", "Must be an internship or entry level\nPrefer to be near Boston")
    page.fill("[data-bind='req.soft']", "Prefer the job to be related to robotics\nPrefer pay of at least $35 an hour")
    page.click("#btn-save-req")

    page.wait_for_selector(".reading")
    reading = page.inner_text(".reading")
    assert "Here is how I read that" in reading and "Waiting for you to confirm" in reading
    assert "A job is listed only if" in reading and "Checked by:" in reading
    text = (running.paths.profile / "requirements.md").read_text(encoding="utf-8")
    assert "- Prefer to be near Boston\n\n## Soft preferences" in text

    # One line is in the hard box but reads like a wish. Answer: only rank lower.
    question = page.locator(".question")
    assert "Prefer to be near Boston" in question.inner_text()
    question.locator("[data-box=soft]").click()
    # The line moves at once; the reading is hidden while it is read again, then comes back.
    page.wait_for_function("document.querySelector(\"[data-bind='req.soft']\").value.includes('Boston')")
    assert page.input_value("[data-bind='req.hard']") == "Must be an internship or entry level"
    page.wait_for_function(
        "document.querySelector('.reading') && !document.querySelector(\".step[data-step='3'] .live\")")
    assert page.locator(".question").count() == 0
    assert "Prefer to be near Boston" in page.inner_text(".reading")

    # Correct the search terms and a weight, then confirm.
    page.fill("[data-bind='reading.titles']", "Robotics Intern, Controls Intern")
    page.select_option("[data-bind='weight.0']", "3")
    page.click("#btn-confirm-reading")
    page.wait_for_function("document.querySelector('.reading').innerText.includes('Confirmed')")
    saved = json.loads((running.paths.profile / "search_profile.json").read_text(encoding="utf-8"))
    assert saved["confirmed"] is True
    assert saved["reading"]["titles"] == ["Robotics Intern", "Controls Intern"]
    assert saved["reading"]["soft"][0] == {"text": "Prefer the job to be related to robotics", "weight": 3}
    assert [h["text"] for h in saved["reading"]["hard"]] == ["Must be an internship or entry level"]


def test_not_signed_in_is_explained_with_the_fix(open_dashboard, running, tmp_path):
    from app.agents.runner import AgentError

    running.agents.signed_in = False
    running.agents.fail_with = AgentError("not_signed_in")
    running.call("POST", "/api/agents/recheck")
    page = open_dashboard()
    page.wait_for_selector("#signin-notice")
    assert f"Run '{cmd('signin')}'" in page.inner_text("#signin-notice")

    resume = tmp_path / "r.pdf"
    resume.write_bytes(make_pdf(RESUME_LINES))
    page.set_input_files("#resume-file", str(resume))
    page.wait_for_function("document.querySelector(\".step[data-step='1']\").innerText.includes('Problem')")
    assert "not signed in to Claude" in page.inner_text(".step[data-step='1']")

    # After signing in, "Check again" clears the notice and reading works.
    running.agents.signed_in = True
    running.agents.fail_with = None
    page.click("#btn-recheck")
    page.wait_for_function("!document.querySelector('#signin-notice')")
    page.click("#btn-read-resume")
    page.wait_for_selector("[data-bind='name.first']")


def test_everything_confirmed_shows_all_set(open_dashboard, running):
    from app.profile.demo import seed_demo_profile

    seed_demo_profile(running.paths)
    page = open_dashboard()
    # With nothing left to do, the first screen is the job list.
    page.wait_for_selector("#list .empty h2")
    assert page.locator(".view-link .todo").count() == 0
    page.click(".view-link[data-view=profile]")
    page.wait_for_selector("#profile .step")
    assert "All set" in page.inner_text("#profile")
    assert page.input_value("[data-bind='name.full']") == "Sam Rivera"
    assert "Confirmed" in page.inner_text(".reading")
