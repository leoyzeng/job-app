"""What the public version adds: per-user data folders, each user's own sign-in, privacy and update helpers."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from app import paths as paths_module
from app.agents import auth
from app.agents.runner import AgentError, build_options
from app.commands import cmd
from app.core import diagnostics, migrate, updates
from app.core.models import Settings
from app.paths import APP_ROOT, Paths, default_paths, user_data_root

KEY = "sk-ant-api03-" + "x" * 40


@pytest.fixture
def no_store(monkeypatch):
    """No system password store and no key in the environment: the key goes to the private file."""
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(auth, "_ORIGINAL_ENV", {"ANTHROPIC_API_KEY": None, "ANTHROPIC_AUTH_TOKEN": None})


# --- where things live ------------------------------------------------------------------------


def test_your_data_lives_in_a_per_user_folder_apart_from_the_program(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBAPP_HOME", str(tmp_path / "mine"))
    p = default_paths()
    assert p.root == (tmp_path / "mine").resolve()
    assert p.profile == p.root / "profile" and p.tracker == p.root / "data" / "tracker.json"
    assert p.settings == p.root / "config" / "settings.json"
    # The program's own files come from the folder you cloned.
    assert p.prompts == APP_ROOT / "prompts" and p.ui == APP_ROOT / "ui"
    # Tests (and anyone passing one folder) still get everything in that folder.
    assert Paths(tmp_path).prompts == tmp_path / "prompts"


def test_the_default_data_folder_follows_each_system(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBAPP_HOME", raising=False)
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert user_data_root() == tmp_path / "Local" / "JobApp"
    monkeypatch.setattr(paths_module.sys, "platform", "darwin")
    assert user_data_root() == Path.home() / "Library" / "Application Support" / "JobApp"
    monkeypatch.setattr(paths_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert user_data_root() == tmp_path / "share" / "job-app"


def test_commands_are_named_the_way_this_system_runs_them():
    expected = "jobapp signin" if sys.platform == "win32" else "./jobapp.sh signin"
    assert cmd("signin") == expected
    assert cmd() in ("jobapp", "./jobapp.sh")


# --- each user's own sign-in ----------------------------------------------------------------


def test_an_api_key_is_kept_privately_and_handed_only_to_the_agents(tmp_path, no_store):
    p = Paths(tmp_path)
    settings = Settings.model_validate({"auth": {"mode": "api_key"}})
    with pytest.raises(AgentError) as caught:
        build_options(p, settings, model="sonnet")
    assert caught.value.kind == "not_signed_in"

    assert auth.save_api_key(p.config, KEY) == "file"
    stored = p.config / auth.KEY_FILE_NAME
    assert stored.read_text(encoding="utf-8").strip() == KEY
    if os.name == "posix":
        assert stored.stat().st_mode & 0o077 == 0  # readable by you only
    options, _ = build_options(p, settings, model="sonnet")
    assert options.env == {"ANTHROPIC_API_KEY": KEY}
    # Never in the settings file.
    assert KEY not in (p.settings.read_text(encoding="utf-8") if p.settings.exists() else "")

    ready = auth.agents_ready(p.config, settings)
    assert ready["signed_in"] and ready["source"] == "file" and ready["key_looks_right"]
    assert KEY not in json.dumps(ready)

    auth.forget_api_key(p.config)
    assert not stored.exists()
    assert auth.agents_ready(p.config, settings)["signed_in"] is False


def test_with_the_plan_sign_in_no_key_reaches_the_agents(tmp_path, no_store, monkeypatch):
    p = Paths(tmp_path)
    auth.save_api_key(p.config, KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used-" + "y" * 20)
    options, _ = build_options(p, Settings(), model="sonnet")
    assert options.env == {}
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_a_key_in_the_environment_wins_in_api_key_mode(tmp_path, no_store, monkeypatch):
    p = Paths(tmp_path)
    auth.save_api_key(p.config, KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-" + "z" * 30)
    key, source = auth.find_api_key(p.config)
    assert source == "environment" and key.startswith("sk-ant-from-env-")


def test_the_password_store_is_used_when_there_is_one(tmp_path, monkeypatch):
    class Store:
        saved = {}

        def set_password(self, service, account, value):
            self.saved[(service, account)] = value

        def get_password(self, service, account):
            return self.saved.get((service, account))

        def delete_password(self, service, account):
            self.saved.pop((service, account), None)

    store = Store()
    monkeypatch.setattr(auth, "_keyring", lambda: store)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(auth, "_ORIGINAL_ENV", {"ANTHROPIC_API_KEY": None, "ANTHROPIC_AUTH_TOKEN": None})
    p = Paths(tmp_path)
    assert auth.save_api_key(p.config, KEY) == "password store"
    assert not (p.config / auth.KEY_FILE_NAME).exists()
    assert auth.find_api_key(p.config) == (KEY, "password store")
    auth.forget_api_key(p.config)
    assert auth.find_api_key(p.config) == (None, "")


def test_api_keys_are_recognised_by_their_shape():
    assert auth.looks_like_api_key(KEY)
    assert not auth.looks_like_api_key("sk-ant-short")
    assert not auth.looks_like_api_key("hello there " + KEY)
    assert not auth.looks_like_api_key("")


def test_the_status_line_never_contains_the_key(tmp_path, no_store):
    from app.checks.signin import describe

    p = Paths(tmp_path)
    auth.save_api_key(p.config, KEY)
    text = describe(auth.agents_ready(p.config, Settings.model_validate({"auth": {"mode": "api_key"}})))
    assert "own API key" in text and KEY not in text


# --- the runtime's transcripts --------------------------------------------------------------


def test_only_job_apps_own_transcripts_are_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    agent_cwd = Paths(tmp_path / "home").agent_cwd
    agent_cwd.mkdir(parents=True)
    ours = auth.transcript_folder(agent_cwd)
    assert ours.parent == tmp_path / "claude" / "projects" and ours.name.endswith("agent-workdir")
    ours.mkdir(parents=True)
    (ours / "old.jsonl").write_text("{}", encoding="utf-8")
    fresh = ours / "running.jsonl"
    fresh.write_text("{}", encoding="utf-8")
    old_time = time.time() - 3600
    os.utime(ours / "old.jsonl", (old_time, old_time))
    someone_else = tmp_path / "claude" / "projects" / "-Users-sam-code-other"
    someone_else.mkdir(parents=True)
    (someone_else / "keep.jsonl").write_text("{}", encoding="utf-8")

    assert auth.forget_transcripts(agent_cwd, older_than_seconds=900) == 1
    assert fresh.exists() and not (ours / "old.jsonl").exists()
    assert auth.forget_transcripts(agent_cwd) == 1
    assert not fresh.exists()
    assert (someone_else / "keep.jsonl").exists()


# --- new versions -----------------------------------------------------------------------------


def test_the_repository_is_read_from_the_clone(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:someone/job-app.git\n'
        '\tfetch = +refs/heads/*:refs/remotes/origin/*\n', encoding="utf-8")
    assert updates.repository(tmp_path) == "someone/job-app"
    (tmp_path / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/a-b/c.d\n',
                                              encoding="utf-8")
    assert updates.repository(tmp_path) == "a-b/c.d"
    assert updates.repository(tmp_path / "nowhere") is None


def test_a_newer_release_is_noticed_at_most_once_a_day(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/o/r\n', encoding="utf-8")
    calls = []

    def fetch(repo):
        calls.append(repo)
        return {"tag": "v99.0.0", "url": "https://github.com/o/r/releases/tag/v99.0.0"}

    cache = tmp_path / "update_check.json"
    found = updates.check(cache, tmp_path, fetch=fetch, now=1_000_000)
    assert found["tag"] == "v99.0.0" and calls == ["o/r"]
    assert updates.check(cache, tmp_path, fetch=fetch, now=1_000_000 + 3600)["tag"] == "v99.0.0"
    assert calls == ["o/r"]  # not asked again within a day
    updates.check(cache, tmp_path, fetch=fetch, now=1_000_000 + updates.DAY + 1)
    assert len(calls) == 2
    assert updates.newer("v0.10.0", "0.9.0") and not updates.newer("v0.9.0", "0.9.0")


def test_no_repository_or_no_network_means_no_notice(tmp_path):
    def offline(repo):
        raise OSError("offline")

    assert updates.check(tmp_path / "c.json", tmp_path, fetch=offline, now=5) is None
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/o/r\n', encoding="utf-8")
    assert updates.check(tmp_path / "c.json", tmp_path, fetch=offline, now=5) is None


# --- moving in, and leaving ---------------------------------------------------------------------


def _old_folder(tmp_path: Path) -> Path:
    old = tmp_path / "old_job_app"
    (old / "profile").mkdir(parents=True)
    (old / "profile" / "resume.pdf").write_bytes(b"%PDF-1.4 sample")
    (old / "data" / "agent_workdir").mkdir(parents=True)
    (old / "data" / "agent_workdir" / "scratch.txt").write_text("x", encoding="utf-8")
    (old / "data" / "tracker.json").write_text('{"jobs": []}', encoding="utf-8")
    (old / "data" / ".session_token").write_text("t", encoding="utf-8")
    (old / "config").mkdir()
    (old / "config" / "settings.json").write_text("{}", encoding="utf-8")
    return old


def test_data_from_an_older_folder_is_copied_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOBAPP_HOME", str(tmp_path / "new"))
    old = _old_folder(tmp_path)
    assert migrate.import_old_folder(str(old)) == 0
    new = tmp_path / "new"
    assert (new / "profile" / "resume.pdf").read_bytes() == b"%PDF-1.4 sample"
    assert (new / "data" / "tracker.json").exists() and (new / "config" / "settings.json").exists()
    assert not (new / "data" / "agent_workdir").exists() and not (new / "data" / ".session_token").exists()
    assert (old / "profile" / "resume.pdf").exists()  # the old folder is untouched
    # A second import would overwrite what is there now, so it is refused.
    assert migrate.import_old_folder(str(old)) == 1
    assert "already has a profile" in capsys.readouterr().out


def test_forgetting_removes_only_job_apps_folders(tmp_path, monkeypatch, no_store):
    monkeypatch.setenv("JOBAPP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    p = default_paths()
    p.ensure()
    (p.profile / "resume.pdf").write_bytes(b"x")
    auth.save_api_key(p.config, KEY)
    (p.root / "something-else.txt").write_text("not ours", encoding="utf-8")
    assert migrate.forget_everything(assume_yes=True) == 0
    assert not p.profile.exists() and not p.data.exists() and not p.config.exists()
    assert (p.root / "something-else.txt").exists()


# --- bug reports -------------------------------------------------------------------------------


def test_diagnostics_hold_nothing_personal(tmp_path):
    settings = Settings.model_validate({"search": {"watchlist": ["Secret Robotics Inc"]}})
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agents.log").write_text(
        json.dumps({"at": "2026-11-12T10:00:00", "agent": "fit_evaluator", "ok": False, "kind": "timeout",
                    "attempts": [{"error": "posting at https://jobs.example.com/secret"}]}) + "\n", encoding="utf-8")
    text = diagnostics.collect(settings=settings, logs=logs, signed_in=True, counts={"new": 3},
                               browsers={"Chrome": True, "Edge": False}, demo=False)
    assert "fit_evaluator: timeout" in text and "watchlist_size=1" in text and "new 3" in text
    assert "Secret Robotics" not in text and "example.com" not in text


def test_the_dashboard_offers_diagnostics_and_the_data_folder(running, monkeypatch):
    from app import window

    opened = []
    monkeypatch.setattr(window, "open_folder", lambda folder: opened.append(folder) or True)
    status, body, _ = running.call("GET", "/api/diagnostics")
    assert status == 200 and body["text"].startswith("Job App ")
    status, body, _ = running.call("POST", "/api/data/open")
    assert status == 200 and opened == [running.paths.root]
    app_info = running.state()["app"]
    assert app_info["data_folder"] == str(running.paths.root)
    assert app_info["commands"]["signin"] == cmd("signin")
