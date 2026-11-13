"""Ashby: public job boards at jobs.ashbyhq.com.

Feed: GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
There is no single-job address; one job is found in its board's feed.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from ..http import FetchError, Web
from ..models import Board, Posting, iso_day, pretty_name
from ..text import html_to_text

API = "https://api.ashbyhq.com/posting-api/job-board"
_BOARD = re.compile(r"^[A-Za-z0-9_.%-]{1,80}$")
_ID = re.compile(r"^[0-9a-fA-F-]{30,40}$")


def identify(url: str) -> Optional[dict]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != "jobs.ashbyhq.com":
        return None
    segments = [s for s in parts.path.split("/") if s]
    if not segments or not _BOARD.match(segments[0]):
        return None
    job_id = segments[1] if len(segments) >= 2 and _ID.match(segments[1]) else ""
    if not job_id:
        jid = (parse_qs(parts.query).get("ashby_jid") or [""])[0]
        job_id = jid if _ID.match(jid) else ""
    return {"token": segments[0], "job_id": job_id}


def _place(job: dict) -> str:
    places = [job.get("location") or ""]
    for extra in job.get("secondaryLocations") or []:
        if isinstance(extra, dict) and extra.get("location"):
            places.append(extra["location"])
    return "; ".join(dict.fromkeys(p for p in places if p))


def _pay(job: dict) -> str:
    comp = job.get("compensation") or {}
    return (comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
            or job.get("compensationSummary") or "")


def _posting(job: dict, token: str, company: str) -> Posting:
    description = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml") or "")
    workplace = str(job.get("workplaceType") or ("Remote" if job.get("isRemote") else ""))
    team = " / ".join(x for x in (job.get("department"), job.get("team")) if x)
    return Posting(
        company=company or pretty_name(token),
        title=(job.get("title") or "").strip(),
        location=_place(job),
        url=job.get("jobUrl") or f"https://jobs.ashbyhq.com/{token}/{job.get('id')}",
        apply_url=job.get("applyUrl") or "",
        source="ashby",
        found_via=f"{company or pretty_name(token)} careers (Ashby)",
        board=f"ashby:{token.lower()}",
        external_id=str(job.get("id") or ""),
        posted_at=iso_day(job.get("publishedAt")),
        pay=_pay(job),
        type=str(job.get("employmentType") or ""),
        remote=workplace,
        description=description.strip(),
        full=bool(description.strip()),
        open=True,
        notes=[f"Team: {team}"] if team else [],
    )


async def read_board(web: Web, board: Board) -> list[Posting]:
    data = (await web.get(f"{API}/{board.token}?includeCompensation=true", max_bytes=25 * 1024 * 1024,
                          timeout=40)).json()
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise FetchError("http", board.token, "Ashby sent something other than a job list.")
    return [_posting(job, board.token, board.company) for job in data["jobs"]
            if isinstance(job, dict) and job.get("isListed", True) is not False]


async def read_job(web: Web, token: str, job_id: str, company: str = "") -> Posting:
    for posting in await read_board(web, Board(ats="ashby", token=token, company=company)):
        if posting.external_id == job_id:
            return posting
    raise FetchError("not_found", job_id, "That job is no longer on the company's Ashby board.")
