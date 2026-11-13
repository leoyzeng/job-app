"""Tracker file, duplicate detection, job lifecycle and settings."""

from __future__ import annotations

import json
import os
import time

import pytest

from app.core import lifecycle
from app.core.dedupe import BadUrl, company_key, job_id_for, make_dedupe_keys, normalize_url
from app.core.jsonfile import backup_path, read_json, write_json_atomic
from app.core.lifecycle import NotAllowed
from app.core.models import Job, Need, Settings, Status, StatusDetail
from app.core.settings import BadSettings, load_settings, update_settings
from app.core.store import DuplicateJob, TrackerStore, TrackerUnreadable


def make_job(url="https://jobs.example.com/acme/123", company="Acme Robotics", title="Robotics Intern", **kw):
    return Job(id="", company=company, title=title, url=url, **kw)


# --- links ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://www.Example.com/jobs/123/", "http://example.com/jobs/123"),
        ("https://example.com/jobs/123?utm_source=li&gh_src=abc", "https://example.com/jobs/123"),
        ("https://example.com/jobs?b=2&a=1", "https://example.com/jobs?a=1&b=2"),
        ("https://example.com:443/jobs/123#apply", "https://example.com/jobs/123"),
        ("https://example.com//jobs///123", "https://example.com/jobs/123"),
    ],
)
def test_same_posting_different_links(a, b):
    assert normalize_url(a) == normalize_url(b)


def test_job_identifier_in_query_is_kept():
    # Greenhouse-style links carry the job number in the query string.
    assert normalize_url("https://acme.com/careers?gh_jid=111") != normalize_url("https://acme.com/careers?gh_jid=222")


@pytest.mark.parametrize("bad", ["", "acme.com/jobs", "ftp://acme.com/x", "javascript:alert(1)", "https://"])
def test_bad_links_are_rejected(bad):
    with pytest.raises(BadUrl):
        normalize_url(bad)


def test_company_names_match_across_spellings():
    assert company_key("Acme Robotics, Inc.") == company_key("ACME Robotics")
    assert company_key("Café Münster GmbH") == "cafe munster"
    assert company_key("Co") == "co"  # never strip a name down to nothing


def test_dedupe_keys_and_id_are_stable():
    keys = make_dedupe_keys("https://www.example.com/j/1?utm_medium=x", "Acme, Inc.", "Robotics  Intern", "Boston, MA")
    assert keys == ["url:example.com/j/1", "key:acme|robotics intern|boston ma"]
    assert job_id_for("https://example.com/j/1") == job_id_for("https://www.example.com/j/1/")
    assert job_id_for("https://example.com/j/1").startswith("j_")


# --- json files ----------------------------------------------------------------


def test_atomic_write_keeps_previous_version(tmp_path):
    path = tmp_path / "x.json"
    write_json_atomic(path, {"n": 1})
    write_json_atomic(path, {"n": 2})
    assert read_json(path) == {"n": 2}
    assert read_json(backup_path(path)) == {"n": 1}
    assert [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name] == []


# --- tracker -------------------------------------------------------------------


