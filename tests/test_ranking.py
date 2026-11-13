"""0.8.0: ranking by hard requirements > soft preferences > resume match, and spreading companies out."""

from __future__ import annotations

from app.core.models import HardResult, Job, Match
from app.core.ranking import order_new, rank_key, spread, spread_queue
from app.profile.models import HardItem, SearchReading, SoftItem
from app.search.agents import clean_verdict, verified_skills

READING = SearchReading(hard=[HardItem(text="internship", test="...")],
                        soft=[SoftItem(text="robotics", weight=3), SoftItem(text="large company", weight=2),
                              SoftItem(text="pay above $40/hour", weight=1)])


def job(id, company, *, soft=0, resume=None, unverified=0, found="2026-11-12T10:00:00-05:00"):
    hard = [HardResult(requirement="internship", result="unknown" if unverified else "pass")]
    return Job(id=id, company=company, title="Intern", url=f"https://x.example/{id}", found_at=found,
               match=Match(hard=hard, soft_points=soft, resume_score=resume,
                           unverified=["internship"] * unverified))


def ids(jobs):
    return [j.id for j in jobs]


def test_hard_beats_soft_beats_resume():
    jobs = [
        job("resume_only", "A", soft=0, resume=100),
        job("one_more_soft", "B", soft=1, resume=0),
        job("unverified_but_great", "C", soft=6, resume=100, unverified=1),
        job("tie_better_resume", "D", soft=0, resume=60),
        job("tie_worse_resume", "E", soft=0, resume=10),
    ]
    assert ids(order_new(jobs)) == ["one_more_soft", "resume_only", "tie_better_resume", "tie_worse_resume",
                                    "unverified_but_great"]
    # With resume match switched off, the ties fall back to newest first.
    jobs[3].found_at = "2026-11-12T09:00:00-05:00"
    assert ids(order_new(jobs, resume_match=False))[1:4] == ["resume_only", "tie_worse_resume", "tie_better_resume"]


def test_jobs_listed_before_this_version_rank_by_banded_score():
    old = Job(id="old", company="A", title="T", url="https://x.example/old", match=Match(soft_score=78))
    assert rank_key(old)[1] == -3.0
    new_ = job("new", "B", soft=2, resume=90)
    assert ids(order_new([new_, old])) == ["old", "new"]


def test_a_company_comes_again_only_after_four_others():
    jobs = [job(f"a{i}", "Acme Robotics, Inc.", soft=5) for i in range(3)] + [
        job("b", "Beta", soft=4), job("c", "Gamma", soft=3), job("d", "Delta", soft=2), job("e", "Epsilon", soft=1),
        job("f", "Zeta", soft=0)]
    assert ids(order_new(jobs)) == ["a0", "b", "c", "d", "e", "a1", "f", "a2"]
    # Spreading never moves a job across hard-requirement tiers.
    jobs.append(job("unverified", "Omega", soft=9, unverified=1))
    assert ids(order_new(jobs))[-1] == "unverified"
    # When only one company is left, it simply follows itself.
    assert ids(spread([job("x1", "X"), job("x2", "X")], company=lambda j: j.company)) == ["x1", "x2"]
    assert ids(spread(jobs, company=lambda j: j.company, spacing=0)) == ids(jobs)


def test_a_search_judges_new_companies_before_ones_already_listed():
    items = [(type("P", (), {"company": name})(), [name]) for name in ("Acme", "Acme", "Beta", "Watched Co", "Gamma")]
    order = spread_queue(items, listed={"acme", "watched co"}, watched=["Watched Co"])
    assert [i[1][0] for i in order] == ["Beta", "Watched Co", "Gamma", "Acme", "Acme"]


def test_soft_preferences_are_scored_from_each_line_with_your_weights():
    verdict = clean_verdict({
        "hard": [{"requirement": "internship", "result": "pass", "evidence": "Summer intern"}],
        "soft": [
            {"preference": "Robotics", "result": "met", "evidence": "robot arms"},
            {"preference": "large company", "result": "not_met", "evidence": "a 12-person startup"},
            {"preference": "pay above $40/hour", "result": "met", "evidence": ""},  # no evidence: unknown
            {"preference": "free lunch", "result": "met", "evidence": "lunch"},     # not one of yours
        ],
        "resume": {"level": "strong", "skills": ["python", "ROS 2", "Kubernetes", "C"],
                   "similar_experience": "Like your calibration work at Halden Robotics."},
    }, READING, posting_text="Robotics intern. Python and ROS 2 on robot arms, some CAD.",
        profile_skills=["Python", "C++", "ROS 2", "C", "PyTorch"])
    assert [s["result"] for s in verdict.soft] == ["met", "not_met", "unknown"]
    assert verdict.soft_points == 3 - 2 and verdict.soft_score == 50 + round(50 * 1 / 6)
    assert verdict.soft_hits == ["robotics"] and verdict.soft_misses == ["large company"]
    # Kubernetes is not yours, and "C" is not named in the posting ("CAD" does not count).
    assert verdict.resume_skills == ["Python", "ROS 2"]
    assert verdict.resume_level == "strong" and verdict.resume_score == 70 + 12
    assert verdict.resume_note == "Like your calibration work at Halden Robotics."
    # Resume match switched off: nothing is kept.
    off = clean_verdict({"resume": {"level": "strong", "skills": ["Python"]}}, READING, posting_text="Python")
    assert off.resume_level == "" and off.resume_score is None and off.resume_skills == []


def test_skills_are_matched_as_words():
    assert verified_skills(["c++", "Go"], ["C++", "Go"], "We use C++ and Google Cloud.") == ["C++"]


def test_the_new_tab_follows_the_ranking_and_shows_the_resume_match(open_dashboard, running):
    from app.core.store import TrackerStore

    store = TrackerStore(running.paths.tracker)
    for item in (job("a0", "Acme", soft=3, resume=20), job("a1", "Acme", soft=3, resume=10),
                 job("b", "Beta", soft=1, resume=90), job("c", "Gamma", soft=3, resume=80)):
        item.title = f"Intern {item.id}"
        item.match.resume_level = "strong" if item.id == "c" else "little"
        item.match.resume_skills = ["Python", "ROS 2"] if item.id == "c" else []
        item.match.resume_note = "Like your calibration work at Halden Robotics." if item.id == "c" else ""
        store.add(item)
    assert [j["id"] for j in sorted(running.state()["jobs"], key=lambda j: j["rank"])] == ["c", "a0", "b", "a1"]
    page = open_dashboard()
    page.click(".view-link[data-view=jobs]")
    page.wait_for_selector(".job[data-id=c]")
    assert [row.get_attribute("data-id") for row in page.locator(".job").all()] == ["c", "a0", "b", "a1"]
    line = page.locator(".job[data-id=c] .job-resume").inner_text()
    assert "Strong resume match" in line and "Python, ROS 2" in line and "Halden Robotics" in line
    # Switched off in the Search screen: the resume no longer ranks or shows.
    running.call("PUT", "/api/settings", {"search": {"resume_match": False}})
    page.wait_for_function("!document.querySelector('.job[data-id=c] .job-resume')")
    assert [j["id"] for j in sorted(running.state()["jobs"], key=lambda j: j["rank"])][:2] == ["a0", "c"]
