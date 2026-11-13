"""Whole searches through the real server, against the made-up web and stand-in agents."""

from __future__ import annotations

import json

from app.commands import cmd
from app.agents.runner import AgentError
from app.search.demo import GH, LIST


def listed(state):
    return sorted((j["company"], j["title"]) for j in state["jobs"])


def outcomes(report, kind):
    return sorted(o["label"] for o in report["outcomes"] if o["outcome"] == kind)


def test_search_needs_a_confirmed_profile(running):
    status, body, _ = running.call("POST", "/api/search")
    assert status == 409 and "Finish the Profile and requirements screen first" in body["message"]
    assert running.state()["search"]["running"] is False


def test_a_search_lists_what_fits_and_explains_the_rest(running):
    running.ready_profile()
    state = running.search()
    assert listed(state) == [
        ("Harborlight Systems", "Controls Software Intern"),
        ("Kestrel Robotics", "Perception Intern (Summer 2027)"),
        ("Kestrel Robotics", "Robotics Software Intern, Summer 2027"),
        ("Northwind AI", "Software Engineering Intern"),
    ]
    last = state["search"]["last"]
    assert last["status"] == "done" and last["message"] == "Search finished. Listed 4 new jobs. 1 did not meet your hard requirements."

    report = running.report()
    reasons = {f["reason"]: f["examples"] for f in report["filtered"]}
    assert reasons == {
        "title is not the kind of job you want": ["Senior Robotics Engineer at Kestrel Robotics"],
        "title names 2026": ["Embedded Firmware Intern - Summer 2026 at Kestrel Robotics"],
        "outside the US": ["Machine Learning Intern at Northwind AI"],
    }
    rejected = [o for o in report["outcomes"] if o["outcome"] == "rejected"]
    assert len(rejected) == 1 and rejected[0]["label"] == "Autonomy Engineering Intern at Tidewater"
    assert "Must be an internship or entry level: Asks for 5+ years of experience." == rejected[0]["reason"]
    assert outcomes(report, "closed") == ["Robotics Intern at Quayside Robotics"]
    sources = {s["name"]: s for s in report["sources"]}
    assert sources["Company job boards"]["count"] == 7 and sources["Company job boards"]["detail"] == "3 of 3 boards read."
    assert sources["Summer 2027 internship list (Simplify, GitHub)"]["count"] == 3
    assert sources["Web search"]["count"] == 1
    assert report["counts"]["repeat"] == 2  # the same jobs reached through the list and the web search
    assert [a["agent"] for a in report["agents"]][:1] == ["search_planner"]

    # The listings carry the verdict, the evidence and where they came from.
    jobs = {j["title"]: j for j in state["jobs"]}
    robot = jobs["Robotics Software Intern, Summer 2027"]
    assert robot["status"] == "new" and robot["source"]["kind"] == "greenhouse"
    assert robot["source"]["found_via"] == "Kestrel Robotics careers (Greenhouse)"
    assert [h["result"] for h in robot["match"]["hard"]] == ["pass", "pass"]
    assert robot["description_summary"].startswith("Sample summary:")
    # Soft preferences are scored by code from the evaluator's met / not met per line, with your weights.
    assert robot["match"]["soft_points"] > 0 and robot["match"]["soft_score"] > 50
    assert any("robot" in hit.lower() for hit in robot["match"]["soft_hits"])
    # The resume match only keeps skills that are both in your profile and named in the posting.
    from app.profile.demo import SAMPLE_FACTS
    assert robot["match"]["resume_level"] in ("some", "strong")
    assert set(robot["match"]["resume_skills"]) <= set(SAMPLE_FACTS["skills"]) and robot["match"]["resume_skills"]
    assert "Halden Robotics" in robot["match"]["resume_note"]
    harbor = jobs["Controls Software Intern"]
    assert harbor["source"]["kind"] == "list" and harbor["source"]["legit"] == "employer"
    assert "The list says: Does Not Offer Sponsorship" in harbor["match"]["concerns"]
    # The posting's text is kept for later.
    saved = (running.paths.data / "postings" / f"{robot['id']}.md").read_text(encoding="utf-8")
    assert "Write ROS 2 nodes" in saved
    # Which agents ran, and what they were asked.
    names = [c["name"] for c in running.agents.calls]
    assert names.count("search_planner") == 1 and "search_agent" in names and names.count("fit_evaluator") == 2
    plan_request = json.loads(running.agents.calls[names.index("search_planner")]["prompt"])
    assert plan_request["hard"][0]["text"] == "Must be an internship or entry level"


