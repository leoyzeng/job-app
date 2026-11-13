"""Lever: public job boards at jobs.lever.co (and jobs.eu.lever.co for EU-hosted companies).

Feed: GET https://api.lever.co/v0/postings/{site}?mode=json
One job: GET https://api.lever.co/v0/postings/{site}/{id}?mode=json
EU-hosted companies answer on api.eu.lever.co instead.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from ..http import FetchError, Web
from ..models import Board, Posting, iso_day, pretty_name
from ..text import html_to_text, tidy

API = {"": "https://api.lever.co/v0/postings", "eu": "https://api.eu.lever.co/v0/postings"}
_SITE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_ID = re.compile(r"^[0-9a-fA-F-]{20,40}$")


def identify(url: str) -> Optional[dict]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in ("jobs.lever.co", "jobs.eu.lever.co"):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if not segments or not _SITE.match(segments[0]):
        return None
    job_id = segments[1] if len(segments) >= 2 and _ID.match(segments[1]) else ""
    return {"token": segments[0], "job_id": job_id, "region": "eu" if ".eu." in host else ""}


def _pay(job: dict) -> str:
    band = job.get("salaryRange") or {}
    if band.get("min") and band.get("max"):
        interval = str(band.get("interval") or "").replace("-", " ")
        text = f"{band.get('currency') or ''} {band['min']:,}-{band['max']:,}".strip()
        return f"{text} {interval}".strip()
    return tidy(job.get("salaryDescriptionPlain") or "")[:200]


def _posting(job: dict, site: str, company: str, region: str) -> Posting:
    categories = job.get("categories") or {}
    parts = [job.get("descriptionPlain") or html_to_text(job.get("description") or "")]
    for block in job.get("lists") or []:
        heading = (block.get("text") or "").strip()
        body = html_to_text(block.get("content") or "")
        if heading or body:
            parts.append(f"{heading}\n{body}".strip())
    parts.append(job.get("additionalPlain") or html_to_text(job.get("additional") or ""))
    description = tidy("\n\n".join(p for p in parts if p))
    locations = categories.get("allLocations") or []
    location = categories.get("location") or ""
    if len(locations) > 1:
        location = "; ".join(locations)
    team = " / ".join(x for x in (categories.get("department"), categories.get("team")) if x)
    return Posting(
        company=company or pretty_name(site),
        title=(job.get("text") or "").strip(),
        location=location,
        url=job.get("hostedUrl") or "",
        apply_url=job.get("applyUrl") or "",
        source="lever",
        found_via=f"{company or pretty_name(site)} careers (Lever)",
        board=f"lever:{site.lower()}",
        external_id=str(job.get("id") or ""),
        posted_at=iso_day(job.get("createdAt")),
        pay=_pay(job),
        type=categories.get("commitment") or "",
        remote=str(job.get("workplaceType") or "").replace("unspecified", ""),
        description=description,
        full=bool(description),
        open=True,
        notes=[f"Team: {team}"] if team else [],
    )


async def read_board(web: Web, board: Board) -> list[Posting]:
    base = API.get(board.region, API[""])
    data = (await web.get(f"{base}/{board.token}?mode=json", max_bytes=25 * 1024 * 1024, timeout=40)).json()
    if not isinstance(data, list):
        raise FetchError("http", board.token, "Lever sent something other than a job list.")
    return [_posting(job, board.token, board.company, board.region) for job in data if isinstance(job, dict)]


async def find_region(web: Web, site: str) -> Optional[str]:
    """"" or "eu" if the site exists on that host, None if on neither."""
    for region in ("", "eu"):
        try:
            data = (await web.get(f"{API[region]}/{site}?mode=json&limit=1", timeout=15)).json()
        except FetchError:
            continue
        if isinstance(data, list):
            return region
    return None


async def read_job(web: Web, site: str, job_id: str, company: str = "", region: str = "") -> Posting:
    data = (await web.get(f"{API.get(region, API[''])}/{site}/{job_id}?mode=json", timeout=20)).json()
    if not isinstance(data, dict) or not data.get("text"):
        raise FetchError("http", job_id, "Lever sent something other than a job.")
    return _posting(data, site, company, region)
