"""The profile and requirements routes, with stand-in agents."""

from __future__ import annotations

import json

from app.commands import cmd
from app.agents.runner import AgentError

from conftest import RESUME_LINES, make_pdf


def profile_file(running):
    return json.loads((running.paths.profile / "profile.json").read_text(encoding="utf-8"))


def reading_file(running):
    return json.loads((running.paths.profile / "search_profile.json").read_text(encoding="utf-8"))


# --- first run -------------------------------------------------------------------------------


def test_first_run_has_nothing_and_says_so(running):
    state = running.state()
    view = state["profile"]
    assert view["resume"] is None and view["facts"] is None and view["reading"] is None
    assert view["requirements"] == {"hard": [], "soft": []}
    assert view["ready"] is False and view["busy"] == {"profile": False, "requirements": False}
    from app import CHECKPOINT

    assert state["app"]["checkpoint"] == CHECKPOINT


def test_sign_in_state_is_reported_and_can_be_rechecked(running):
    running.agents.signed_in = False
    status, body, _ = running.call("POST", "/api/agents/recheck")
    assert status == 200 and body["agents"] == {"signed_in": False}
    running.agents.signed_in = True
    assert running.call("POST", "/api/agents/recheck")[1]["agents"] == {"signed_in": True}
    assert running.state()["agents"] == {"signed_in": True}


# --- resume to profile -------------------------------------------------------------------------


def test_upload_reads_the_resume_into_an_unconfirmed_profile(running, resume_pdf):
    status, body = running.upload("Sam Rivera resume.pdf", resume_pdf)
    assert status == 200 and body["profile"]["resume"]["file"] == "resume.pdf"
    view = running.wait_idle()
    assert view["errors"]["profile"] == "" and view["confirmed"] is False and view["stale"] is False
    assert view["facts"]["name"] == {"first": "Sam", "last": "Rivera", "full": "Sam Rivera"}
    assert view["facts"]["education"][0]["end_is_expected"] is True

    # The agent was given the resume's text and the instruction file, and no tools.
    call = running.agents.calls[0]
    assert call["name"] == "profile_builder"
    assert "<resume>" in call["prompt"] and "Halden Robotics" in call["prompt"]
    assert "Extract only what the resume states" in call["instructions"]
    assert (running.paths.profile / "resume.md").read_text(encoding="utf-8").startswith("Sam Rivera")

    # The agent's own question is kept, and code adds one for each value it could not find in the resume.
    fields = [item["field"] for item in view["to_confirm"]]
    assert fields[0] == "contact.phone"
    assert "links.linkedin" in fields                                  # not in this test resume
    assert "name.first" not in fields and "contact.email" not in fields
    saved = profile_file(running)
    assert saved["source"]["file"] == "resume.pdf" and len(saved["source"]["sha256"]) == 64


def test_bad_uploads_are_refused(running):
    status, body = running.upload("resume.txt", b"plain text")
    assert status == 422 and body["message"] == "Upload a PDF or a Word (.docx) file."
    status, body = running.upload("scan.pdf", make_pdf(["x"]))
    assert status == 422 and "almost no selectable text" in body["message"]
    assert running.agents.calls == []
    assert running.state()["profile"]["facts"] is None


def test_reading_without_a_resume_is_refused(running):
    status, body, _ = running.call("POST", "/api/profile/read")
    assert status == 422 and "no resume yet" in body["message"]


def test_confirming_saves_your_edits_and_clears_the_questions(running, resume_pdf):
    running.upload("resume.pdf", resume_pdf)
    facts = running.wait_idle()["facts"]
    facts["contact"]["phone"] = "555-0199"
    facts["links"]["linkedin"] = ""
    facts["skills"] = "Python, C++\nROS 2"
    facts["experience"][0]["highlights"] = "Line one, with a comma\nLine two"
    status, body, _ = running.call("PUT", "/api/profile/facts", facts)
    assert status == 200
    view = body["profile"]
    assert view["confirmed"] is True and view["to_confirm"] == [] and view["confirmed_at"]
    assert view["facts"]["contact"]["phone"] == "555-0199"
    assert view["facts"]["skills"] == ["Python", "C++", "ROS 2"]
    assert view["facts"]["experience"][0]["highlights"] == ["Line one, with a comma", "Line two"]
    assert profile_file(running)["confirmed"] is True


def test_profile_can_be_typed_in_without_any_agent(running):
    status, body, _ = running.call("PUT", "/api/profile/facts", {"name": {"first": "Sam", "last": "Rivera"}})
    assert status == 200 and body["profile"]["confirmed"] is True
    assert running.agents.calls == []
    assert running.call("PUT", "/api/profile/facts", ["not", "facts"])[0] == 422
    assert running.call("PUT", "/api/profile/facts", {"education": "not a list"})[0] == 422