def test_a_second_search_does_not_repeat_or_pay_twice(running):
    running.ready_profile()
    running.search()
    first_calls = len(running.agents.calls)
    jobs = running.state()["jobs"]
    skipped = jobs[0]
    running.call("POST", f"/api/jobs/{skipped['id']}/skip")
    running.web.requests.clear()
    state = running.search()
    assert len(state["jobs"]) == 4 and [j for j in state["jobs"] if j["id"] == skipped["id"]][0]["status"] == "skipped"
    report = running.report()
    assert report["counts"].get("listed", 0) == 0
    assert report["counts"]["already_listed"] == 4 and report["counts"]["judged_before"] == 2
    assert report["plan_reused"] is True
    # Only the web agent ran again: no planner, no evaluator, and no board look-ups.
    assert [c["name"] for c in running.agents.calls[first_calls:]] == ["search_agent"]
    assert not any(r.startswith(f"{GH}/kestrelrobotics") and "jobs" not in r for r in running.web.requests)


def test_changed_requirements_mean_rejected_jobs_are_judged_again(running):
    running.ready_profile()
    running.search()
    reading_path = running.paths.profile / "search_profile.json"
    data = json.loads(reading_path.read_text(encoding="utf-8"))
    data["reading"]["soft"][0]["weight"] = 3
    reading_path.write_text(json.dumps(data), encoding="utf-8")
    running.search()
    report = running.report()
    assert report["plan_reused"] is False
    assert outcomes(report, "rejected") == ["Autonomy Engineering Intern at Tidewater"]  # judged again
    assert report["counts"]["judged_before"] == 1  # the closed posting is still remembered


def test_strict_mode_hides_jobs_that_cannot_be_checked(running):
    running.ready_profile()
    running.call("PUT", "/api/settings", {"search": {"strict_hard_requirements": True}})

    def unsure(request):
        return {"results": [{"key": p["key"], "hard": [{"requirement": h["text"], "result": "unknown", "evidence": ""}
                                                       for h in request["hard"]],
                             "soft_score": 50, "soft_hits": [], "soft_misses": [], "concerns": [], "summary": "s",
                             "closed": False, "scam_signs": []} for p in request["postings"]]}

    running.agents.override["fit_evaluator"] = unsure
    state = running.search()
    assert state["jobs"] == []
    report = running.report()
    assert report["counts"]["rejected"] == 5 and all(
        o["reason"].startswith("Could not verify:") for o in report["outcomes"] if o["outcome"] == "rejected")


def test_unverifiable_jobs_are_listed_with_a_note_by_default(running):
    running.ready_profile()

    def unsure(request):
        return {"results": [{"key": p["key"], "hard": [{"requirement": h["text"], "result": "unknown", "evidence": ""}
                                                       for h in request["hard"]],
                             "soft_score": 50, "soft_hits": [], "soft_misses": [], "concerns": [], "summary": "s",
                             "closed": False, "scam_signs": []} for p in request["postings"]]}

    running.agents.override["fit_evaluator"] = unsure
    state = running.search()
    assert len(state["jobs"]) == 5
    assert all(j["match"]["unverified"] == ["Must be an internship or entry level", "Must be in the United States"]
               for j in state["jobs"])


def test_the_budget_and_the_listing_cap_are_kept(running):
    running.ready_profile()
    running.call("PUT", "/api/settings", {"budget_usd": {"per_search": 0.05}})
    running.agents.cost = 0.04  # each agent run "costs" this much
    state = running.search()
    report = running.report()
    # Planner (0.04) + web agent (0.04) already pass the budget, so nothing is judged.
    assert state["jobs"] == [] and any("search budget" in n for n in report["notes"])
    assert report["cost_usd"] == 0.08

    running.call("PUT", "/api/settings", {"budget_usd": {"per_search": 10}, "search": {"max_new_per_search": 2}})
    running.agents.cost = None
    state = running.search()
    assert len(state["jobs"]) == 2


def test_sources_can_be_switched_off(running):
    running.ready_profile()
    running.call("PUT", "/api/settings", {"search": {"sources": {"boards": False, "web": False}}})
    running.search()
    report = running.report()
    assert [s["name"] for s in report["sources"]] == ["Summer 2027 internship list (Simplify, GitHub)"]
    assert not any(r.startswith("https://api.lever.co") for r in running.web.requests)
    running.call("PUT", "/api/settings", {"search": {"sources": {"lists": False}}})
    status, body, _ = running.call("POST", "/api/search")
    assert status == 409 and "Every source is switched off" in body["message"]


def test_a_source_that_fails_does_not_stop_the_search(running):
    running.ready_profile()
    del running.web.routes[LIST]
    state = running.search()
    report = running.report()
    lists = [s for s in report["sources"] if s["name"].startswith("Summer 2027")][0]
    assert lists["ok"] is False and "404" in lists["detail"]
    assert len(state["jobs"]) == 3  # the boards and the web search still worked


def test_not_signed_in_stops_the_search_and_says_why(running):
    running.ready_profile()
    running.agents.fail_with = AgentError("not_signed_in")
    state = running.search()
    assert state["search"]["last"]["status"] == "failed"
    assert f"Run '{cmd('signin')}'" in state["search"]["last"]["message"]
    assert state["agents"]["signed_in"] is False and state["jobs"] == []
    status, body, _ = running.call("POST", "/api/search")
    assert status == 409 and "not signed in" in body["message"]


