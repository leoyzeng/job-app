"""The local server: who may talk to it, and what each request does."""

from __future__ import annotations

import json
import threading
import urllib.request

from app.server.app import COOKIE, load_or_create_token


# --- who may talk to it ----------------------------------------------------------


def test_health_needs_no_sign_in(running):
    status, body, _ = running.call("GET", "/api/health", signed_in=False)
    assert status == 200 and body["app"] == "job-app" and body["demo"] is False


def test_data_is_refused_without_the_session_cookie(running):
    status, body, _ = running.call("GET", "/api/state", signed_in=False)
    assert status == 401 and body["error"] == "not_connected"
    status, _, _ = running.call("POST", "/api/jobs", {"url": "https://x.example/1"}, signed_in=False)
    assert status == 401
    assert not running.paths.tracker.exists()


def test_dashboard_page_needs_the_token(running):
    assert running.call("GET", "/", signed_in=False)[0] == 401
    assert running.call("GET", "/?t=wrong", signed_in=False)[0] == 401
    status, _, headers = running.call("GET", f"/?t={running.token}", signed_in=False)
    assert status == 303 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}={running.token}")
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    status, page, _ = running.call("GET", "/")
    assert status == 200 and "<title>Job App</title>" in page


def test_requests_under_another_host_name_are_refused(running):
    status, _, _ = running.call("GET", "/api/state", headers={"Host": "evil.example"})
    assert status == 400


def test_requests_sent_by_another_site_are_refused(running):
    status, _, _ = running.call("POST", "/api/jobs", {"url": "https://x.example/1", "company": "X", "title": "Y"},
                                headers={"Origin": "https://evil.example"})
    assert status == 403
    assert running.state()["jobs"] == []


def test_same_origin_requests_are_accepted(running):
    status, _, _ = running.add()
    assert status == 201
    status, _, _ = running.call("POST", "/api/jobs",
                                {"url": "https://x.example/2", "company": "X", "title": "Y"},
                                headers={"Origin": running.base})
    assert status == 201


def test_token_is_created_once_and_reused(tmp_path):
    path = tmp_path / "data" / ".session_token"
    first = load_or_create_token(path)
    assert len(first) >= 32 and load_or_create_token(path) == first


def test_static_files_are_always_rechecked(running):
    status, body, headers = running.call("GET", "/static/app.js", signed_in=False)
    assert status == 200 and "Job App dashboard" in body
    assert headers["cache-control"] == "no-cache"


# --- adding jobs -----------------------------------------------------------------


def test_add_a_job(running):
    status, body, _ = running.add(location="Boston, MA", pay="$40 an hour",
                                  description="First line.\r\n\r\n\r\n\r\nSecond   line.")
    assert status == 201
    job = body["job"]
    assert job["status"] == "new" and job["group"] == "new"
    assert job["details"]["pay"] == "$40 an hour"
    assert job["description_summary"] == "First line.\n\nSecond line."
    assert job["source"]["found_via"] == "added by you"
    assert job["actions"]["skip"]["enabled"] and job["actions"]["applied"]["enabled"]
    on_disk = json.loads(running.paths.tracker.read_text(encoding="utf-8"))
    assert on_disk["jobs"][job["id"]]["company"] == "Acme Robotics"
    state = running.state()
    assert state["counts"] == {"new": 1, "in_progress": 0, "waiting": 0, "applied": 0, "hidden": 0}


def test_add_reports_each_field_problem_in_plain_words(running):
    status, body, _ = running.call("POST", "/api/jobs", {"url": "acme.com/jobs/1", "company": "  ", "title": ""})
    assert status == 422
    # With a real link the app reads the company and title itself, so only the link is asked for.
    assert body["fields"] == {"url": "Enter a full link that starts with https://"}
    # A page that cannot be read: now the company and title are needed.
    status, body, _ = running.call("POST", "/api/jobs", {"url": "https://jobs.example.com/gone/1"})
    assert status == 422
    assert body["fields"] == {"company": "Enter the company name.", "title": "Enter the job title."}
    assert "no longer exists" in body["message"] and "Fill in the details yourself" in body["message"]
    status, body, _ = running.call("POST", "/api/jobs", {"company": "Acme"})
    assert status == 422 and body["fields"]["url"] == "This is needed."
    status, body, _ = running.call("POST", "/api/jobs", ["not", "an", "object"])
    assert status == 422
    assert running.state()["jobs"] == []


