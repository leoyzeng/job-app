"""The dashboard's checkpoint 5 parts: an application's record on its listing, and the Answers screen."""

from __future__ import annotations

from app.apply.answers import AnswerStore
from app.apply.record import ApplicationRecord, NeedItem
from app.core.models import Job, Need, Source, Status, StatusDetail
from app.core.store import TrackerStore


def seed_application(running, *, looks_submitted=False):
    store = TrackerStore(running.paths.tracker)
    job = Job(id="j_app", company="Practice Robotics", title="Robotics Software Intern",
              url="https://jobs.example.com/greenhouse/1", source=Source(found_via="company career page"),
              status=Status.WAITING_FOR_INPUT,
              status_detail=StatusDetail(message="Filled 14 fields. I need you for the sponsorship question.",
                                         needs=[Need(label="Sponsorship", page=1, reason="A sensitive question.")]))
    job.application.folder = "applications/j_app"
    if looks_submitted:
        job.status = Status.WAITING_FOR_REVIEW
        setattr(job.application, "looks_submitted", True)
    store.add(job)
    record = ApplicationRecord(running.paths.applications / "j_app")
    record.upsert("f_1", label="First Name", value="Sam", source="profile", filled_by="code", verified=True)
    record.upsert("f_2", label="Resume/CV", value="resume.pdf", source="resume", filled_by="code", verified=True)
    record.upsert("f_3", label="Why are you interested in Practice Robotics?", value="I wrote ROS 2 code. " * 3,
                  source="ai_draft", filled_by="agent", verified=True)
    record.upsert("f_4", label="Do you have US work authorization?", value="Yes", source="saved_answer",
                  match="similar", answer_id="a_0001", filled_by="agent", verified=True)
    record.upsert("f_5", label="Phone", value="555-0142", source="user", filled_by="user", user_changed=True)
    record.add_need(NeedItem(label="Sponsorship", key="f_6", reason="A sensitive question."))
    record.state.review_notes = ["AI-drafted: Why are you interested in Practice Robotics?. Read it before you submit."]
    record.state.cost_usd = 0.42
    record.save_draft("why_are_you_interested.md", "# Why are you interested?\n\nI wrote ROS 2 code.")
    record.log("Filled 3 field(s) from your profile.")
    record.save()
    return job


def test_a_listing_shows_how_its_application_was_filled(open_dashboard, running):
    seed_application(running)
    page = open_dashboard()
    page.click(".view-link[data-view=jobs]")
    page.click(".tab[data-tab=waiting]")
    row = page.locator(".job[data-id=j_app]")
    row.wait_for()
    assert "Sponsorship" in row.locator(".needs").inner_text()
    row.locator("[data-action=details]").click()
    record = row.locator(".app-record")
    record.locator(".app-fields").wait_for()
    table = {r.locator("th").inner_text(): r.locator("td.source").inner_text()
             for r in record.locator("tbody tr").all()}
    assert table["First Name"] == "from your profile"
    assert table["Why are you interested in Practice Robotics?"] == "AI-drafted"
    assert table["Do you have US work authorization?"] == "your saved answer (similar question)"
    assert table["Phone"] == "entered by you"
    assert "Read it before you submit" in record.locator(".review-notes").inner_text()
    record.locator(".drafts summary").click()
    assert "I wrote ROS 2 code." in record.locator(".draft-text").inner_text()
    assert "Agent usage so far: $0.42" in record.locator(".app-meta").inner_text()
    record.locator(".events summary").click()
    assert "Filled 3 field(s) from your profile." in record.locator(".events").inner_text()
    # Continue is the next step for a paused application.
    assert "btn-main" in row.locator("[data-action=continue]").get_attribute("class")


def test_a_listing_that_looks_submitted_offers_mark_as_applied(open_dashboard, running):
    seed_application(running, looks_submitted=True)
    page = open_dashboard()
    page.click(".view-link[data-view=jobs]")
    page.click(".tab[data-tab=waiting]")
    row = page.locator(".job[data-id=j_app]")
    row.wait_for()
    assert "Looks submitted" in row.locator(".submitted").inner_text()
    assert "btn-main" in row.locator("[data-action=applied]").get_attribute("class")
    row.locator("[data-action=applied]").click()
    page.wait_for_selector("#toasts >> text=Marked Robotics Software Intern at Practice Robotics as applied.")
    assert TrackerStore(running.paths.tracker).get("j_app").status == Status.APPLIED


def test_the_answers_screen_lists_corrects_and_deletes_your_answers(open_dashboard, running):
    store = AnswerStore(running.paths.answers)
    auth, _ = store.learn("Are you legally authorized to work in the United States?", "select", ["Yes", "No"], "Yes",
                          source="user", job_id="j_1", sensitive=True)
    pronouns, _ = store.learn("Preferred pronouns", "text", [], "they/them", source="user", job_id="j_1",
                              sensitive=False)
    store.learn("Why do you want to work at Acme?", "textarea", [], "Because of the robots. " * 6, source="user",
                job_id="j_1", sensitive=False, company="Acme")
    running.app.app.state.applications.answers._fresh()

    page = open_dashboard()
    page.click(".view-link[data-view=answers]")
    page.wait_for_selector(".answer")
    groups = [g.inner_text() for g in page.locator(".group-title").all()]
    assert groups == ["Sensitive answers", "Answers the agent can reuse", "Written for one job"]

    card = page.locator(f".answer[data-id={pronouns.id}]")
    card.locator(".answer-input").fill("she/her")
    card.locator("button", has_text="Save").click()
    page.wait_for_selector("#toasts >> text=Saved. The agent uses this from now on.")
    assert AnswerStore(running.paths.answers).get(pronouns.id).answer == "she/her"
    card = page.locator(f".answer[data-id={pronouns.id}]")
    card.locator("summary", has_text="Earlier answers").click()
    assert "they/them" in card.locator(".answer-history").inner_text()

    # Marking it sensitive moves it to the first group.
    card.locator("label.tick", has_text="Sensitive").locator("input").check()
    page.wait_for_function("document.querySelectorAll('.group')[0].innerText.includes('Preferred pronouns')")
    assert AnswerStore(running.paths.answers).get(pronouns.id).sensitivity == "sensitive"

    # Find, then delete with two clicks.
    page.fill("#answers-filter", "authorized")
    assert page.locator(".answer").count() == 1
    card = page.locator(f".answer[data-id={auth.id}]")
    card.locator("button", has_text="Delete").click()
    card.locator("button", has_text="Click again to delete").click()
    page.wait_for_selector("#toasts >> text=Deleted. The agent will ask you again next time.")
    assert [a.id for a in AnswerStore(running.paths.answers).all()] == [pronouns.id, "a_0003"]
