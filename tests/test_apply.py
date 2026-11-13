"""Prepare, Continue and Mark as applied, end to end: the real server, Job App's browser window (headless),
the practice forms, and the rule-based stand-in for the application agent."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.core.models import Status, StatusDetail
from app.forms import fill, read_page

WAITING = ("waiting_for_input", "waiting_for_page_review", "waiting_for_review", "error", "closed", "applied")


def add_practice_job(running, style, *, company="Practice Robotics", n=1):
    status, body, _ = running.add(url=f"https://jobs.example.com/{style}/{n}", company=company,
                                  title="Robotics Software Intern")
    assert status == 201, body
    store = running.app.app.state.store
    job = store.get(body["job"]["id"])
    job.apply_url = f"{running.base}/practice/{style}.html"
    store.update(job)
    return job.id


def job_of(running, job_id):
    return next(j for j in running.state()["jobs"] if j["id"] == job_id)


def wait_for(running, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = job_of(running, job_id)
        if job["status"] in WAITING and not job["busy"]:
            return job
        time.sleep(0.15)
    raise AssertionError(f"still {job_of(running, job_id)['status']}")


def press(running, job_id, action, expect=200):
    status, body, _ = running.call("POST", f"/api/jobs/{job_id}/{action}", timeout=60)
    assert status == expect, body
    return body


def service(running):
    return running.app.app.state.applications


def on_page(running, job_id, work, timeout=60):
    """Run ``work(page)`` on the application's tab, in the server's event loop: you, typing in the window."""
    svc = service(running)
    page = svc.sessions[job_id].page
    return asyncio.run_coroutine_threadsafe(work(page), svc.loop).result(timeout)


def type_as_user(running, job_id, values):
    async def work(page):
        reading = await read_page(page)
        keys = {f.label: f.key for f in reading.fields}
        results = await fill(page, {keys[label]: value for label, value in values.items()})
        assert all(r.status == "ok" for r in results), [r.model_dump() for r in results]
    on_page(running, job_id, work)


def details(running, job_id):
    status, body, _ = running.call("GET", f"/api/jobs/{job_id}/application")
    assert status == 200
    return body["application"]


def sources(app):
    return {f["label"]: f["source"] for f in app["fields"]}


AUTH = "Are you legally authorized to work in the United States?"
SPONSOR = "Will you now or in the future require sponsorship for employment visa status (e.g. H-1B)?"
WHY = "Why are you interested in Practice Robotics?"


@pytest.fixture
def ready(running_with_browser):
    running_with_browser.ready_profile(resume=True)
    return running_with_browser


def test_prepare_fills_what_is_certain_and_stops_for_what_is_yours(ready):
    running = ready
    job_id = add_practice_job(running, "greenhouse")
    body = press(running, job_id, "prepare")
    assert body["job"]["status"] in ("queued", "filling")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_input", job["status_detail"]
    needs = [n["label"] for n in job["status_detail"]["needs"]]
    assert needs == [AUTH, SPONSOR]
    assert "I need you for" in job["status_detail"]["message"]

    app = details(running, job_id)
    got = sources(app)
    assert got["First Name"] == "profile" and got["Email"] == "profile" and got["Phone"] == "profile"
    assert got["Resume/CV"] == "resume"
    assert got["School"] == "resume"
    assert got[WHY] == "ai_draft" and got["Cover Letter"] == "ai_draft"
    assert AUTH not in got and SPONSOR not in got
    assert any(n.startswith("AI-drafted: " + WHY) for n in app["state"]["review_notes"])
    assert any("privacy notice" in n for n in app["state"]["review_notes"])
    drafts = {d["name"] for d in app["drafts"]}
    assert "cover_letter.docx" in drafts and any(name.startswith("why_are_you_interested") for name in drafts)
    status, _, headers = running.call("GET", f"/api/jobs/{job_id}/drafts/cover_letter.docx")
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    assert running.call("GET", f"/api/jobs/{job_id}/drafts/..%2Fstate.json")[0] == 404

    # The drafted answer is in the form and states only resume facts.
    async def why_text(page):
        return await page.locator("#question_31006").input_value()
    text = on_page(running, job_id, why_text)
    assert "Halden Robotics" in text and "40 minutes to 6" in text

    # Nothing was submitted, and the agent was told why this leg started.
    assert not on_page(running, job_id, lambda page: page.locator("#practice-sent").count())
    leg = running.apply_agent.legs[0]
    assert leg["reason"] == "prepare" and leg["outcome"] == "needs_input"
    assert leg["brief"]["job"]["company"] == "Practice Robotics" and leg["brief"]["settings"]["write_long_answers"]
    record = json.loads((running.paths.applications / job_id / "fields.json").read_text(encoding="utf-8"))
    assert all(f["value"] != "" for f in record["fields"] if f["source"] == "profile")


def test_continue_learns_your_answers_and_the_next_application_reuses_them(ready):
    running = ready
    first = add_practice_job(running, "greenhouse")
    press(running, first, "prepare")
    assert wait_for(running, first)["status"] == "waiting_for_input"

    # You answer the two sensitive questions and rewrite the drafted answer in your own words.
    mine = ("I spent a summer at Halden Robotics writing ROS 2 code for a robot arm, and Practice Robotics is "
            "building the kind of mobile manipulators I want to keep working on.")
    type_as_user(running, first, {AUTH: "Yes", SPONSOR: "No", WHY: mine})
    body = press(running, first, "continue")
    assert body["job"]["status"] == "filling"
    job = wait_for(running, first)
    assert job["status"] == "waiting_for_review", job["status_detail"]
    assert job["status_detail"]["needs"] == []

    leg = running.apply_agent.legs[-1]
    assert leg["reason"] == "continue_after_input"
    changes = {c["label"]: c for c in leg["brief"]["user_changes"]}
    assert changes[AUTH]["value"] == "(answered)" and changes[AUTH]["learned"] == "created"
    assert changes[WHY]["value"].startswith("I spent a summer")

    status, body, _ = running.call("GET", "/api/answers")
    saved = {a["question"]: a for a in body["answers"]}
    assert saved[AUTH]["answer"] == "Yes" and saved[AUTH]["sensitivity"] == "sensitive"
    assert saved[SPONSOR]["answer"] == "No"
    assert saved[WHY]["scope"] == "job_specific" and saved[WHY]["source"] == "user_correction"
    assert running.state()["answers"]["count"] == 3
    app = details(running, first)
    assert sources(app)[AUTH] == "user"
    assert next(f for f in app["fields"] if f["label"] == WHY)["user_changed"] is True
    # Your rewrite of the draft is kept beside it, and is now a sample of your writing.
    yours = next(d for d in app["drafts"] if d["name"].endswith(".your_version.md"))
    assert yours["text"].endswith(mine + "\n")
    assert any(sample["answer"] == mine for sample in service(running).answers.writing_samples())

    # Mark as applied keeps the record and says what was saved.
    body = press(running, first, "applied")
    assert body["job"]["status"] == "applied"

    # The next application with the same questions needs nothing from you.
    second = add_practice_job(running, "greenhouse", company="Second Robotics", n=2)
    press(running, second, "prepare")
    job = wait_for(running, second)
    assert job["status"] == "waiting_for_review", job["status_detail"]
    app = details(running, second)
    fields = {f["label"]: f for f in app["fields"]}
    assert fields[AUTH]["source"] == "saved_answer" and fields[AUTH]["match"] == "exact"
    assert fields[SPONSOR]["source"] == "saved_answer"
    # The answer written for the first company is not reused; the question is drafted afresh.
    assert fields[WHY]["source"] == "ai_draft"
    status, body, _ = running.call("GET", "/api/answers")
    assert {a["question"]: a["times_used"] for a in body["answers"]}[AUTH] == 1


def test_pause_after_each_page_walks_a_workday_form_one_page_at_a_time(ready):
    running = ready
    running.call("PUT", "/api/settings", {"pause_after_each_page": True})
    job_id = add_practice_job(running, "workday")
    press(running, job_id, "prepare")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_input"
    assert [n["label"] for n in job["status_detail"]["needs"]] == [
        "How Did You Hear About Us?", "Have you previously been employed by Practice Robotics?", "Phone Device Type"]

    type_as_user(running, job_id, {"How Did You Hear About Us?": "Company Website",
                                   "Have you previously been employed by Practice Robotics?": "No",
                                   "Phone Device Type": "Mobile"})
    press(running, job_id, "continue")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_page_review", job["status_detail"]
    assert "Check this page" in job["status_detail"]["message"]

    # You approve page 1; the agent moves on and stops at the sensitive questions on page 2.
    press(running, job_id, "continue")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_input"
    assert [n["label"] for n in job["status_detail"]["needs"]] == [
        "Are you legally authorized to work in the country in which this job is located?",
        "Will you now or in the future require sponsorship for employment visa status?"]
    assert job["application"]["page"] == 2
    type_as_user(running, job_id, {
        "Are you legally authorized to work in the country in which this job is located?": "Yes",
        "Will you now or in the future require sponsorship for employment visa status?": "No"})
    press(running, job_id, "continue")
    assert wait_for(running, job_id)["status"] == "waiting_for_page_review"
    press(running, job_id, "continue")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_review", job["status_detail"]
    assert job["application"]["page"] == 3

    # Review: nothing changed; Continue only checks the form again.
    legs_before = len(running.apply_agent.legs)
    press(running, job_id, "continue")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_review" and "Your changes are saved" in job["status_detail"]["message"]
    assert len(running.apply_agent.legs) == legs_before

    # You submit it yourself; the listing notices and offers Mark as applied.
    async def submit(page):
        await page.click("#next")
    on_page(running, job_id, submit)
    deadline = time.time() + 15
    while not job_of(running, job_id)["application"].get("looks_submitted"):
        assert time.time() < deadline, "the confirmation was not noticed"
        time.sleep(0.2)
    press(running, job_id, "applied")
    assert job_of(running, job_id)["status"] == "applied"


def test_drafts_that_fail_the_fact_check_are_not_used(ready):
    running = ready
    running.agents.override["writing_agent"] = {
        "status": "ok", "text": "At Halden Robotics I led a team of 12 and cut costs by 35%.",
        "facts_used": ["Led a team of 12 at Halden Robotics"], "notes": "", "reason": ""}
    job_id = add_practice_job(running, "greenhouse")
    press(running, job_id, "prepare")
    job = wait_for(running, job_id)
    needs = {n["label"]: n["reason"] for n in job["status_detail"]["needs"]}
    assert set(needs) == {AUTH, SPONSOR, WHY, "Cover Letter"}
    assert "fact check" in needs[WHY] and "12, 35" in needs[WHY]
    assert needs["Cover Letter"].startswith("Optional. ")
    writes = [c for c in running.agents.calls if c["name"] == "writing_agent"]
    # Each draft is sent back once with the reasons, then given up.
    assert len(writes) == 4 and "Your previous draft could not be used" in writes[1]["prompt"]
    assert "Rule 5" not in writes[0]["instructions"] and "Only real facts" in writes[0]["instructions"]
    async def why_text(page):
        return await page.locator("#question_31006").input_value()
    assert on_page(running, job_id, why_text) == ""
    assert "Cover Letter" not in sources(details(running, job_id))
    assert job["status"] == "waiting_for_input"

    # A question the resume cannot answer truthfully is left for you, with the writer's reason.
    running.agents.override["writing_agent"] = {"status": "cannot_answer", "text": "", "facts_used": [], "notes": "",
                                                "reason": "Nothing in your resume shows this."}
    other = add_practice_job(running, "greenhouse", company="Other Co", n=2)
    press(running, other, "prepare")
    needs = {n["label"]: n["reason"] for n in wait_for(running, other)["status_detail"]["needs"]}
    assert needs[WHY] == "Nothing in your resume shows this."


def test_skip_closes_the_tab_and_a_failed_agent_keeps_what_was_filled(ready):
    running = ready
    from app.apply.agent import LegRun
    from app.agents.runner import AgentError

    async def broken(leg, brief):
        await leg.get_page()
        return LegRun(error=AgentError("limit"))

    running.apply_agent.override = broken
    job_id = add_practice_job(running, "lever")
    press(running, job_id, "prepare")
    job = wait_for(running, job_id)
    assert job["status"] == "error"
    assert "usage limit" in job["status_detail"]["message"] and "What was filled is kept" in job["status_detail"]["message"]
    assert sources(details(running, job_id))["Full name"] == "profile"

    # Prepare again picks up where it stopped, in the same tab.
    running.apply_agent.override = None
    press(running, job_id, "prepare")
    job = wait_for(running, job_id)
    assert job["status"] in ("waiting_for_input", "waiting_for_review")
    page = service(running).sessions[job_id].page
    press(running, job_id, "skip")
    assert job_of(running, job_id)["status"] == "skipped"
    assert job_id not in service(running).sessions
    assert page.is_closed()


def test_prepare_refuses_without_a_profile_and_queues_beyond_the_limit(running_with_browser):
    running = running_with_browser
    job_id = add_practice_job(running, "ashby")
    status, body, _ = running.call("POST", f"/api/jobs/{job_id}/prepare")
    assert status == 409 and "Confirm your profile" in body["message"]
    running.ready_profile(resume=True)
    other = add_practice_job(running, "lever", company="Other Co", n=2)
    press(running, job_id, "prepare")
    body = press(running, other, "prepare")
    assert body["job"]["status"] == "queued"
    assert "Waiting for another application" in body["job"]["status_detail"]["message"]
    wait_for(running, job_id)
    wait_for(running, other)
    assert [leg["brief"]["job"]["id"] for leg in running.apply_agent.legs] == [job_id, other]


def test_a_restart_turns_unfinished_applications_into_problems(tmp_path):
    from app.apply.agent import rule_based_agent
    from app.apply.service import RESTARTED, ApplicationService
    from app.core.events import EventBus
    from app.core.models import Job
    from app.core.store import TrackerStore
    from app.paths import Paths

    paths = Paths(tmp_path)
    paths.ensure()
    store = TrackerStore(paths.tracker)
    store.add(Job(id="j_a", company="A", title="T", url="https://a.example.com/1", status=Status.FILLING))
    store.add(Job(id="j_b", company="B", title="T", url="https://b.example.com/1", status=Status.WAITING_FOR_INPUT,
                  status_detail=StatusDetail(message="keep me")))
    ApplicationService(paths, store, EventBus(), None, None, rule_based_agent).startup()
    assert store.get("j_a").status == Status.ERROR and store.get("j_a").status_detail.message == RESTARTED
    assert store.get("j_b").status == Status.WAITING_FOR_INPUT


def test_answers_can_be_corrected_and_deleted_from_the_dashboard(running):
    from app.apply.answers import AnswerStore

    store = AnswerStore(running.paths.answers)
    answer, _ = store.learn("Preferred pronouns", "text", [], "they/them", source="user", job_id="j_1",
                            sensitive=False)
    service(running).answers._fresh()
    status, body, _ = running.call("GET", "/api/answers")
    assert [a["answer"] for a in body["answers"]] == ["they/them"]
    status, body, _ = running.call("PUT", f"/api/answers/{answer.id}", {"answer": "she/her", "sensitivity": "sensitive"})
    assert status == 200 and body["answer"]["answer"] == "she/her" and body["answer"]["sensitivity"] == "sensitive"
    assert running.call("PUT", f"/api/answers/{answer.id}", {"answer": "  "})[0] == 422
    assert running.call("PUT", f"/api/answers/{answer.id}", {"answer": {"x": 1}})[0] == 422
    assert running.call("DELETE", f"/api/answers/{answer.id}")[0] == 200
    assert running.call("DELETE", f"/api/answers/{answer.id}")[0] == 404
    assert running.call("GET", "/api/answers")[1]["answers"] == []


# --- 0.8.0 -------------------------------------------------------------------------------------------

PAGE_ONE = {"How Did You Hear About Us?": ("select", "Company Website"),
            "Have you previously been employed by Practice Robotics?": ("radio", "No"),
            "Phone Device Type": ("select", "Mobile")}


def seed_answers(running, extra=None):
    answers = service(running).answers
    for question, (kind, value) in {**PAGE_ONE, **(extra or {})}.items():
        answers.learn(question, kind, [], value, source="user", job_id="j_seed",
                      sensitive=question in (AUTH, SPONSOR))


def test_reworded_sensitive_questions_get_your_answers(ready):
    running = ready
    # You answered the Greenhouse wording before; the Workday form words both questions differently.
    seed_answers(running, {AUTH: ("select", "Yes"), SPONSOR: ("select", "No")})
    job_id = add_practice_job(running, "workday")
    store = running.app.app.state.store
    job = store.get(job_id)
    job.location = "Pittsburgh, PA"
    store.update(job)
    press(running, job_id, "prepare")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_review", job["status_detail"]
    fields = {f["label"]: f for f in details(running, job_id)["fields"]}
    auth = fields["Are you legally authorized to work in the country in which this job is located?"]
    sponsor = fields["Will you now or in the future require sponsorship for employment visa status?"]
    assert (auth["source"], auth["match"], auth["value"]) == ("saved_answer", "same_meaning", "Yes")
    assert (sponsor["source"], sponsor["match"], sponsor["value"]) == ("saved_answer", "same_meaning", "No")
    notes = details(running, job_id)["state"]["review_notes"]
    assert sum(n.startswith("Answered ") and n.endswith("Check it.") for n in notes) == 2
    assert [c for c in running.agents.calls if c["name"] == "question_matcher"]

    # Marked as applied without changes: the new wordings are remembered, so next time they match exactly.
    press(running, job_id, "applied")
    saved = {a.question: a for a in service(running).answers.all()}
    assert "Are you legally authorized to work in the country in which this job is located?" in saved[AUTH].also_asked_as


def test_with_meaning_matching_off_only_the_same_wording_is_used(ready):
    running = ready
    running.call("PUT", "/api/settings", {"match_questions_by_meaning": False})
    seed_answers(running, {AUTH: ("select", "Yes"), SPONSOR: ("select", "No")})
    job_id = add_practice_job(running, "workday")
    press(running, job_id, "prepare")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_input"
    assert len(job["status_detail"]["needs"]) == 2 and job["application"]["page"] == 2
    assert not [c for c in running.agents.calls if c["name"] == "question_matcher"]


def test_never_click_next_leaves_every_page_turn_to_you(ready):
    running = ready
    running.call("PUT", "/api/settings", {"never_click_next": True})
    seed_answers(running, {AUTH: ("select", "Yes"), SPONSOR: ("select", "No")})
    job_id = add_practice_job(running, "workday")
    store = running.app.app.state.store
    job = store.get(job_id)
    job.location = "Pittsburgh, PA"
    store.update(job)
    press(running, job_id, "prepare")
    job = wait_for(running, job_id)
    assert job["status"] == "waiting_for_page_review"
    assert "click Next yourself" in job["status_detail"]["message"]

    async def next_page(page):
        await page.click("#next")
        await page.wait_for_timeout(300)
    for expected_page in (2, 3):
        on_page(running, job_id, next_page)
        press(running, job_id, "continue")
        job = wait_for(running, job_id)
        assert job["application"]["page"] == expected_page
    assert job["status"] == "waiting_for_review", job["status_detail"]
    # The agent never clicked a button that moves on: every page turn was yours.
    assert all("click" not in leg["calls"] for leg in running.apply_agent.legs)
    assert [leg["reason"] for leg in running.apply_agent.legs] == ["prepare", "continue_after_input",
                                                                   "continue_after_input"]