def test_a_search_can_be_stopped(running):
    running.ready_profile()
    running.web.delay = 0.3
    status, _, _ = running.call("POST", "/api/search")
    assert status == 202
    status, body, _ = running.call("POST", "/api/search")
    assert status == 409 and body["message"] == "A search is already running."
    running.call("POST", "/api/search/stop")
    state = running.wait_search()
    assert state["search"]["last"]["status"] == "stopped"
    assert state["search"]["last"]["message"].startswith("Search stopped.")


def test_a_failing_evaluator_is_reported_not_fatal(running):
    running.ready_profile()
    running.agents.override["fit_evaluator"] = {"results": "nonsense"}
    state = running.search()
    report = running.report()
    assert state["jobs"] == [] and report["status"] == "done"
    assert report["counts"]["not_judged"] >= 4


def test_the_report_and_watchlist_are_available(running):
    running.ready_profile()
    running.call("PUT", "/api/settings", {"search": {"watchlist": ["Kestrel Robotics", "Nobody Inc"]}})
    running.search()
    body = running.call("GET", "/api/search/report")[1]
    watch = {w["name"]: w for w in body["watchlist"]}
    assert watch["Kestrel Robotics"]["boards"][0]["system"] == "Greenhouse"
    assert watch["Kestrel Robotics"]["boards"][0]["last_count"] == 4
    assert watch["Nobody Inc"]["boards"] == [] and "No board found" in watch["Nobody Inc"]["note"]
    assert body["report"]["status"] == "done"
    # Watchlist companies are not suggested again by the planner.
    plan_request = json.loads([c for c in running.agents.calls if c["name"] == "search_planner"][0]["prompt"])
    assert plan_request["watchlist"] == ["Kestrel Robotics", "Nobody Inc"]


# --- add a job by link ----------------------------------------------------------------------------


def test_a_pasted_link_is_read_and_judged(running):
    running.ready_profile()
    status, body, _ = running.call("POST", "/api/jobs", {"url": "https://job-boards.greenhouse.io/kestrelrobotics/jobs/5002"},
                                   timeout=15)
    assert status == 201 and body["read"] is True and body["judging"] is True
    job = body["job"]
    assert (job["company"], job["title"], job["location"]) == ("Kestrel Robotics", "Perception Intern (Summer 2027)", "Boston, MA")
    assert job["source"]["kind"] == "manual"
    state = running.wait_search()
    job = state["jobs"][0]
    assert [h["result"] for h in job["match"]["hard"]] == ["pass", "pass"] and job["judging"] is False
    assert job["description_summary"].startswith("Sample summary:")
    assert (running.paths.data / "postings" / f"{job['id']}.md").exists()


def test_a_pasted_link_that_fails_a_requirement_is_still_added_with_the_verdict(running):
    running.ready_profile()
    running.call("POST", "/api/jobs", {"url": "https://jobs.ashbyhq.com/tidewater/cccccccc-3333-4333-8333-cccccccccccc"})
    job = running.wait_search()["jobs"][0]
    assert job["match"]["failed"] == ["Must be an internship or entry level"]


def test_your_own_details_win_over_the_page(running):
    running.ready_profile()
    status, body, _ = running.call("POST", "/api/jobs", {
        "url": "https://job-boards.greenhouse.io/kestrelrobotics/jobs/5002", "title": "Perception Intern",
        "description": "Ask Dana about this one."})
    assert status == 201 and body["job"]["title"] == "Perception Intern"
    job = running.wait_search()["jobs"][0]
    assert job["description_summary"] == "Ask Dana about this one."


def test_links_to_restricted_boards_are_not_read(running):
    running.ready_profile()
    status, body, _ = running.call("POST", "/api/jobs", {"url": "https://www.linkedin.com/jobs/view/4012345"})
    assert status == 422 and "does not allow automated reading" in body["message"]
    status, body, _ = running.call("POST", "/api/jobs", {"url": "https://www.linkedin.com/jobs/view/4012345",
                                                         "company": "Acme", "title": "Intern"})
    assert status == 201 and body["read"] is False and body["judging"] is False
    assert not any("linkedin" in r for r in running.web.requests)


def test_a_pasted_link_is_not_judged_before_the_profile_is_ready(running):
    status, body, _ = running.call("POST", "/api/jobs", {"url": "https://job-boards.greenhouse.io/kestrelrobotics/jobs/5002"})
    assert status == 201 and body["read"] is True and body["judging"] is False
    assert running.state()["jobs"][0]["match"]["hard"] == []


def test_board_names_become_readable_company_names(running):
    running.ready_profile()
    status, body, _ = running.call("POST", "/api/jobs", {"url": "https://jobs.lever.co/northwind-ai/bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"})
    assert status == 201 and body["job"]["company"] == "Northwind AI"
    assert body["job"]["details"]["team"] == "Engineering / Platform"
    job = running.wait_search()["jobs"][0]
    assert not any(c.startswith("Team:") for c in job["match"]["concerns"])
