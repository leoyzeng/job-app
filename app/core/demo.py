"""Sample jobs for sample-data mode (``jobapp demo``).

Every company here is made up. The set covers every status so the dashboard
can be seen in all its states before search and autofill exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .dedupe import job_id_for, make_dedupe_keys
from .models import (
    ApplicationRef, Details, HardResult, HistoryEntry, Job, Match, Need, Source, Status, StatusDetail,
)


def _ago(**delta) -> str:
    return (datetime.now().astimezone() - timedelta(**delta)).isoformat(timespec="seconds")


def _day(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _job(slug, company, title, location, *, status=Status.NEW, path=(), **kw) -> Job:
    url = f"https://careers.example.com/{slug}"
    history, previous = [], "new"
    for index, (to, by, hours) in enumerate(path):
        history.append(HistoryEntry(at=_ago(hours=hours), from_status=previous, to=to, by=by))
        previous = to
    return Job(
        id=job_id_for(url),
        company=company,
        title=title,
        location=location,
        url=url,
        apply_url=url + "/apply",
        dedupe_keys=make_dedupe_keys(url, company, title, location),
        status=status,
        history=history,
        **kw,
    )


PRACTICE_STYLES = ("greenhouse", "lever", "ashby", "workday", "embedded")


def sample_jobs(practice_base: str = "") -> list[Job]:
    """The sample listings. With ``practice_base`` their application links point at the practice forms,
    so Prepare can be tried in sample-data mode without touching a real site."""
    jobs = _sample_jobs()
    if practice_base:
        for index, job in enumerate(jobs):
            job.apply_url = f"{practice_base.rstrip('/')}/{PRACTICE_STYLES[index % len(PRACTICE_STYLES)]}.html"
    return jobs


def _sample_jobs() -> list[Job]:
    entry = HardResult(requirement="Must be an internship or entry level", result="pass", evidence="Title says Intern")
    return [
        _job(
            "halcyon/robotics-software-intern", "Halcyon Robotics", "Robotics Software Intern, Summer 2027",
            "Pittsburgh, PA (on site)",
            found_at=_ago(hours=2), posted_at=_day(2),
            source=Source(kind="greenhouse", found_via="company career page"),
            details=Details(pay="$38 to $45 an hour", type="Internship", remote="on site"),
            description_summary=(
                "Work on motion planning and controls for a mobile manipulator used in warehouse picking. "
                "The team writes C++ and Python on ROS 2 and tests on real hardware every week. "
                "Interns own one feature from design review to a demo on the robot."
            ),
            match=Match(
                hard=[entry], soft_score=91,
                soft_hits=["robotics", "ROS", "pay above your preference"], concerns=[],
            ),
        ),
        _job(
            "tessellate/embedded-engineer-1", "Tessellate Labs", "Embedded Systems Engineer I", "Remote (US)",
            found_at=_ago(hours=2), posted_at=_day(6),
            source=Source(kind="lever", found_via="web search"),
            details=Details(pay="", type="Entry level", remote="remote"),
            description_summary=(
                "Firmware for battery-powered sensor nodes: bring-up on STM32, drivers, and a small RTOS. "
                "Looking for new graduates comfortable with C and an oscilloscope."
            ),
            match=Match(
                hard=[HardResult(requirement="Must be an internship or entry level", result="pass",
                                 evidence="\"0 to 2 years of experience\"")],
                soft_score=74, soft_hits=["embedded"], concerns=["Pay is not listed", "Sponsorship is not mentioned"],
            ),
        ),
        _job(
            "northfield/controls-new-grad", "Northfield Automation", "Controls Engineer, New Grad",
            "Columbus, OH (hybrid)",
            found_at=_ago(days=1), posted_at=_day(9),
            source=Source(kind="ashby", found_via="your watchlist"),
            details=Details(pay="$82,000 to $95,000 a year", type="Entry level", remote="hybrid",
                            deadline=_day(-12)),
            description_summary=(
                "Design and tune controllers for conveyor and sortation systems, and support commissioning at "
                "customer sites. Some travel."
            ),
            match=Match(hard=[HardResult(requirement="Must be an internship or entry level", result="pass",
                                         evidence="Title says New Grad")],
                        soft_score=68, soft_hits=["controls", "automation"],
                        concerns=["Up to 25% travel"]),
        ),
        _job(
            "copperline/perception-intern", "Copperline Motion", "Perception Engineering Intern", "Boston, MA (hybrid)",
            status=Status.QUEUED, path=[("queued", "user", 0.02)],
            found_at=_ago(days=1), posted_at=_day(4),
            source=Source(kind="greenhouse", found_via="company career page"),
            details=Details(pay="$40 an hour", type="Internship", remote="hybrid"),
            description_summary="Camera and lidar perception for sidewalk delivery robots.",
            match=Match(hard=[entry], soft_score=88, soft_hits=["robotics", "PyTorch"]),
        ),
        _job(
            "orbitwell/firmware-intern", "Orbitwell", "Firmware Engineer Intern", "Seattle, WA (on site)",
            status=Status.FILLING, path=[("queued", "user", 0.2), ("filling", "app", 0.18)],
            status_detail=StatusDetail(message="Page 2 of 4: education and work history"),
            application=ApplicationRef(page=2, pages_total=4),
            found_at=_ago(days=2), posted_at=_day(7),
            source=Source(kind="workday", found_via="web search"),
            details=Details(pay="$36 to $42 an hour", type="Internship", remote="on site"),
            description_summary="Flight computer firmware in C and Rust for small satellites.",
            match=Match(hard=[entry], soft_score=79, soft_hits=["embedded", "FPGA"]),
        ),
        _job(
            "kestrel/autonomy-intern", "Kestrel Dynamics", "Autonomy Software Intern", "Austin, TX (on site)",
            status=Status.WAITING_FOR_INPUT,
            path=[("queued", "user", 3), ("filling", "app", 3), ("waiting_for_input", "agent", 2.9)],
            status_detail=StatusDetail(
                message="2 fields need you",
                needs=[
                    Need(label="Salary expectation", page=1, reason="You have not answered this before."),
                    Need(label="Will you now or in the future require sponsorship?", page=1,
                         reason="This is never guessed."),
                ],
            ),
            application=ApplicationRef(page=1, pages_total=1),
            found_at=_ago(days=2), posted_at=_day(3),
            source=Source(kind="lever", found_via="company career page"),
            details=Details(pay="$45 an hour", type="Internship", remote="on site"),
            description_summary="Planning and state estimation for an autonomous inspection drone.",
            match=Match(hard=[entry], soft_score=93, soft_hits=["robotics", "ROS", "controls"]),
        ),
        _job(
            "lumen/ml-infra-intern", "Lumen Foundry", "ML Infrastructure Intern", "San Jose, CA (hybrid)",
            status=Status.WAITING_FOR_PAGE_REVIEW,
            path=[("queued", "user", 5), ("filling", "app", 5), ("waiting_for_page_review", "agent", 4.9)],
            status_detail=StatusDetail(message="Page 1 of 3 is filled. Look it over, then click Continue."),
            application=ApplicationRef(page=1, pages_total=3),
            found_at=_ago(days=3), posted_at=_day(10),
            source=Source(kind="workday", found_via="web search"),
            details=Details(pay="$50 an hour", type="Internship", remote="hybrid"),
            description_summary="Test and release tooling for an inference accelerator software stack.",
            match=Match(hard=[entry], soft_score=71, soft_hits=["pay above your preference"]),
        ),
        _job(
            "brightwater/swe-early-career", "Brightwater Mobility", "Software Engineer, Early Career",
            "Ann Arbor, MI (hybrid)",
            status=Status.WAITING_FOR_REVIEW,
            path=[("queued", "user", 26), ("filling", "app", 26), ("waiting_for_review", "agent", 25.8)],
            status_detail=StatusDetail(
                message="Everything is filled. Review and submit in the browser, then mark it as applied."
            ),
            application=ApplicationRef(page=1, pages_total=1),
            found_at=_ago(days=4), posted_at=_day(12),
            source=Source(kind="ashby", found_via="your watchlist"),
            details=Details(pay="$98,000 a year", type="Entry level", remote="hybrid"),
            description_summary="Simulation and tooling for driver-assistance features.",
            match=Match(hard=[HardResult(requirement="Must be an internship or entry level", result="pass",
                                         evidence="Title says Early Career")],
                        soft_score=65, soft_hits=["simulation"]),
        ),
        _job(
            "stonebridge/test-engineer-intern", "Stonebridge Controls", "Test Engineer Intern", "Cleveland, OH (on site)",
            status=Status.ERROR,
            path=[("queued", "user", 30), ("filling", "app", 30), ("error", "app", 29.9)],
            status_detail=StatusDetail(
                message="The application page did not load. Check the link, then click Prepare to try again."
            ),
            found_at=_ago(days=4), posted_at=_day(15),
            source=Source(kind="other", found_via="web search"),
            details=Details(pay="$28 an hour", type="Internship", remote="on site"),
            description_summary="Hardware-in-the-loop test rigs for industrial drives.",
            match=Match(hard=[entry], soft_score=58, soft_hits=["controls"], concerns=["Pay is below your preference"]),
        ),
        _job(
            "quillon/flight-software-intern", "Quillon Aerospace", "Flight Software Intern", "Denver, CO (on site)",
            status=Status.APPLIED,
            path=[("queued", "user", 75), ("filling", "app", 75), ("waiting_for_review", "agent", 74.8),
                  ("applied", "user", 74)],
            found_at=_ago(days=6), posted_at=_day(14),
            source=Source(kind="greenhouse", found_via="company career page"),
            details=Details(pay="$41 an hour", type="Internship", remote="on site"),
            description_summary="Guidance and navigation software for a small launch vehicle.",
            match=Match(hard=[entry], soft_score=84, soft_hits=["controls", "C++"]),
        ),
        _job(
            "paperclip/data-analyst", "Paperclip Analytics", "Data Analyst", "Remote (US)",
            status=Status.SKIPPED, path=[("skipped", "user", 50)],
            found_at=_ago(days=5), posted_at=_day(20),
            source=Source(kind="other", found_via="web search"),
            details=Details(pay="$70,000 a year", type="Entry level", remote="remote"),
            description_summary="Dashboards and reporting for retail clients.",
            match=Match(hard=[HardResult(requirement="Must be an internship or entry level", result="pass",
                                         evidence="\"Entry level\"")], soft_score=22),
        ),
        _job(
            "vantage/robotics-intern", "Vantage Kinetics", "Robotics Intern", "Detroit, MI (on site)",
            status=Status.CLOSED, path=[("closed", "app", 20)],
            status_detail=StatusDetail(message="The employer took this posting down."),
            found_at=_ago(days=8), posted_at=_day(40),
            source=Source(kind="lever", found_via="your watchlist"),
            details=Details(pay="$35 an hour", type="Internship", remote="on site"),
            description_summary="Robot cell integration for automotive assembly.",
            match=Match(hard=[entry], soft_score=80, soft_hits=["robotics"]),
        ),
    ]