def test_the_same_job_cannot_be_added_twice(running):
    running.add()
    status, body, _ = running.add(url="https://www.jobs.example.com/acme/1/?utm_source=newsletter")
    assert status == 409 and body["error"] == "duplicate"
    assert body["message"] == "Robotics Intern at Acme Robotics is already in your tracker: it is under New."
    assert len(running.state()["jobs"]) == 1


# --- buttons -----------------------------------------------------------------------


def test_skip_hides_the_job_for_good(running):
    job = running.add()[1]["job"]
    status, body, _ = running.call("POST", f"/api/jobs/{job['id']}/skip")
    assert status == 200 and body["job"]["status"] == "skipped" and body["job"]["group"] == "hidden"
    assert running.state()["counts"]["new"] == 0 and running.state()["counts"]["hidden"] == 1
    status, body, _ = running.add()
    assert status == 409 and "you skipped it" in body["message"]


def test_skip_can_be_undone(running):
    job = running.add()[1]["job"]
    running.call("POST", f"/api/jobs/{job['id']}/skip")
    status, body, _ = running.call("POST", f"/api/jobs/{job['id']}/restore")
    assert status == 200 and body["job"]["status"] == "new"
    history = body["job"]["history"]
    assert [(h["from"], h["to"]) for h in history] == [("new", "skipped"), ("skipped", "new")]


def test_mark_as_applied(running):
    job = running.add()[1]["job"]
    status, body, _ = running.call("POST", f"/api/jobs/{job['id']}/applied")
    assert status == 200 and body["job"]["status"] == "applied" and body["job"]["group"] == "applied"
    assert body["job"]["actions"]["skip"]["applicable"] is False
    status, body, _ = running.call("POST", f"/api/jobs/{job['id']}/skip")
    assert status == 409 and body["error"] == "not_allowed"
    status, body, _ = running.add()
    assert status == 409 and "you applied to it" in body["message"]


def test_prepare_needs_a_confirmed_profile_and_job_apps_window(running):
    job = running.add()[1]["job"]
    assert job["actions"]["prepare"] == {"applicable": True, "enabled": True, "reason": ""}
    assert job["actions"]["continue"]["applicable"] is False
    status, body, _ = running.call("POST", f"/api/jobs/{job['id']}/prepare")
    assert status == 409 and body["error"] == "refused" and "Confirm your profile" in body["message"]
    running.ready_profile()
    status, body, _ = running.call("POST", f"/api/jobs/{job['id']}/prepare")
    assert status == 409 and "needs Job App's browser window" in body["message"]
    status, body, _ = running.call("POST", f"/api/jobs/{job['id']}/continue")
    assert status == 409 and body["error"] == "not_allowed"
    assert running.state()["jobs"][0]["status"] == "new"


def test_unknown_job_or_button(running):
    assert running.call("POST", "/api/jobs/j_missing/skip")[0] == 404
    job = running.add()[1]["job"]
    assert running.call("POST", f"/api/jobs/{job['id']}/explode")[0] == 404


# --- settings ----------------------------------------------------------------------


def test_settings_are_saved_to_the_file(running):
    state = running.state()
    assert state["settings"]["pause_after_each_page"] is False
    assert state["settings"]["write_long_answers"] is True
    assert state["settings"]["auth"]["mode"] == "subscription"
    status, body, _ = running.call("PUT", "/api/settings", {"pause_after_each_page": True})
    assert status == 200 and body["settings"]["pause_after_each_page"] is True
    saved = json.loads(running.paths.settings.read_text(encoding="utf-8"))
    assert saved["pause_after_each_page"] is True and saved["write_long_answers"] is True


def test_bad_settings_are_refused(running):
    for patch in ({"pause_after_each_page": "maybe"}, {"made_up": 1}, None, []):
        status, body, _ = running.call("PUT", "/api/settings", patch)
        assert status == 422, patch
    assert running.state()["settings"]["pause_after_each_page"] is False


# --- live updates ------------------------------------------------------------------


def test_open_dashboards_hear_about_changes(running):
    request = urllib.request.Request(running.base + "/api/events")
    request.add_header("Cookie", f"{COOKIE}={running.token}")
    stream = urllib.request.urlopen(request, timeout=5)
    assert stream.readline().decode().startswith(": connected")
    stream.readline()  # blank line that ends the comment

    heard = []

    def listen():
        for raw in stream:
            line = raw.decode().strip()
            if line.startswith("data: "):
                heard.append(json.loads(line[6:]))
                return

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    job = running.add()[1]["job"]
    listener.join(timeout=5)
    stream.close()
    assert heard == [{"type": "jobs_changed", "job_id": job["id"]}]
