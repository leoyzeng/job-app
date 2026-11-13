"""Sample-data mode for the search, and what the tests use: a made-up web and stand-in agents.

Nothing here goes on the internet. ``FakeWeb`` answers from a table of
addresses; the companies, boards and postings are invented, on example
domains. The stand-in agents follow simple rules instead of calling Claude.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable, Optional

from ..agents.runner import AgentReply
from .http import FetchError, Response, check_public
from .text import in_us


def _public(host, port, type=0):  # noqa: A002 - mirrors socket.getaddrinfo
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class FakeWeb:
    """Answers GET and POST requests from a table. Anything not in the table is "not found"."""

    def __init__(self, routes: Optional[dict[str, Any]] = None, posts: Optional[dict[str, Any]] = None,
                 delay: float = 0.0):
        self.routes: dict[str, Any] = dict(routes or {})
        self.posts: dict[str, Any] = dict(posts or {})
        self.requests: list[str] = []
        self.delay = delay
        self.counts: dict[str, int] = {}
        self.bytes = 0

    def _answer(self, url: str, table: dict[str, Any], body: Optional[dict] = None) -> Response:
        check_public(url, _public)
        value = table.get(url)
        if callable(value):
            value = value(body) if body is not None else value()
        if value is None:
            raise FetchError("not_found", url, "The site answered 404.", 404)
        status, content, headers = 200, value, {}
        if isinstance(value, tuple):
            status, content = value[0], value[1]
            headers = value[2] if len(value) > 2 else {}
        if status in (404, 410):
            raise FetchError("not_found", url, f"The site answered {status}.", status)
        if status >= 400:
            raise FetchError("http", url, f"The site answered {status}.", status)
        if isinstance(content, (dict, list)):
            data = json.dumps(content).encode("utf-8")
            headers = {"content-type": "application/json", **headers}
        elif isinstance(content, bytes):
            data = content
        else:
            data = str(content).encode("utf-8")
            headers = {"content-type": "text/html; charset=utf-8", **headers}
        return Response(url=url, status=status, headers=headers, body=data)

    async def get(self, url: str, *, max_bytes: int = 0, timeout: float = 0) -> Response:
        self.requests.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._answer(url, self.routes)

    async def post_json(self, url: str, body: dict, *, max_bytes: int = 0, timeout: float = 0) -> Response:
        self.requests.append(f"POST {url} {json.dumps(body, sort_keys=True)}")
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._answer(url, self.posts, body)


# --- the made-up job market ----------------------------------------------------------------------

GH = "https://boards-api.greenhouse.io/v1/boards"
LEVER = "https://api.lever.co/v0/postings"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board"
LIST = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"

ROBOTICS_TEXT = ("<p>Kestrel Robotics builds inspection drones for bridges and power lines.</p>"
                 "<h3>What you will do</h3><ul><li>Write ROS 2 nodes for flight control and perception.</li>"
                 "<li>Test on real hardware in our Pittsburgh lab.</li></ul>"
                 "<h3>About the internship</h3><p>This is a 12-week Summer 2027 internship, May to August 2027, "
                 "for students pursuing a degree in computer science, electrical engineering or robotics. "
                 "Pay is $42 an hour.</p>")


def _gh_job(job_id: int, title: str, location: str, html: str, published: str = "2026-11-02T10:00:00-05:00") -> dict:
    return {
        "id": job_id, "internal_job_id": job_id + 1000, "title": title, "updated_at": published,
        "first_published": published, "location": {"name": location},
        "absolute_url": f"https://job-boards.greenhouse.io/kestrelrobotics/jobs/{job_id}",
        "content": html.replace("<", "&lt;").replace(">", "&gt;"), "company_name": "Kestrel Robotics",
        "departments": [{"id": 1, "name": "Engineering"}], "offices": [], "metadata": None,
    }


def sample_routes() -> dict[str, Any]:
    kestrel_jobs = [
        _gh_job(5001, "Robotics Software Intern, Summer 2027", "Pittsburgh, PA", ROBOTICS_TEXT),
        _gh_job(5002, "Perception Intern (Summer 2027)", "Boston, MA",
                "<p>Train perception models for drone inspection.</p><p>Summer 2027 internship, 12 weeks, "
                "starting in May 2027. Hybrid in Boston.</p>"),
        _gh_job(5003, "Senior Robotics Engineer", "Pittsburgh, PA", "<p>8+ years of experience.</p>"),
        _gh_job(5004, "Embedded Firmware Intern - Summer 2026", "Pittsburgh, PA", "<p>Last year's role.</p>"),
    ]
    northwind = [
        {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "text": "Machine Learning Intern",
         "hostedUrl": "https://jobs.lever.co/northwind-ai/aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
         "applyUrl": "https://jobs.lever.co/northwind-ai/aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa/apply",
         "categories": {"commitment": "Intern", "department": "Research", "location": "Toronto, ON",
                        "team": "ML", "allLocations": ["Toronto, ON"]},
         "createdAt": 1793804400000, "descriptionPlain": "Work on language models in Toronto.", "lists": [],
         "additionalPlain": "", "workplaceType": "onsite"},
        {"id": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb", "text": "Software Engineering Intern",
         "hostedUrl": "https://jobs.lever.co/northwind-ai/bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
         "applyUrl": "https://jobs.lever.co/northwind-ai/bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb/apply",
         "categories": {"commitment": "Intern", "department": "Engineering", "location": "Remote - US",
                        "team": "Platform", "allLocations": ["Remote - US"]},
         "createdAt": 1794063600000,
         "descriptionPlain": "Build the platform that serves our models. Summer 2027 internship, remote within "
                             "the United States, May 17 to August 13, 2027.",
         "lists": [{"text": "What we look for", "content": "<li>Python or Go</li><li>Curiosity</li>"}],
         "additionalPlain": "", "workplaceType": "remote",
         "salaryRange": {"min": 38, "max": 45, "currency": "USD", "interval": "per-hour-wage"}},
    ]
    tidewater = {"apiVersion": "1", "jobs": [
        {"id": "cccccccc-3333-4333-8333-cccccccccccc", "title": "Autonomy Engineering Intern",
         "department": "Autonomy", "team": "Planning", "employmentType": "Intern", "location": "Austin, TX",
         "secondaryLocations": [], "publishedAt": "2026-10-20T15:00:00.000Z", "isListed": True, "isRemote": False,
         "workplaceType": "OnSite", "jobUrl": "https://jobs.ashbyhq.com/tidewater/cccccccc-3333-4333-8333-cccccccccccc",
         "applyUrl": "https://jobs.ashbyhq.com/tidewater/cccccccc-3333-4333-8333-cccccccccccc/application",
         "descriptionPlain": "Motion planning for autonomous boats. Requires 5+ years of professional "
                             "experience in robotics.", "descriptionHtml": ""},
    ]}
    harborlight_page = (
        "<html><head><title>Controls Intern | Harborlight Systems</title>"
        '<script type="application/ld+json">' + json.dumps({
            "@context": "https://schema.org", "@type": "JobPosting", "title": "Controls Software Intern",
            "hiringOrganization": {"@type": "Organization", "name": "Harborlight Systems"},
            "jobLocation": {"@type": "Place", "address": {"addressLocality": "Seattle", "addressRegion": "WA",
                                                          "addressCountry": "US"}},
            "datePosted": "2026-11-05", "employmentType": "INTERN",
            "description": "<p>Write control software for marine robots. Summer 2027 internship in Seattle, "
                           "about twelve weeks starting in May 2027. We look for students who enjoy "
                           "robotics, C++ and testing on real hardware. You will join a small team, pair with "
                           "an engineer every day, and ship code that runs on boats in Puget Sound by the end "
                           "of the summer. Interns present their work to the whole company in August.</p>",
        }) + "</script></head><body><nav>Menu</nav><h1>Controls Software Intern</h1>"
        "<p>Write control software for marine robots.</p></body></html>")
    listings = [
        {"company_name": "Kestrel Robotics", "title": "Robotics Software Intern, Summer 2027",
         "locations": ["Pittsburgh, PA"], "url": "https://job-boards.greenhouse.io/kestrelrobotics/jobs/5001",
         "date_posted": 1793631600, "terms": ["Summer 2027"], "active": True, "is_visible": True,
         "sponsorship": "Other", "id": "1"},
        {"company_name": "Harborlight Systems", "title": "Controls Software Intern",
         "locations": ["Seattle, WA"], "url": "https://careers.harborlight.example/jobs/controls-intern?utm_source=Simplify",
         "date_posted": 1793890800, "terms": ["Summer 2027"], "active": True, "is_visible": True,
         "sponsorship": "Does Not Offer Sponsorship", "id": "2"},
        {"company_name": "Old Mill Labs", "title": "Software Intern", "locations": ["Denver, CO"],
         "url": "https://careers.oldmill.example/jobs/9", "date_posted": 1793545200, "terms": ["Summer 2027"],
         "active": False, "is_visible": True, "id": "3"},
        {"company_name": "Quayside Robotics", "title": "Robotics Intern", "locations": ["Portland, OR"],
         "url": "https://careers.quayside.example/jobs/robotics-intern", "date_posted": 1793977200,
         "terms": ["Summer 2027"], "active": True, "is_visible": True, "id": "4"},
    ]
    return {
        f"{GH}/kestrelrobotics": {"name": "Kestrel Robotics", "content": ""},
        f"{GH}/kestrelrobotics/jobs?content=true": {"jobs": kestrel_jobs, "meta": {"total": len(kestrel_jobs)}},
        **{f"{GH}/kestrelrobotics/jobs/{j['id']}?pay_transparency=true": j for j in kestrel_jobs},
        f"{LEVER}/northwind-ai?mode=json&limit=1": northwind[:1],
        f"{LEVER}/northwind-ai?mode=json": northwind,
        **{f"{LEVER}/northwind-ai/{j['id']}?mode=json": j for j in northwind},
        f"{ASHBY}/tidewater?includeCompensation=true": tidewater,
        LIST: listings,
        "https://careers.harborlight.example/jobs/controls-intern?utm_source=Simplify": harborlight_page,
        "https://careers.quayside.example/jobs/robotics-intern": (
            "<html><body><h1>Robotics Intern</h1><p>We are no longer accepting applications for this role.</p>"
            "</body></html>"),
    }


def sample_web() -> FakeWeb:
    return FakeWeb(sample_routes())


# --- stand-in agents ------------------------------------------------------------------------------

DEMO_COMPANIES = ["Kestrel Robotics", "Northwind AI", "Tidewater"]


def fake_plan(request: dict) -> dict:
    text = " ".join([h["text"] for h in request.get("hard", [])] + request.get("types", [])).lower()
    intern = "intern" in text
    years = sorted(set(re.findall(r"\b(20\d\d)\b", text))) or (["2027"] if intern else [])
    watch = {w.lower() for w in request.get("watchlist", [])}
    return {
        "queries": [f"{t} {years[0] if years else ''}".strip() for t in (request.get("titles") or ["engineer"])[:4]],
        "kind_words": ["intern", "internship", "co-op"] if intern else [],
        "role_words": [],
        "exclude_words": ["senior", "staff", "principal", "manager"] if intern else [],
        "countries": ["US"] if "united states" in text or " us" in f" {text}" else [],
        "years": years if intern else [],
        "terms": [f"Summer {y}" for y in years] if intern else [],
        "lists": ["internships"] if intern else [],
        "companies": [{"name": n, "why": "Sample company."} for n in DEMO_COMPANIES if n.lower() not in watch],
        "notes": "",
    }


def _years_required(text: str) -> Optional[int]:
    match = re.search(r"(\d+)\+?\s*years", text.lower())
    return int(match.group(1)) if match else None


def fake_verdicts(request: dict) -> dict:
    results = []
    for posting in request.get("postings", []):
        body = f"{posting.get('title', '')} {posting.get('type', '')} {posting.get('description', '')}".lower()
        hard = []
        for requirement in request.get("hard", []):
            line = requirement["text"].lower()
            result, evidence = "unknown", ""
            if "intern" in line or "entry level" in line:
                years = _years_required(body)
                if years is not None and years >= 3:
                    result, evidence = "fail", f"Asks for {years}+ years of experience."
                elif "intern" in body or "entry level" in body:
                    result, evidence = "pass", "The posting is for an internship."
            elif "united states" in line or re.search(r"\bus\b", line):
                where = in_us(posting.get("location", ""))
                if where is True:
                    result, evidence = "pass", posting.get("location", "")
                elif where is False:
                    result, evidence = "fail", posting.get("location", "")
            elif re.search(r"20\d\d", line):
                if "2027" in body:
                    result, evidence = "pass", "The posting names 2027."
            hard.append({"requirement": requirement["text"], "result": result, "evidence": evidence})
        soft = []
        for preference in request.get("soft", []):
            words = [w for w in re.findall(r"[a-z+#]{3,}", preference["text"].lower())
                     if w not in ("prefer", "preferably", "with", "and", "the", "work", "working", "roles", "role")]
            hit = next((w for w in words if w.rstrip("s") in body), None)
            soft.append({"preference": preference["text"], "result": "met" if hit else "unknown",
                         "evidence": f"The posting mentions {hit}." if hit else ""})
        resume = request.get("resume") or {}
        skills = [s for s in resume.get("skills", []) if re.search(r"(?<![\w+#])" + re.escape(s.lower()) + r"(?![\w+#])", body)]
        level = "strong" if len(skills) >= 3 else "some" if skills else "none"
        first_job = (resume.get("experience") or [{}])[0]
        description = posting.get("description", "")
        first = re.split(r"(?<=[.!?])\s", description.strip(), maxsplit=1)[0] if description else ""
        results.append({
            "key": posting["key"], "hard": hard, "soft": soft,
            "resume": {"level": level, "skills": skills,
                       "similar_experience": (f"Like your {first_job.get('title', '')} role at {first_job.get('employer', '')}."
                                              if level != "none" and first_job else "")},
            "concerns": [], "summary": f"Sample summary: {first[:200]}", "closed": False, "scam_signs": [],
        })
    return {"results": results}


async def demo_web_agent(*, prompt: str, instructions: str, schema: dict, read_page: Callable) -> AgentReply:
    request = json.loads(prompt)
    await asyncio.sleep(0.3)
    url = "https://jobs.lever.co/northwind-ai/bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
    page = await read_page(url)
    postings = []
    if "Software Engineering Intern" in page:
        postings.append({
            "company": "Northwind AI", "title": "Software Engineering Intern", "location": "Remote - US", "url": url,
            "found_on": "web search", "is_employer_page": True, "posted_at": "", "pay": "", "type": "Intern",
            "summary": "Platform internship.", "why_relevant": "Software internship in the US.", "doubts": "",
        })
    return AgentReply(data={"postings": postings[: request.get("max_results", 10)],
                            "tried": f"Ran {len(request.get('queries', []))} sample web searches."},
                      path="demo", model="stand-in", turns=2, cost_usd=0.0, duration_ms=300)
