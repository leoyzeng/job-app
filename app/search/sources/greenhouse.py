"""Greenhouse: public job boards at boards.greenhouse.io and job-boards.greenhouse.io.

Feed: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
One job: GET .../v1/boards/{token}/jobs/{id}?pay_transparency=true
Board name: GET .../v1/boards/{token}
The description (``content``) arrives with its HTML tags escaped.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from ..http import FetchError, Web
from ..models import Board, Posting, iso_day, pretty_name
from ..text import html_to_text, unescape_html

API = "https://boards-api.greenhouse.io/v1/boards"
HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io")
_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def identify(url: str) -> Optional[dict]:
    """{"token", "job_id"} for a Greenhouse link; job_id is "" for a board link."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    query = parse_qs(parts.query)
    if host not in HOSTS:
        # Embedded boards on company sites keep Greenhouse's own parameters.
        if host == "boards-api.greenhouse.io":
            match = re.match(r"^/v1/boards/([^/]+)(?:/jobs/(\d+))?", parts.path)
            if match and _TOKEN.match(match.group(1)):
                return {"token": match.group(1), "job_id": match.group(2) or ""}
        return None
    segments = [s for s in parts.path.split("/") if s]
    if segments[:1] == ["embed"]:
        token = (query.get("for") or [""])[0]
        job_id = (query.get("token") or [""])[0]
        if token and _TOKEN.match(token):
            return {"token": token, "job_id": job_id if job_id.isdigit() else ""}
        return None
    if not segments or not _TOKEN.match(segments[0]):
        return None
    token = segments[0]
    job_id = ""
    if len(segments) >= 3 and segments[1] == "jobs" and segments[2].isdigit():
        job_id = segments[2]
    elif (query.get("gh_jid") or [""])[0].isdigit():
        job_id = query["gh_jid"][0]
    return {"token": token, "job_id": job_id}


def _posting(job: dict, token: str, company: str) -> Posting:
    content = html_to_text(unescape_html(job.get("content") or ""))
    pay = ""
    for band in job.get("pay_input_ranges") or []:
        low, high = band.get("min_cents"), band.get("max_cents")
        if low and high:
            pay = f"{band.get('currency_type') or ''} {low / 100:,.0f}-{high / 100:,.0f}".strip()
            if band.get("title"):
                pay += f" ({band['title']})"
            break
    departments = ", ".join(d.get("name", "") for d in job.get("departments") or [] if d.get("name"))
    return Posting(
        company=job.get("company_name") or company or pretty_name(token),
        title=(job.get("title") or "").strip(),
        location=((job.get("location") or {}).get("name") or "").strip(),
        url=job.get("absolute_url") or f"https://job-boards.greenhouse.io/{token}/jobs/{job.get('id')}",
        apply_url=job.get("absolute_url") or "",
        source="greenhouse",
        found_via=f"{company or pretty_name(token)} careers (Greenhouse)",
        board=f"greenhouse:{token.lower()}",
        external_id=str(job.get("id") or ""),
        posted_at=iso_day(job.get("first_published") or job.get("updated_at")),
        pay=pay,
        description=content,
        full=bool(content),
        open=True,
        notes=[f"Team: {departments}"] if departments else [],
    )


async def board_name(web: Web, token: str) -> str:
    data = (await web.get(f"{API}/{token}", timeout=15)).json()
    return (data.get("name") or "").strip() if isinstance(data, dict) else ""


async def read_board(web: Web, board: Board) -> list[Posting]:
    data = (await web.get(f"{API}/{board.token}/jobs?content=true", max_bytes=25 * 1024 * 1024, timeout=40)).json()
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise FetchError("http", board.token, "Greenhouse sent something other than a job list.")
    return [_posting(job, board.token, board.company) for job in data["jobs"] if isinstance(job, dict)]


async def read_job(web: Web, token: str, job_id: str, company: str = "") -> Posting:
    data = (await web.get(f"{API}/{token}/jobs/{job_id}?pay_transparency=true", timeout=20)).json()
    if not isinstance(data, dict) or not data.get("title"):
        raise FetchError("http", job_id, "Greenhouse sent something other than a job.")
    return _posting(data, token, company)
