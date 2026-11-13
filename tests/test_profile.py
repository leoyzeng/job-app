"""Resume reading, requirements, the checks on what agents return, and the agent runner."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.commands import cmd
from app.agents import runner
from app.agents.runner import AgentError, classify, parse_json
from app.core.models import Settings
from app.paths import Paths
from app.profile.models import BuilderOutput, Facts, SearchReading, agent_schema
from app.profile.requirements import (
    BadRequirements, clean_lines, fingerprint, format_requirements, parse_requirements, reconcile,
)
from app.profile.resume import BadResume, ResumeText, current_resume, extract_text, save_upload
from app.profile.service import load_instructions, not_in_resume

from conftest import REPO, RESUME_LINES, make_pdf


# --- resume ------------------------------------------------------------------------------


def test_pdf_and_word_resumes_are_read(tmp_path, resume_pdf, resume_docx):
    for name, data in (("My Resume.pdf", resume_pdf), ("cv.docx", resume_docx)):
        path = save_upload(tmp_path, name, data)
        assert path.name == "resume" + path.suffix
        text = extract_text(path).text
        assert "Sam Rivera" in text and "Halden Robotics" in text and "3.8/4.0" in text


def test_one_current_resume_and_the_older_one_is_kept(tmp_path, resume_pdf, resume_docx):
    save_upload(tmp_path, "a.pdf", resume_pdf)
    save_upload(tmp_path, "b.docx", resume_docx)
    assert current_resume(tmp_path).name == "resume.docx"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["resume.docx", "resume.previous.pdf"]


def test_a_resume_dropped_in_by_hand_under_any_name_is_found(tmp_path, resume_pdf):
    (tmp_path / "Sam Rivera 2026.pdf").write_bytes(resume_pdf)
    (tmp_path / "notes.txt").write_text("not a resume", encoding="utf-8")
    assert current_resume(tmp_path).name == "Sam Rivera 2026.pdf"
    assert current_resume(tmp_path / "missing") is None


@pytest.mark.parametrize(
    "name, data, expected",
    [
        ("resume.txt", b"hello", "Upload a PDF or a Word (.docx) file."),
        ("resume.pdf", b"", "That file is empty."),
        ("resume.pdf", b"PK not really a pdf", "named like a PDF file but is not one"),
        ("resume.docx", b"%PDF-1.4", "named like a Word file but is not one"),
        ("resume.pdf", b"%PDF" + b"0" * (10 * 1024 * 1024), "larger than 10 MB"),
    ],
)
def test_unusable_uploads_are_refused_with_a_reason(tmp_path, name, data, expected):
    with pytest.raises(BadResume, match=expected.replace("(", r"\(").replace(")", r"\)")):
        save_upload(tmp_path, name, data)
    assert list(tmp_path.iterdir()) == []


def test_a_scan_with_no_text_is_reported(tmp_path):
    path = save_upload(tmp_path, "scan.pdf", make_pdf(["x"]))
    with pytest.raises(BadResume, match="almost no selectable text"):
        extract_text(path)


def test_a_broken_file_is_reported(tmp_path):
    path = save_upload(tmp_path, "broken.pdf", b"%PDF-1.4 this is not a real pdf body")
    with pytest.raises(BadResume, match="could not be opened"):
        extract_text(path)


def test_links_are_listed_for_the_agent():
    text = ResumeText(text="Sam Rivera\nPersonal Website", links=["https://samrivera.example.com/"]).as_markdown()
    assert text.endswith("Links in the document:\n- https://samrivera.example.com/\n")


# --- facts are checked against the resume ----------------------------------------------------


def test_values_missing_from_the_resume_become_questions():
    resume = ResumeText(text="\n".join(RESUME_LINES), links=["https://samrivera.example.com/"])
    good = Facts.model_validate({
        "name": {"first": "Sam", "last": "Rivera"},
        "contact": {"email": "sam.rivera@example.com", "phone": "+1 (555) 0142"},
        "links": {"github": "https://www.github.com/samrivera-example/", "website": "samrivera.example.com"},
        "education": [{"school": "Three Rivers University"}],
        "experience": [{"employer": "Halden  Robotics"}],
    })
    assert not_in_resume(good, resume) == []

    invented = Facts.model_validate({
        "name": {"first": "Samuel", "last": "Rivera"},
        "contact": {"email": "sam@elsewhere.example", "phone": "555-9999"},
        "links": {"linkedin": "linkedin.com/in/someone-else"},
        "education": [{"school": "Harvard University"}],
        "experience": [{"employer": "Halden Robotics"}, {"employer": "Globex"}],
    })
    flagged = {item.field for item in not_in_resume(invented, resume)}
    assert flagged == {"name.first", "contact.email", "contact.phone", "links.linkedin",
                       "education[0].school", "experience[1].employer"}


def test_agent_output_is_tolerant_of_nulls_and_extra_keys():
    out = BuilderOutput.model_validate({
        "name": {"first": "Sam", "last": None, "nickname": "S"},
        "contact": None, "skills": ["Python", "python", " C++ "], "surprise": 1,
        "education": [{"school": "X", "gpa": None, "honors": None}],
        "to_confirm": [{"field": "contact.phone", "question": "No phone number found. What is it?"}],
    })
    assert out.name.last == "" and out.contact.email == "" and out.skills == ["Python", "C++"]
    assert out.education[0].honors == [] and out.to_confirm[0].field == "contact.phone"


def test_schema_given_to_agents_is_plain():
    for model in (BuilderOutput, SearchReading):
        schema = agent_schema(model)
        text = json.dumps(schema)
        assert "$ref" not in text and "$defs" not in text
        assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])


# --- requirements ----------------------------------------------------------------------------


def test_requirements_file_round_trip():
    hard = clean_lines("- Must be an internship or entry level\n\n  * must be an internship or entry level \n2) In the US")
    assert hard == ["Must be an internship or entry level", "In the US"]
    text = format_requirements(hard, ["Prefer robotics"])
    assert text == ("## Hard requirements\n- Must be an internship or entry level\n- In the US\n\n"
                    "## Soft preferences\n- Prefer robotics\n")
    assert parse_requirements(text) == (hard, ["Prefer robotics"])
    assert parse_requirements("") == ([], [])


def test_requirements_limits():
    with pytest.raises(BadRequirements, match="too long"):
        clean_lines(["x" * 401])
    with pytest.raises(BadRequirements, match="40 lines"):
        clean_lines([f"line {n}" for n in range(41)])
    with pytest.raises(BadRequirements):
        clean_lines([1, 2])


def test_fingerprint_follows_meaning_not_formatting():
    assert fingerprint(["Must be remote"], ["Prefer robotics"]) == fingerprint(["  must be REMOTE "], ["prefer robotics"])
    assert fingerprint(["Must be remote"], ["Prefer robotics"]) != fingerprint(["Prefer robotics"], ["Must be remote"])


def test_an_agent_cannot_add_drop_reword_or_move_a_requirement():
    hard, soft = ["Must be an internship", "Must be in the US"], ["Prefer robotics", "Prefer $35 an hour"]
    from_agent = SearchReading.model_validate({
        "hard": [
            {"text": "must be an internship", "test": "Title says intern."},    # fine, different capitals
            {"text": "Prefer robotics", "test": "Mentions robots."},             # moved from soft: refused
            {"text": "Must have a pension plan", "test": "Invented."},           # invented: refused
        ],                                                                         # "Must be in the US" dropped: restored
        "soft": [{"text": "Prefer robotics", "weight": 3}, {"text": "Prefer forty dollars", "weight": 3}],
        "ambiguous": [
            {"text": "Prefer $35 an hour", "question": "Rule or wish?"},
            {"text": "Must be in the US", "question": "Already settled."},
            {"text": "Something the user never wrote", "question": "?"},
        ],
    })
    reading = reconcile(from_agent, hard, soft, settled=["Must be in the US"])
    assert [(h.text, h.test) for h in reading.hard] == [
        ("Must be an internship", "Title says intern."), ("Must be in the US", ""),
    ]
    assert [(s.text, s.weight) for s in reading.soft] == [("Prefer robotics", 3), ("Prefer $35 an hour", 2)]
    assert [a.text for a in reading.ambiguous] == ["Prefer $35 an hour"]


# --- instructions ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["profile_builder", "requirements_reader"])
def test_instruction_files_exist_and_carry_the_shared_rules(name):
    text = load_instructions(Paths(REPO), name)
    assert text.startswith("# Rules for every Job App agent")
    assert "Work only from what you are given" in text and "```json" in text


# --- the agent runner ----------------------------------------------------------------------------


def test_json_is_found_in_an_agents_reply():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('Here you go:\n```json\n{"a": {"b": [1, 2]}}\n```\nDone.') == {"a": {"b": [1, 2]}}
    assert parse_json('Sure. {"a": "x}y"} Hope that helps.') == {"a": "x}y"}
    for bad in ("", "no json here", "[1, 2, 3]", '{"unfinished": '):
        with pytest.raises(ValueError):
            parse_json(bad)


@pytest.mark.parametrize(
    "message, kind",
    [
        ("Claude Code returned an error result: Not logged in · Please run /login", "not_signed_in"),
        ("API Error: 401 authentication_error", "not_signed_in"),
        ("Claude usage limit reached. Your limit will reset at 9pm", "limit"),
        ("API Error: 429 rate_limit_error", "limit"),
        ("Command failed with exit code 1", "failed"),
    ],
)
def test_runtime_errors_are_sorted_into_kinds(message, kind):
    assert classify(message) == kind
    assert AgentError(kind).message  # every kind has words for the user


class FakeSdk:
    """Replaces the SDK's ``query`` with a scripted conversation, built from the SDK's own message types."""

    def __init__(self, monkeypatch, scripts):
        import claude_agent_sdk as sdk

        self.sdk, self.scripts, self.seen = sdk, list(scripts), []
        monkeypatch.setattr(sdk, "query", self.query)

    def result(self, *, structured=None, text=None, is_error=False, subtype="success"):
        return self.sdk.ResultMessage(
            subtype=subtype, duration_ms=1200, duration_api_ms=1000, is_error=is_error, num_turns=2,
            session_id="s", total_cost_usd=0.01, result=text, structured_output=structured,
        )

    def says(self, text):
        return self.sdk.AssistantMessage(content=[self.sdk.TextBlock(text=text)], model="claude-test")

    async def query(self, *, prompt, options):
        self.seen.append({"prompt": prompt, "options": options})
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        for message in script:
            yield message


def run_ask(tmp_path, **kw):
    """Run the runner in its own thread and event loop.

    The browser tests keep an event loop running in the main thread for the
    whole session, and asyncio.run refuses to start a second one there.
    """
    import threading

    paths = Paths(tmp_path)
    box = {}

    def work():
        try:
            box["value"] = asyncio.run(runner.ask_structured(
                paths, Settings(), name="t", instructions="Be exact.", prompt="Do it.", schema={"type": "object"},
                model="sonnet", **kw,
            ))
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            box["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=30)
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_structured_answer_is_used_and_logged(tmp_path, monkeypatch):
    sdk = FakeSdk(monkeypatch, [])
    sdk.scripts.append([sdk.says("working"), sdk.result(structured={"ok": True}, text="done")])
    reply = run_ask(tmp_path)
    assert reply.data == {"ok": True} and reply.path == "structured" and reply.cost_usd == 0.01
    options = sdk.seen[0]["options"]
    assert options.tools == [] and options.setting_sources == [] and options.model == "sonnet"
    assert options.output_format == {"type": "json_schema", "schema": {"type": "object"}}
    assert options.system_prompt == "Be exact." and options.max_turns == 6
    entry = json.loads((tmp_path / "data" / "logs" / "agents.log").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["ok"] is True and entry["agent"] == "t" and entry["attempts"][0]["path"] == "structured"
    assert "Do it." not in json.dumps(entry)  # what was asked is not written to the log


def test_plain_json_in_the_reply_is_accepted(tmp_path, monkeypatch):
    sdk = FakeSdk(monkeypatch, [])
    sdk.scripts.append([sdk.says('```json\n{"ok": 1}\n```'), sdk.result(text="")])
    reply = run_ask(tmp_path)
    assert reply.data == {"ok": 1} and reply.path == "text" and len(sdk.seen) == 1


def test_falls_back_to_asking_for_plain_json(tmp_path, monkeypatch):
    sdk = FakeSdk(monkeypatch, [])
    sdk.scripts.append([sdk.result(is_error=True, subtype="error_max_structured_output_retries", text="gave up")])
    sdk.scripts.append([sdk.says('{"ok": 2}'), sdk.result(text='{"ok": 2}')])
    reply = run_ask(tmp_path)
    assert reply.data == {"ok": 2} and reply.path == "text"
    assert sdk.seen[1]["options"].output_format is None
    assert sdk.seen[1]["prompt"].endswith("and nothing else.")


def test_not_signed_in_is_reported_without_a_second_try(tmp_path, monkeypatch):
    sdk = FakeSdk(monkeypatch, [RuntimeError("Claude Code returned an error result: Not logged in · Please run /login")])
    with pytest.raises(AgentError) as caught:
        run_ask(tmp_path)
    assert caught.value.kind == "not_signed_in" and cmd("signin") in caught.value.message
    assert len(sdk.seen) == 1
    entry = json.loads((tmp_path / "data" / "logs" / "agents.log").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["ok"] is False and entry["kind"] == "not_signed_in"


def test_an_answer_that_is_never_json_is_bad_output(tmp_path, monkeypatch):
    sdk = FakeSdk(monkeypatch, [])
    sdk.scripts.append([sdk.says("I cannot do that."), sdk.result(text="I cannot do that.")])
    sdk.scripts.append([sdk.says("Still no."), sdk.result(text="Still no.")])
    with pytest.raises(AgentError) as caught:
        run_ask(tmp_path)
    assert caught.value.kind == "bad_output" and len(sdk.seen) == 2


def test_a_slow_agent_times_out(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk

    async def never(*, prompt, options):
        await asyncio.sleep(30)
        yield None

    monkeypatch.setattr(sdk, "query", never)
    with pytest.raises(AgentError) as caught:
        run_ask(tmp_path, timeout=0.2)
    assert caught.value.kind == "timeout"


def test_api_key_is_ignored_when_using_the_plan_sign_in(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    sdk = FakeSdk(monkeypatch, [])
    sdk.scripts.append([sdk.result(structured={"ok": True})])
    run_ask(tmp_path)
    assert "ANTHROPIC_API_KEY" not in os.environ