def test_add_and_reload(tmp_path):
    store = TrackerStore(tmp_path / "tracker.json")
    job = store.add(make_job(location="Boston"))
    assert job.id.startswith("j_") and job.status == Status.NEW
    again = TrackerStore(tmp_path / "tracker.json")
    assert [j.id for j in again.all_jobs()] == [job.id]
    on_disk = json.loads((tmp_path / "tracker.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == 1 and job.id in on_disk["jobs"]


def test_skipped_job_never_comes_back(tmp_path):
    store = TrackerStore(tmp_path / "tracker.json")
    job = store.add(make_job())
    store.move(job.id, "skip")
    # Same posting through a tracking link ...
    with pytest.raises(DuplicateJob) as hit:
        store.add(make_job(url="https://www.jobs.example.com/acme/123/?utm_source=x"))
    assert hit.value.existing.status == Status.SKIPPED
    # ... and the same job found on another site.
    with pytest.raises(DuplicateJob):
        store.add(make_job(url="https://other-board.example.org/view/999", company="ACME Robotics Inc"))
    assert len(store.all_jobs()) == 1


def test_applied_job_never_comes_back(tmp_path):
    store = TrackerStore(tmp_path / "tracker.json")
    job = store.add(make_job())
    store.move(job.id, "mark_applied")
    with pytest.raises(DuplicateJob):
        store.add(make_job())


def test_hand_edit_is_picked_up(tmp_path):
    path = tmp_path / "tracker.json"
    store = TrackerStore(path)
    job = store.add(make_job())
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["jobs"][job.id]["location"] = "Edited by hand"
    raw["jobs"][job.id]["my_note"] = "kept"
    time.sleep(0.01)
    path.write_text(json.dumps(raw), encoding="utf-8")
    os.utime(path, None)
    assert store.get(job.id).location == "Edited by hand"
    store.move(job.id, "skip")
    assert json.loads(path.read_text(encoding="utf-8"))["jobs"][job.id]["my_note"] == "kept"


def test_damaged_tracker_recovers_from_backup(tmp_path):
    path = tmp_path / "tracker.json"
    store = TrackerStore(path)
    first = store.add(make_job())
    store.add(make_job(url="https://jobs.example.com/acme/456", title="Controls Intern"))
    path.write_text("{ this is not json", encoding="utf-8")
    recovered = TrackerStore(path)
    jobs = recovered.all_jobs()
    assert [j.id for j in jobs] == [first.id]  # the backup holds the state before the last write
    assert recovered.recovered_from_backup
    assert any(p.name.startswith("tracker.damaged-") for p in tmp_path.iterdir())


def test_damaged_tracker_without_backup_is_not_overwritten(tmp_path):
    path = tmp_path / "tracker.json"
    path.write_text("not json", encoding="utf-8")
    store = TrackerStore(path)
    with pytest.raises(TrackerUnreadable):
        store.all_jobs()
    assert path.read_text(encoding="utf-8") == "not json"


# --- lifecycle -----------------------------------------------------------------


def test_full_application_path():
    job = make_job()
    job.id = "j_1"
    for move, expected in [
        ("prepare", Status.QUEUED),
        ("start_filling", Status.FILLING),
        ("needs_input", Status.WAITING_FOR_INPUT),
        ("continue", Status.FILLING),
        ("page_done", Status.WAITING_FOR_PAGE_REVIEW),
        ("continue", Status.FILLING),
        ("ready_for_review", Status.WAITING_FOR_REVIEW),
        ("continue", Status.FILLING),
        ("ready_for_review", Status.WAITING_FOR_REVIEW),
        ("mark_applied", Status.APPLIED),
    ]:
        lifecycle.apply(job, move)
        assert job.status == expected
    assert [h.to for h in job.history][-1] == "applied"
    assert job.history[0].from_status == "new"
    assert job.to_json()["history"][0]["from"] == "new"  # stored under the plan's key


@pytest.mark.parametrize(
    "status, move",
    [
        (Status.APPLIED, "skip"),
        (Status.SKIPPED, "skip"),
        (Status.NEW, "continue"),
        (Status.FILLING, "prepare"),
        (Status.WAITING_FOR_INPUT, "prepare"),
        (Status.NEW, "restore"),
        (Status.APPLIED, "mark_applied"),
        (Status.NEW, "needs_input"),
        (Status.APPLIED, "posting_closed"),
        (Status.NEW, "no_such_move"),
    ],
)
def test_moves_that_make_no_sense_are_refused(status, move):
    job = make_job(status=status)
    with pytest.raises(NotAllowed):
        lifecycle.apply(job, move)
    assert job.status == status and job.history == []


@pytest.mark.parametrize("status", [s for s in Status if s != Status.APPLIED])
def test_mark_applied_works_from_anywhere(status):
    job = make_job(status=status)
    lifecycle.apply(job, "mark_applied")
    assert job.status == Status.APPLIED


@pytest.mark.parametrize("status", [Status.SKIPPED, Status.APPLIED, Status.CLOSED])
def test_restore_returns_to_new(status):
    job = make_job(status=status)
    lifecycle.apply(job, "restore")
    assert job.status == Status.NEW


def test_status_message_belongs_to_one_status():
    job = make_job(status=Status.FILLING)
    detail = StatusDetail(message="2 fields need you", needs=[Need(label="Salary expectation")])
    lifecycle.apply(job, "needs_input", by="agent", detail=detail)
    assert job.status_detail.needs[0].label == "Salary expectation"
    lifecycle.apply(job, "continue")
    assert job.status_detail.message == "" and job.status_detail.needs == []


def test_every_status_has_a_tab():
    assert set(lifecycle.GROUPS) == set(Status)


# --- settings ------------------------------------------------------------------


def test_settings_created_with_defaults(tmp_path):
    path = tmp_path / "config" / "settings.json"
    settings = load_settings(path)
    assert settings.pause_after_each_page is False and settings.write_long_answers is True
    assert settings.auth.mode == "subscription" and settings.browser.mode == "dedicated"
    assert json.loads(path.read_text(encoding="utf-8"))["pause_after_each_page"] is False


def test_settings_partial_update(tmp_path):
    path = tmp_path / "settings.json"
    update_settings(path, {"pause_after_each_page": True, "budget_usd": {"per_day": 3}})
    settings = load_settings(path)
    assert settings.pause_after_each_page is True
    assert settings.budget_usd.per_day == 3 and settings.budget_usd.per_search == 1.5


@pytest.mark.parametrize(
    "patch",
    [{"pause_after_each_page": "sometimes"}, {"nonsense": 1}, {"budget_usd": {"per_day": -1}}, {"browser": {"mode": "safari"}}],
)
def test_bad_settings_are_rejected_and_file_untouched(tmp_path, patch):
    path = tmp_path / "settings.json"
    load_settings(path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(BadSettings):
        update_settings(path, patch)
    assert path.read_text(encoding="utf-8") == before


def test_damaged_settings_fall_back_without_overwriting(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ oops", encoding="utf-8")
    assert load_settings(path) == Settings()
    assert path.read_text(encoding="utf-8") == "{ oops"
