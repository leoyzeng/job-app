"""The browser setting and the "open posting" request, through the real server."""

from __future__ import annotations

import json


def add_one(running):
    status, body, _ = running.add()
    assert status == 201
    return body["job"]["id"]


def test_postings_open_in_job_apps_window_by_default(running):
    job_id = add_one(running)
    assert running.state()["settings"]["browser"] == {"mode": "dedicated", "channel": "chrome"}
    status, body, _ = running.call("POST", f"/api/jobs/{job_id}/open")
    assert status == 200
    assert body["opened"] == {"where": "dedicated", "message": "Opened in Job App's browser window.", "loaded": True}
    assert running.browsers.opened == [("https://jobs.example.com/acme/1", "dedicated", "chrome")]


def test_choosing_your_own_chrome_is_saved_and_used(running):
    job_id = add_one(running)
    status, body, _ = running.call("PUT", "/api/settings", {"browser": {"mode": "own_chrome"}})
    assert status == 200 and body["settings"]["browser"]["mode"] == "own_chrome"
    saved = json.loads(running.paths.settings.read_text(encoding="utf-8"))
    # Only the mode changed; the other browser setting is kept.
    assert saved["browser"] == {"mode": "own_chrome", "channel": "chrome"}

    status, body, _ = running.call("POST", f"/api/jobs/{job_id}/open")
    assert status == 200 and body["opened"]["where"] == "own_chrome"
    assert running.browsers.opened[-1] == ("https://jobs.example.com/acme/1", "own_chrome", "chrome")


def test_a_one_off_choice_does_not_change_the_setting(running):
    job_id = add_one(running)
    status, body, _ = running.call("POST", f"/api/jobs/{job_id}/open", {"in": "own_chrome"})
    assert status == 200 and body["opened"]["where"] == "own_chrome"
    assert running.state()["settings"]["browser"]["mode"] == "dedicated"


def test_unknown_browser_choices_are_refused(running):
    job_id = add_one(running)
    status, body, _ = running.call("POST", f"/api/jobs/{job_id}/open", {"in": "claude_desktop"})
    assert status == 422
    status, body, _ = running.call("PUT", "/api/settings", {"browser": {"mode": "claude_desktop"}})
    assert status == 422 and "browser" in json.dumps(body["fields"])
    assert running.browsers.opened == []


def test_a_window_that_will_not_open_is_reported(running):
    job_id = add_one(running)
    running.browsers.fail_in = {"dedicated"}
    status, body, _ = running.call("POST", f"/api/jobs/{job_id}/open")
    assert status == 502
    assert body["error"] == "open_failed" and body["where"] == "dedicated"
    assert "could not be opened" in body["message"]


def test_opening_an_unknown_job(running):
    status, body, _ = running.call("POST", "/api/jobs/j_missing0/open")
    assert status == 404 and body["error"] == "unknown_job"


def test_opening_needs_the_session_cookie(running):
    job_id = add_one(running)
    status, _, _ = running.call("POST", f"/api/jobs/{job_id}/open", signed_in=False)
    assert status == 401
    # Another site cannot make the app open pages either.
    status, _, _ = running.call("POST", f"/api/jobs/{job_id}/open",
                                headers={"Origin": "https://evil.example"})
    assert status == 403
    assert running.browsers.opened == []


def test_opening_does_not_change_the_job(running):
    job_id = add_one(running)
    before = [j for j in running.state()["jobs"] if j["id"] == job_id][0]
    running.call("POST", f"/api/jobs/{job_id}/open")
    after = [j for j in running.state()["jobs"] if j["id"] == job_id][0]
    assert after["status"] == before["status"] == "new"
    assert after["history"] == before["history"]


def test_state_says_what_the_browsers_look_like(running):
    assert running.state()["browser"] == {"window_open": False, "window_browser": None, "chrome_found": True}
