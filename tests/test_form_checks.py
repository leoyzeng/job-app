"""The check command's form engine tests, run here against the practice forms."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading

import pytest

from app.checks import form_checks
from app.core.models import Settings
from app.paths import Paths

from conftest import REPO


def run_async(make_coro, timeout=180):
    box = {}

    def work():
        try:
            box["v"] = asyncio.run(make_coro())
        except BaseException as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout)
    if "e" in box:
        raise box["e"]
    return box["v"]


@pytest.fixture
def paths(tmp_path):
    pytest.importorskip("playwright.async_api")
    shutil.copytree(REPO / "ui", tmp_path / "ui")
    p = Paths(tmp_path)
    p.ensure()
    return p


def test_the_practice_form_check(paths):
    settings = Settings.model_validate({"browser": {"channel": "chromium"}})
    result = run_async(lambda: form_checks.practice_check(paths, settings, headless=True))
    assert result.status == "ok", result.details
    assert result.summary.startswith("Form engine: filled and checked 62 fields of every kind on 5 practice forms")
    assert result.details["change_found"] == [["First Name", "changed"]] or result.details["change_found"] == [("First Name", "changed")]
    assert not (paths.data / "check_files" / "practice-resume.pdf").exists()


def test_the_real_form_check_reads_and_saves_samples(paths, monkeypatch):
    async def scenario():
        site = form_checks.PracticeSite(paths.ui / "practice")
        async with site:
            async def targets(_paths):
                return [("greenhouse", "practice", f"{site.base}/embedded.html"),
                        ("lever", "practice", f"{site.base}/lever.html")]

            monkeypatch.setattr(form_checks, "_application_urls", targets)
            # The stand-in sites are on this PC, which the real check never reads.
            from app.search import pages

            async def allow(route):
                await route.continue_()

            monkeypatch.setattr(pages, "guard_route", allow)
            return await form_checks.real_forms_check(paths, Settings.model_validate({"browser": {"channel": "chromium"}}))

    result = run_async(scenario)
    assert result.status == "ok", result.details
    assert "Greenhouse 25 fields; Lever 19 fields" in result.summary and "read only" in result.summary
    assert result.details["lever"]["submit_found"] is True
    saved = json.loads((paths.logs / "form_samples" / "lever.json").read_text(encoding="utf-8"))
    assert saved["fields"][0]["label"] == "Resume/CV"
    html = (paths.logs / "form_samples" / "lever.html").read_text(encoding="utf-8")
    assert "<script" not in html and "Full name" in html
    assert "First Name" in (paths.logs / "form_samples" / "greenhouse-frame.html").read_text(encoding="utf-8")


def test_the_real_form_check_without_feeds_is_only_a_note(paths, monkeypatch):
    async def none(_paths):
        return []

    monkeypatch.setattr(form_checks, "_application_urls", none)
    result = run_async(lambda: form_checks.real_forms_check(paths, Settings()))
    assert result.status == "warn" and result.summary == "Could not find a real application form to read."


def test_the_autofill_check_plumbing_with_the_stand_in_agent(paths, monkeypatch):
    """The real agent needs a Claude sign-in, so here the rule-based stand-in takes its place."""
    from app.apply import agent as agent_module
    from app.checks import apply_checks

    shutil.copytree(REPO / "prompts", paths.root / "prompts")
    monkeypatch.setattr(agent_module, "real_application_agent", lambda paths, load: agent_module.rule_based_agent)
    settings = Settings.model_validate({"browser": {"channel": "chromium"}, "write_long_answers": False})
    result = run_async(lambda: apply_checks.autofill_check(paths, settings, headless=True))
    assert result.status == "ok", result.details
    assert "Nothing was submitted" in result.summary and result.details["outcome"] == "needs_input"
    assert result.details["submitted"] is False
    assert "made-up sample person" in result.details["facts"]
    labels = {n["label"] for n in result.details["needs"]}
    assert "Are you legally authorized to work in the United States?" in labels
