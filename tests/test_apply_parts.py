"""Checkpoint 5 parts: the answer repository, the application record, the writer's checks, and the
application agent's tools, each tested on its own."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.apply.answers import AnswerStore, norm_question
from app.apply.record import ApplicationRecord, NeedItem
from app.apply.tools import Leg, LegContext, degree_option
from app.apply.writer import limits, problems, tidy
from app.core.models import Job, Settings
from app.forms import fill, read_page
from app.forms.model import Field
from app.profile.demo import SAMPLE_FACTS
from app.profile.models import Facts

from test_forms import Env


# --- answers ---------------------------------------------------------------------------


def test_answers_are_learned_and_offered_again(tmp_path):
    store = AnswerStore(tmp_path / "answers.json")
    answer, what = store.learn("Are you legally authorized to work in the United States? *", "combobox",
                               ["Yes", "No"], "Yes", source="user", job_id="j_1", sensitive=True)
    assert what == "created" and answer.id == "a_0001" and answer.sensitivity == "sensitive"
    # The same question, worded the same, is an exact match; punctuation and the asterisk do not matter.
    exact = store.candidates("Are you legally authorized to work in the United States?", "select")
    assert [(c.answer.id, c.match) for c in exact] == [("a_0001", "exact")]
    # A close wording is offered as "similar", for the agent to judge; a different question is not offered.
    similar = store.candidates("Are you authorized to work legally in the United States", "radio")
    assert [(c.answer.id, c.match) for c in similar] == [("a_0001", "similar")]
    assert store.candidates("Will you require sponsorship?", "select") == []
    # A text question never gets a choice answer.
    assert store.candidates("Are you legally authorized to work in the United States?", "textarea") == []

    # Saving the same answer again changes nothing; a correction keeps the old answer in the history.
    assert store.learn("Are you legally authorized to work in the United States?", "select", [], "Yes",
                       source="user", job_id="j_2", sensitive=True)[1] == "same"
    answer, what = store.learn("Are you legally authorized to work in the United States?", "select", [], "No",
                               source="user_correction", job_id="j_2", sensitive=True)
    assert what == "corrected" and answer.answer == "No" and [h.answer for h in answer.history] == ["Yes"]

    # The file survives a restart.
    again = AnswerStore(tmp_path / "answers.json")
    assert again.get("a_0001").answer == "No"


def test_job_specific_answers_are_kept_but_not_reused(tmp_path):
    store = AnswerStore(tmp_path / "answers.json")
    text = "I like the team's work on warehouse robots. " * 4
    answer, _ = store.learn("Why do you want to work at Acme?", "textarea", [], text, source="user", job_id="j_1",
                            sensitive=False, company="Acme")
    assert answer.scope == "job_specific"
    assert store.candidates("Why do you want to work at Acme?", "textarea") == []
    # Named after the company, so job specific too.
    other, _ = store.learn("What draws you to Acme Robotics?", "textarea", [], text, source="user", job_id="j_1",
                           sensitive=False, company="Acme Robotics")
    assert other.scope == "job_specific"
    # They are still samples of your writing.
    assert sorted(s["question"] for s in store.writing_samples()) == ["What draws you to Acme Robotics?",
                                                                     "Why do you want to work at Acme?"]


def test_answers_can_be_edited_reworded_and_deleted(tmp_path):
    store = AnswerStore(tmp_path / "answers.json")
    answer, _ = store.learn("Preferred pronouns", "text", [], "they/them", source="user", job_id="j_1",
                            sensitive=False)
    store.add_wording(answer.id, "Your pronouns")
    assert store.candidates("Your pronouns", "text")[0].match == "exact"
    store.edit(answer.id, answer="she/her", sensitivity="sensitive", scope="global")
    item = store.get(answer.id)
    assert item.answer == "she/her" and item.sensitivity == "sensitive" and item.history[-1].answer == "they/them"
    store.used(answer.id)
    assert store.get(answer.id).times_used == 1
    store.delete(answer.id)
    assert store.all() == []
    with pytest.raises(KeyError):
        store.delete(answer.id)
    assert norm_question("  Are you 18+? (required) *") == "are you 18+"


# --- the application record -----------------------------------------------------------------


def test_the_record_keeps_fields_and_the_needs_list(tmp_path):
    record = ApplicationRecord(tmp_path / "j_1")
    record.upsert("f_1", label="First name", value="Sam", source="profile", filled_by="code")
    record.add_need(NeedItem(label="Sponsorship", key="f_2", reason="Sensitive."))
    record.add_need(NeedItem(label="Sponsorship", key="f_2", reason="Still sensitive."))
    record.add_need(NeedItem(label="Sign in, then click Continue", kind="task"))
    assert [n.reason for n in record.state.needs] == ["Still sensitive.", ""]
    record.log("Something happened")
    record.save()
    again = ApplicationRecord(tmp_path / "j_1")
    assert again.field("f_1").value == "Sam" and len(again.required_needs()) == 2
    again.drop_need(key="f_2")
    again.drop_need(label="Sign in, then click Continue")
    assert again.state.needs == []
    assert again.events()[-1].endswith("Something happened")
    assert again.save_draft("x.md", "hello").read_text(encoding="utf-8") == "hello"


# --- the writer's checks ------------------------------------------------------------------------


RESUME = "Halden Robotics. Wrote a ROS 2 node that cut arm calibration time from 40 minutes to 6. Python, C++."


def test_drafts_with_made_up_numbers_or_facts_are_refused():
    good = {"text": "At Halden Robotics I cut arm calibration from 40 minutes to 6 with a ROS 2 node.",
            "facts_used": ["Wrote a ROS 2 node that cut arm calibration time from 40 minutes to 6."]}
    assert problems(good, source_text=RESUME, other_text="", word_limit=None, char_limit=None) == []
    made_up = {"text": "I led a team of 12 engineers and cut costs by 30%.",
               "facts_used": ["Led a team of 12 engineers at Halden Robotics"]}
    found = problems(made_up, source_text=RESUME, other_text="", word_limit=None, char_limit=None)
    assert any("12, 30" in p for p in found) and any("could not be found" in p for p in found)
    placeholder = {"text": "I would love to join [Company].", "facts_used": []}
    assert "placeholder" in problems(placeholder, source_text=RESUME, other_text="", word_limit=None,
                                     char_limit=None)[0]
    long = {"text": "word " * 60, "facts_used": []}
    assert problems(long, source_text=RESUME, other_text="", word_limit=50, char_limit=None) == [
        "It has 60 words; the limit is 50."]
    # Numbers from the posting or the question are fine.
    posting = {"text": "The 2027 internship fits my 40 minutes to 6 work.", "facts_used": []}
    assert problems(posting, source_text=RESUME, other_text="Summer 2027 internship", word_limit=None,
                    char_limit=None) == []


def test_limits_and_tidying():
    field = Field(key="f", kind="textarea", label="Tell us about a project (max 150 words)", maxlength=1000)
    assert limits(field, "") == (150, 1000)
    assert limits(Field(key="f", kind="textarea", label="Why us?"), "Keep it under 500 characters") == (None, 500)
    assert tidy("## Heading\n**Bold** text\n\n- a point\n\n\n second  paragraph ") == \
        "Heading Bold text\n\na point\n\nsecond paragraph"
    assert degree_option("Master of Science", ["Bachelor's Degree", "Master's Degree", "Other"]) == "Master's Degree"
    assert degree_option("Master of Science", ["Masters", "Master's Degree"]) is None
    assert degree_option("High school diploma", ["Bachelor's Degree"]) is None


# --- the agent's tools, on a real page ------------------------------------------------------------------


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    pytest.importorskip("playwright.async_api")
    e = Env(tmp_path_factory.mktemp("tools"))
    try:
        e.run(e.start())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"No browser for the tool tests: {exc}")
    yield e
    e.run(e.stop())
    e.loop.call_soon_threadsafe(e.loop.stop)
    e.thread.join(timeout=5)


def make_leg(env, page, tmp: Path, *, reason="prepare", settings=None, answers=None, writer=None) -> Leg:
    folder = tmp / "profile"
    folder.mkdir(exist_ok=True)
    resume = folder / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    job = Job(id="j_test", company="Practice Robotics", title="Robotics Software Intern", url="https://example.com/1")
    ctx = LegContext(job=job, page=page, record=ApplicationRecord(tmp / "applications" / "j_test"),
                     answers=answers or AnswerStore(tmp / "answers.json"), settings=settings or Settings(),
                     facts=Facts.model_validate(SAMPLE_FACTS), resume=resume, profile_folder=folder, reason=reason,
                     site_notes=tmp / "site_notes", writer=writer)
    return Leg(ctx)


def field_id(view, label):
    return next(f["id"] for f in view["fields"] if f["label"] == label)


def test_the_tools_refuse_what_belongs_to_you(env, tmp_path):
    async def scenario():
        page = await env.open("greenhouse")
        answers = AnswerStore(tmp_path / "answers.json")
        saved, _ = answers.learn("Are you legally authorized to work in the United States?", "combobox", ["Yes", "No"],
                                 "Yes", source="user", job_id="j_old", sensitive=True)
        leg = make_leg(env, page, tmp_path, answers=answers)
        view = await leg.get_page()
        auth = next(f for f in view["fields"] if f["label"].startswith("Are you legally authorized"))
        sponsor = field_id(view, "Will you now or in the future require sponsorship for employment visa status (e.g. H-1B)?")
        privacy = field_id(view, "I acknowledge that I have read the candidate privacy notice.")
        why = field_id(view, "Why are you interested in Practice Robotics?")
        school = next(f for f in view["fields"] if f["label"] == "School")
        assert auth["sensitive"] == "work_authorization"
        assert auth["suggested"] == {"value": "Yes", "source": "saved_answer", "answer_id": saved.id,
                                     "match": "exact", "sensitive": True}
        assert school["suggested"]["value"] == "Three Rivers University" and school["suggested"]["source"] == "resume"
        assert next(f for f in view["fields"] if f["label"] == "First Name")["suggested"] == {
            "value": "Sam", "source": "profile"}
        submit = next(b for b in view["buttons"] if b["text"] == "Submit application")
        assert submit["kind"] == "submit" and submit["you_may_click"] is False

        result = await leg.fill_fields([
            {"id": sponsor, "value": "No", "source": "inferred", "reason": "guess"},
            {"id": auth["id"], "value": "No", "source": "saved_answer", "answer_id": saved.id, "reason": "changed"},
            {"id": auth["id"], "value": "Yes", "source": "saved_answer", "answer_id": "a_9999", "reason": "made up"},
            {"id": privacy, "value": True, "source": "inferred", "reason": "tick it"},
            {"id": why, "value": "x" * 400, "source": "inferred", "reason": "long text"},
            {"id": "f_nothing", "value": "x", "source": "profile", "reason": ""},
        ])
        statuses = [(r.get("status"), r["message"][:40]) for r in result["results"]]
        assert all(status == "refused" for status, _ in statuses), statuses
        messages = " ".join(r["message"] for r in result["results"])
        for expected in ("sensitive question", "exactly as the user gave it", "answer_id must be one of",
                         "Agreements and certifications", "draft_long_answer", "no field with that id"):
            assert expected in messages, expected
        # The saved answer, used exactly, is accepted.
        result = await leg.fill_fields([{"id": auth["id"], "value": "Yes", "source": "saved_answer",
                                         "answer_id": saved.id, "reason": "your answer"}])
        assert result["results"][0]["status"] == "ok"
        assert leg.ctx.record.field(auth["id"]).source == "saved_answer"
        assert answers.get(saved.id).times_used == 1

        # Submit is never clicked; Next is not clicked while "pause after each page" is on.
        assert "never submits" in (await leg.click(submit["id"], "next_page"))["message"]
        # Ending the leg is checked against the page.
        refused = await leg.end_leg("ready_for_review", "done")
        assert refused["ok"] is False and "Will you now or in the future" in refused["message"]
        assert (await leg.end_leg("needs_input", "x"))["ok"] is False
        await leg.need_user("Sponsorship", "Sensitive: only your own answer.", True, sponsor)
        assert (await leg.end_leg("needs_input", "I need you for sponsorship."))["ok"] is True
        assert leg.outcome == "needs_input"
        assert any("privacy notice" in note for note in leg.ctx.record.state.review_notes)
        assert not await page.locator("#practice-sent").count()
        await page.close()

    env.run(scenario())


def test_what_you_typed_is_never_overwritten_and_drafting_obeys_the_setting(env, tmp_path):
    async def scenario():
        page = await env.open("greenhouse")
        leg = make_leg(env, page, tmp_path, settings=Settings(write_long_answers=False))
        view = await leg.get_page()
        preferred = field_id(view, "Preferred First Name")
        why = field_id(view, "Why are you interested in Practice Robotics?")
        await fill(page, {preferred: "Sammy"})
        leg.ctx.record.upsert(preferred, label="Preferred First Name", value="Sammy", source="user",
                              filled_by="user", user_changed=True)
        result = await leg.fill_fields([{"id": preferred, "value": "Sam", "source": "profile", "reason": ""}])
        assert result["results"][0]["status"] == "refused" and "user entered" in result["results"][0]["message"]
        drafted = await leg.draft_long_answer(why, "")
        assert drafted["ok"] is False and "switched off" in drafted["message"]
        # The resume goes only into a file field, and only from the profile folder.
        resume_field = field_id(view, "Resume/CV")
        assert (await leg.upload_document(resume_field, "resume"))["ok"] is True
        assert (await leg.upload_document(preferred, "resume"))["ok"] is False
        assert "No cover letter" in (await leg.upload_document(field_id(view, "Cover Letter"), "cover_letter"))["message"]
        await page.close()

    env.run(scenario())


def test_passwords_are_never_filled_and_sign_in_pages_are_reported(env, tmp_path):
    async def scenario():
        page = await env.browser.new_page()
        await page.set_content("""<form><label for=e>Email</label><input id=e type=email>
            <label for=p>Password</label><input id=p type=password>
            <button type=button>Sign in</button></form>""")
        leg = make_leg(env, page, tmp_path)
        view = await leg.get_page()
        assert any("password" in notice for notice in view["notices"])
        password = next(f for f in view["fields"] if f["kind"] == "password")
        assert password["rule"].startswith("never filled")
        email = next(f for f in view["fields"] if f["kind"] == "email")
        result = await leg.fill_fields([{"id": password["id"], "value": "hunter2", "source": "profile", "reason": ""},
                                        {"id": email["id"], "value": "sam@example.com", "source": "profile", "reason": ""}])
        # Nothing at all is filled on a page that asks for a password, not even the email.
        assert result["ok"] is False and "password" in result["message"]
        assert await page.locator("#p").input_value() == ""
        assert await page.locator("#e").input_value() == ""
        # And no button on it may be clicked to move on.
        assert all(not b["you_may_click"] for b in view["buttons"])
        button = view["buttons"][0]["id"]
        clicked = await leg.click(button, "next_page")
        assert clicked["ok"] is False and "password" in clicked["message"]
        await page.close()

    env.run(scenario())


def test_the_rule_based_agent_stops_at_a_sign_in_page(env, tmp_path):
    from app.apply.agent import rule_based_agent

    async def scenario():
        page = await env.browser.new_page()
        await page.set_content("""<form><label for=e>Email</label><input id=e type=email>
            <label for=p>Password</label><input id=p type=password><button type=button>Sign in</button></form>""")
        leg = make_leg(env, page, tmp_path)
        await rule_based_agent(leg, {})
        assert leg.outcome == "blocked"
        assert leg.ctx.record.state.needs[0].label.startswith("Sign in or create your account")
        assert await page.locator("#p").input_value() == ""
        await page.close()

    env.run(scenario())


def test_site_notes_are_kept_per_site(env, tmp_path):
    async def scenario():
        page = await env.open("lever")
        leg = make_leg(env, page, tmp_path)
        await leg.save_site_note("The location box needs a pause before its list appears.")
        await leg.save_site_note("The location box needs a pause before its list appears.")
        notes = (tmp_path / "site_notes" / "127.0.0.1.md").read_text(encoding="utf-8").splitlines()
        assert notes == ["# Notes about 127.0.0.1", "- The location box needs a pause before its list appears."]
        shot = await leg.screenshot()
        assert shot[:2] == b"\xff\xd8"
        inspected = await leg.inspect_element((await leg.get_page())["fields"][0]["id"])
        assert inspected["ok"] and "<" in inspected["html"]
        await page.close()

    env.run(scenario())


def test_the_brief_never_repeats_sensitive_values(env, tmp_path):
    from app.apply.tools import build_brief

    async def scenario():
        page = await env.open("lever")
        leg = make_leg(env, page, tmp_path)
        leg.ctx.record.upsert("f_x", label="Sponsorship", category="sensitive:sponsorship", value="No", source="user")
        leg.ctx.record.upsert("f_y", label="Full name", category="full_name", value="Sam Rivera", source="profile")
        brief = build_brief(leg.ctx, [])
        filled = {f["label"]: f["value"] for f in brief["record"]["fields_filled"]}
        assert filled == {"Sponsorship": "(answered)", "Full name": "Sam Rivera"}
        assert brief["profile"]["experience"][0]["employer"] == "Halden Robotics"
        json.dumps(brief)
        await page.close()

    env.run(scenario())


def test_sensitive_questions_only_take_answers_to_the_same_question(env, tmp_path):
    async def scenario():
        page = await env.open("greenhouse")
        answers = AnswerStore(tmp_path / "answers.json")
        # Close wording, but not the same question: for a sensitive question that is not enough.
        canada, _ = answers.learn("Are you legally authorized to work in Canada?", "select", ["Yes", "No"], "Yes",
                                  source="user", job_id="j_old", sensitive=True)
        heard, _ = answers.learn("How did you hear about this role?", "select", ["LinkedIn"], "LinkedIn",
                                 source="user", job_id="j_old", sensitive=False)
        leg = make_leg(env, page, tmp_path, answers=answers)
        view = await leg.get_page()
        auth = next(f for f in view["fields"] if f["label"].startswith("Are you legally authorized"))
        assert "suggested" not in auth
        result = await leg.fill_fields([{"id": auth["id"], "value": "Yes", "source": "saved_answer",
                                         "answer_id": canada.id, "reason": "close enough"}])
        assert result["results"][0]["status"] == "refused"
        # An ordinary question's close wording is offered, for the agent to judge.
        source = next(f for f in view["fields"] if f["label"] == "How did you hear about this job?")
        assert source["suggested"]["match"] == "similar" and source["suggested"]["answer_id"] == heard.id
        assert source["suggested"]["saved_question"] == "How did you hear about this role?"
        await page.close()

    env.run(scenario())


def test_secret_values_never_reach_the_agent(env, tmp_path):
    async def scenario():
        page = await env.browser.new_page()
        await page.set_content("""<form><div class="field"><label for=p>Password</label>
            <input id=p type=password value="hunter2-secret"></div>
            <div class="field"><label for=s>Social Security Number</label><input id=s value="123-45-6789"></div></form>""")
        leg = make_leg(env, page, tmp_path)
        view = await leg.get_page()
        assert {f["label"]: f.get("value") for f in view["fields"]} == {
            "Password": "(entered)", "Social Security Number": "(entered)"}
        for field in view["fields"]:
            html = (await leg.inspect_element(field["id"]))["html"]
            assert "hunter2" not in html and "123-45" not in html, html
        assert (await leg.screenshot())[:2] == b"\xff\xd8"
        assert "hunter2" not in json.dumps(view) and "123-45" not in json.dumps(view)
        await page.close()

    env.run(scenario())


# --- 0.8.0: never clicking Next, and questions recognised by meaning --------------------------------


def test_never_click_next_refuses_every_button_that_moves_on(env, tmp_path):
    async def scenario():
        page = await env.open("workday")
        leg = make_leg(env, page, tmp_path, reason="continue_after_page_review",
                       settings=Settings(never_click_next=True))
        view = await leg.get_page()
        nxt = next(b for b in view["buttons"] if b["kind"] == "next")
        assert nxt["you_may_click"] is False
        result = await leg.click(nxt["id"], "next_page")
        assert result["ok"] is False and "never clicks Next" in result["message"]
        assert leg.next_clicks == 0 and leg.ctx.record.state.page == 1
        # Ending the page is allowed even with "Pause after each page" off: you move on yourself.
        for f in view["fields"]:
            if f.get("required") and "value" not in f and not f.get("hidden"):
                await leg.need_user(f["label"], "test", False, f["id"])
        refused = await leg.end_leg("ready_for_review", "x")
        assert refused["ok"] is False and "Next button" in refused["message"]
        assert (await leg.end_leg("page_done", "Filled; click Next yourself."))["ok"] is True
        assert leg.outcome == "page_done"
        await page.close()

        page = await env.browser.new_page()
        await page.set_content("<h1>Robotics intern</h1><p>Join us.</p><button type=button>Apply</button>")
        (tmp_path / "second").mkdir()
        leg = make_leg(env, page, tmp_path / "second", settings=Settings(never_click_next=True))
        view = await leg.get_page()
        opener = next(b for b in view["buttons"] if b["kind"] == "open_application")
        assert opener["you_may_click"] is False
        assert (await leg.click(opener["id"], "open_form"))["ok"] is False
        from app.apply.agent import rule_based_agent
        await rule_based_agent(leg, {})
        assert leg.outcome == "needs_input" and leg.ctx.record.state.needs[0].label.startswith("Open the application")
        await page.close()

    env.run(scenario())


def matcher_for(reply=None):
    """A question matcher whose agent is the word-rule stand-in, or returns ``reply``."""
    from app.agents.runner import AgentReply
    from app.apply.questions import QuestionMatcher, demo_match
    from app.paths import Paths
    from conftest import REPO

    calls = []

    async def ask(*, name, instructions, prompt, schema):
        request = json.loads(prompt)
        calls.append(request)
        data = reply(request) if callable(reply) else reply if reply is not None else demo_match(request)
        return AgentReply(data=data, cost_usd=0.01)

    matcher = QuestionMatcher(Paths(REPO), ask)
    matcher.calls = calls
    return matcher


QUESTIONS_PAGE = """<form><h2>Eligibility</h2>
<label for=elig>Are you eligible to work in the country where this role is based?</label>
<select id=elig aria-required=true><option value=""></option><option>Yes</option><option>No</option></select>
<label for=visa>Do you need a visa to work here?</label>
<select id=visa aria-required=true><option value=""></option><option>Yes</option><option>No</option></select>
<label for=ca>Are you authorized to work in Canada?</label>
<select id=ca><option value=""></option><option>Yes</option><option>No</option></select>
<label for=g>Gender identity</label>
<select id=g><option value=""></option><option>Man</option><option>Woman</option><option>I prefer not to say</option></select>
<label for=start>When can you start?</label><input id=start>
</form>"""


def test_questions_are_recognised_by_meaning_and_your_own_answers_reused(env, tmp_path):
    async def scenario():
        page = await env.browser.new_page()
        await page.set_content(QUESTIONS_PAGE)
        answers = AnswerStore(tmp_path / "answers.json")
        us, _ = answers.learn("Are you legally authorized to work in the United States?", "select", ["Yes", "No"], "Yes",
                              source="user", job_id="j_old", sensitive=True)
        matcher = matcher_for()
        leg = make_leg(env, page, tmp_path, answers=answers)
        leg.ctx.matcher = matcher
        leg.ctx.job.location = "Pittsburgh, PA"
        view = await leg.get_page()
        fields = {f["label"]: f for f in view["fields"]}
        elig = fields["Are you eligible to work in the country where this role is based?"]
        # Different wording, same question, same country: your answer is offered, marked as matched by meaning.
        assert elig["suggested"]["match"] == "same_meaning" and elig["suggested"]["value"] == "Yes"
        assert elig["suggested"]["answer_id"] == us.id and elig["sensitive"] == "work_authorization"
        assert "United States" in elig["suggested"]["why"]
        # The visa question is not matched by the wording rules, but it is sensitive all the same.
        visa = fields["Do you need a visa to work here?"]
        assert visa["sensitive"] == "sponsorship" and "suggested" not in visa
        # Canada is a different country: not matched.
        assert "suggested" not in fields["Are you authorized to work in Canada?"]
        assert fields["When can you start?"].get("sensitive") is None
        # The matcher was asked once, with the page's context and the job's country.
        assert len(matcher.calls) == 1 and matcher.calls[0]["job"]["country"] == "United States"
        assert {q["label"] for q in matcher.calls[0]["questions"]} >= {"Do you need a visa to work here?"}
        await leg.get_page()
        assert len(matcher.calls) == 1

        result = await leg.fill_fields([
            {"id": visa["id"], "value": "No", "source": "inferred", "reason": "guess"},
            {"id": elig["id"], "value": "No", "source": "saved_answer", "answer_id": us.id, "reason": "changed"},
        ])
        assert [r["status"] for r in result["results"]] == ["refused", "refused"]
        result = await leg.fill_fields([{"id": elig["id"], "value": "Yes", "source": "saved_answer",
                                         "answer_id": us.id, "reason": "your answer"}])
        assert result["results"][0]["status"] == "ok"
        assert leg.ctx.record.field(elig["id"]).match == "same_meaning"
        assert any(n.startswith('Answered "Are you eligible to work in the country where this role is based" with '
                                'your saved answer to "Are you legally authorized to work in the United States"')
                   for n in leg.ctx.record.state.review_notes)
        # What the matcher read is kept with the application, for learning and later legs.
        saved = leg.ctx.record.state.questions[visa["id"]]
        assert saved["topic"] == "sponsorship" and saved["sensitive"] is True
        await page.close()

    env.run(scenario())


def test_code_refuses_matches_the_rules_do_not_allow(env, tmp_path):
    async def scenario():
        page = await env.browser.new_page()
        await page.set_content(QUESTIONS_PAGE)
        answers = AnswerStore(tmp_path / "answers.json")
        us, _ = answers.learn("Are you legally authorized to work in the United States?", "select", ["Yes", "No"], "Yes",
                              source="user", job_id="j_old", sensitive=True, topic="work_authorization",
                              place="United States")
        gender, _ = answers.learn("Gender", "select", ["Male", "Female"], "Male", source="user", job_id="j_old",
                                  sensitive=True, topic="gender")
        why, _ = answers.learn("Why do you want to work at Acme?", "textarea", [], "Because. " * 20, source="user",
                               job_id="j_old", sensitive=False, company="Acme")
        citizen, _ = answers.learn("Are you a U.S. citizen?", "radio", ["Yes", "No"], "Yes", source="user",
                                   job_id="j_old", sensitive=True, topic="citizenship", place="United States")

        def reply(request):
            ids = {q["label"]: q["id"] for q in request["questions"]}
            def q(label, topic, place, answer_id, relation, value):
                return {"id": ids[label], "asks": label, "topic": topic, "sensitive": True, "place": place,
                        "match": {"answer_id": answer_id, "relation": relation, "value": value, "why": "test"}}
            return {"questions": [
                # Same question but the value is not one of the choices.
                q("Are you eligible to work in the country where this role is based?", "work_authorization",
                  "United States", us.id, "same", "Absolutely"),
                # Worked out from citizenship: allowed for this topic, on a choice question.
                q("Do you need a visa to work here?", "sponsorship", "United States", citizen.id, "derived", "No"),
                # A different country from the one the answer was recorded for.
                q("Are you authorized to work in Canada?", "work_authorization", "Canada", us.id, "same", "Yes"),
                # Self-identification may only be "same"; and a job-specific answer is never reused.
                q("Gender identity", "gender", "", gender.id, "derived", "Man"),
                q("When can you start?", "start_date", "", why.id, "same", "Because."),
            ]}

        leg = make_leg(env, page, tmp_path, answers=answers)
        leg.ctx.matcher = matcher_for(reply)
        view = await leg.get_page()
        fields = {f["label"]: f for f in view["fields"]}
        offered = {label: f.get("suggested", {}).get("match") for label, f in fields.items()}
        assert offered == {
            "Are you eligible to work in the country where this role is based?": None,
            "Do you need a visa to work here?": "derived",
            "Are you authorized to work in Canada?": None,
            "Gender identity": None,
            "When can you start?": None,
        }
        refused = {q["label"]: q["refused"] for q in leg.ctx.record.state.questions.values()}
        assert "not one of the choices" in refused["Are you eligible to work in the country where this role is based?"]
        assert "was for United States" in refused["Are you authorized to work in Canada?"]
        assert "only work authorization" in refused["Gender identity"]
        assert "written for one job" in refused["When can you start?"]

        # A same-meaning match for self-identification may map your wording onto the form's ("Male" -> "Man").
        (tmp_path / "second").mkdir()
        leg2 = make_leg(env, page, tmp_path / "second", answers=answers)
        leg2.ctx.matcher = matcher_for(lambda request: {"questions": [
            {"id": q["id"], "asks": q["label"], "topic": "gender", "sensitive": True, "place": "",
             "match": {"answer_id": gender.id, "relation": "same", "value": "Man", "why": "Same question."}}
            for q in request["questions"] if q["label"] == "Gender identity"]})
        view = await leg2.get_page()
        assert next(f for f in view["fields"] if f["label"] == "Gender identity")["suggested"]["value"] == "Man"

        # If the matcher fails, only exact wording is matched, and nothing breaks.
        from app.agents.runner import AgentError

        async def broken(**_kw):
            raise AgentError("limit")
        (tmp_path / "third").mkdir()
        leg3 = make_leg(env, page, tmp_path / "third", answers=answers)
        leg3.ctx.matcher = matcher_for()
        leg3.ctx.matcher.ask = broken
        view = await leg3.get_page()
        assert all("suggested" not in f for f in view["fields"])
        await page.close()

    env.run(scenario())