def test_a_changed_resume_file_marks_the_profile_out_of_date(running, resume_pdf):
    running.upload("resume.pdf", resume_pdf)
    running.wait_idle()
    (running.paths.profile / "resume.pdf").write_bytes(make_pdf(RESUME_LINES + ["New line added later."]))
    assert running.state()["profile"]["stale"] is True
    running.call("POST", "/api/profile/read")
    assert running.wait_idle()["stale"] is False


def test_agent_problems_are_shown_in_plain_words(running, resume_pdf):
    running.agents.fail_with = AgentError("not_signed_in")
    running.upload("resume.pdf", resume_pdf)
    view = running.wait_idle()
    assert view["errors"]["profile"] == f"The agents are not signed in to Claude. Run '{cmd('signin')}', then try again."
    assert view["facts"] is None and view["resume"]["file"] == "resume.pdf"   # the upload itself is kept
    assert running.state()["agents"] == {"signed_in": False}

    running.agents.fail_with = AgentError("limit")
    running.call("POST", "/api/profile/read")
    assert "usage limit" in running.wait_idle()["errors"]["profile"]

    # An answer in the wrong shape is refused rather than saved.
    running.agents.fail_with = None
    running.agents.override["profile_builder"] = {"education": "none"}
    running.call("POST", "/api/profile/read")
    view = running.wait_idle()
    assert "could not be understood" in view["errors"]["profile"] and view["facts"] is None

    # And a later good run clears the message.
    running.agents.override.clear()
    running.call("POST", "/api/profile/read")
    view = running.wait_idle()
    assert view["errors"]["profile"] == "" and view["facts"]["name"]["first"] == "Sam"
    assert running.state()["agents"] == {"signed_in": True}


# --- requirements ----------------------------------------------------------------------------------


HARD = ["Must be an internship or entry level", "Must be in the United States"]
SOFT = ["Prefer the job to be related to robotics", "Prefer pay of at least $35 an hour"]


def test_requirements_are_saved_in_your_words_then_read(running):
    status, body, _ = running.call("PUT", "/api/requirements", {"hard": HARD, "soft": "\n".join(SOFT)})
    assert status == 200
    text = (running.paths.profile / "requirements.md").read_text(encoding="utf-8")
    assert text.startswith("## Hard requirements\n- Must be an internship or entry level\n")
    assert "## Soft preferences\n- Prefer the job to be related to robotics\n" in text

    view = running.wait_idle()
    assert view["requirements"] == {"hard": HARD, "soft": SOFT}
    reading = view["reading"]
    assert [item["text"] for item in reading["hard"]] == HARD and all(item["test"] for item in reading["hard"])
    assert [item["text"] for item in reading["soft"]] == SOFT
    assert reading["types"] == ["internship", "entry level"] and reading["pay_floor"] == "$35 an hour"
    assert view["reading_confirmed"] is False and view["reading_stale"] is False and view["ready"] is False

    request = json.loads(running.agents.calls[-1]["prompt"])
    assert request["hard"] == HARD and request["soft"] == SOFT and request["settled"] == []
    assert "today" in request


def test_the_agent_cannot_change_which_box_a_line_is_in(running):
    running.agents.override["requirements_reader"] = {
        "hard": [{"text": SOFT[0], "test": "moved by the agent"}, {"text": "Must have free lunch", "test": "invented"}],
        "soft": [], "titles": ["Robotics Intern"], "keywords": [], "locations": [], "types": [],
    }
    running.call("PUT", "/api/requirements", {"hard": HARD, "soft": SOFT})
    reading = running.wait_idle()["reading"]
    assert [item["text"] for item in reading["hard"]] == HARD
    assert [item["text"] for item in reading["soft"]] == SOFT
    assert reading["titles"] == ["Robotics Intern"]


def test_a_line_in_both_boxes_or_too_long_is_refused(running):
    status, body, _ = running.call("PUT", "/api/requirements", {"hard": ["Must be remote"], "soft": ["must be remote"]})
    assert status == 422 and body["message"] == '"Must be remote" is in both boxes. Keep it in one.'
    status, body, _ = running.call("PUT", "/api/requirements", {"hard": ["x" * 500], "soft": []})
    assert status == 422 and "too long" in body["message"]
    assert not (running.paths.profile / "requirements.md").exists()


def test_a_question_about_a_line_can_be_answered_either_way(running):
    hard = ["Must be an internship", "Prefer to be near Boston"]         # second reads like a wish
    soft = ["Must have a robotics team"]                                  # reads like a rule
    running.call("PUT", "/api/requirements", {"hard": hard, "soft": soft})
    view = running.wait_idle()
    assert [item["text"] for item in view["reading"]["ambiguous"]] == ["Prefer to be near Boston", "Must have a robotics team"]

    # "It stays a hard requirement": nothing moves, the question goes away, no agent is needed.
    calls = len(running.agents.calls)
    status, body, _ = running.call("POST", "/api/requirements/settle", {"text": "Prefer to be near Boston", "box": "hard"})
    assert status == 200 and len(running.agents.calls) == calls
    view = running.wait_idle()
    assert view["requirements"]["hard"] == hard
    assert [item["text"] for item in view["reading"]["ambiguous"]] == ["Must have a robotics team"]

    # "It is really a hard requirement": the line moves, and is read again to get a test.
    running.call("POST", "/api/requirements/settle", {"text": "Must have a robotics team", "box": "hard"})
    view = running.wait_idle()
    assert view["requirements"] == {"hard": hard + ["Must have a robotics team"], "soft": []}
    assert view["reading"]["ambiguous"] == [] and view["reading_stale"] is False
    assert view["reading"]["hard"][-1]["test"]
    assert json.loads(running.agents.calls[-1]["prompt"])["settled"] == ["Prefer to be near Boston", "Must have a robotics team"]

    status, body, _ = running.call("POST", "/api/requirements/settle", {"text": "Never written", "box": "soft"})
    assert status == 422 and "no longer in your requirements" in body["message"]


def test_corrections_to_the_reading_are_saved_and_confirmed(running):
    running.call("PUT", "/api/requirements", {"hard": HARD, "soft": SOFT})
    running.wait_idle()
    patch = {
        "titles": "Robotics Intern, Controls Intern", "locations": "Boston, MA\nPittsburgh, PA",
        "pay_floor": "$38 an hour", "remote_ok": False, "weights": {SOFT[0]: 3, SOFT[1]: 99},
        "hard": [{"text": "Sneaked in", "test": "x"}],      # not something a correction may change
    }
    status, body, _ = running.call("PUT", "/api/requirements/reading", patch)
    assert status == 200
    view = body["profile"]
    assert view["reading_confirmed"] is True
    reading = view["reading"]
    assert reading["titles"] == ["Robotics Intern", "Controls Intern"]
    assert reading["locations"] == ["Boston, MA", "Pittsburgh, PA"]
    assert reading["pay_floor"] == "$38 an hour" and reading["remote_ok"] is False
    assert [item["weight"] for item in reading["soft"]] == [3, 3]
    assert [item["text"] for item in reading["hard"]] == HARD
    assert reading_file(running)["confirmed"] is True


def test_changing_requirements_after_confirming_needs_a_new_reading(running, resume_pdf):
    running.upload("resume.pdf", resume_pdf)
    running.call("PUT", "/api/profile/facts", running.wait_idle()["facts"])
    running.call("PUT", "/api/requirements", {"hard": HARD, "soft": SOFT})
    running.wait_idle()
    running.call("PUT", "/api/requirements/reading", {})
    assert running.state()["profile"]["ready"] is True

    # A hand edit to requirements.md is noticed.
    path = running.paths.profile / "requirements.md"
    path.write_text(path.read_text(encoding="utf-8") + "- Prefer a short commute\n", encoding="utf-8")
    view = running.state()["profile"]
    assert view["reading_stale"] is True and view["reading_confirmed"] is False and view["ready"] is False
    status, body, _ = running.call("PUT", "/api/requirements/reading", {})
    assert status == 422 and "changed since they were read" in body["message"]

    running.call("POST", "/api/requirements/read")
    view = running.wait_idle()
    assert view["reading_stale"] is False and view["requirements"]["soft"][-1] == "Prefer a short commute"
    # The profile gave the reader context, but only as context.
    request = json.loads(running.agents.calls[-1]["prompt"])
    assert request["profile"]["education"][0]["field"] == "Electrical and Computer Engineering"


def test_clearing_both_boxes_needs_no_agent(running):
    running.call("PUT", "/api/requirements", {"hard": HARD, "soft": []})
    running.wait_idle()
    calls = len(running.agents.calls)
    status, body, _ = running.call("PUT", "/api/requirements", {"hard": [], "soft": ""})
    assert status == 200 and len(running.agents.calls) == calls
    view = running.wait_idle()
    assert view["requirements"] == {"hard": [], "soft": []} and view["reading"]["hard"] == []
    assert running.call("POST", "/api/requirements/read")[0] == 422


def test_requirements_reader_failure_keeps_your_words(running):
    running.agents.fail_with = AgentError("timeout")
    running.call("PUT", "/api/requirements", {"hard": HARD, "soft": SOFT})
    view = running.wait_idle()
    assert "did not answer in time" in view["errors"]["requirements"]
    assert view["requirements"] == {"hard": HARD, "soft": SOFT} and view["reading"] is None
