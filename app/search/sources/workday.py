"""Workday: career sites at {company}.wd{N}.myworkdayjobs.com.

Workday pages are drawn by JavaScript, so their plain HTML says almost
nothing. The sites load their data from a JSON service on the same host,
which is what this reads:

Search: POST https://{host}/wday/cxs/{tenant}/{site}/jobs  {"searchText": "...", "limit": 20, "offset": 0}
One job: GET https://{host}/wday/cxs/{tenant}/{site}/job/{...}

This service is not a published API, so it is read gently (a few pages per
search) and any failure is reported and skipped.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from ..http import FetchError, Web
from ..models import Board, Posting, pretty_name
from ..text import html_to_text

_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
MAX_PAGES = 5
PAGE = 20


def identify(url: str) -> Optional[dict]:
    """{"token": "host|tenant|site", "job_path": "/job/..." or ""}."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    match = _HOST.match(host)
    if not match:
        return None
    tenant = match.group(1)
    segments = [s for s in parts.path.split("/") if s]
    if segments and _LOCALE.match(segments[0]):
        segments = segments[1:]
    if segments[:2] == ["wday", "cxs"] and len(segments) >= 4:
        segments = segments[3:]
    if not segments:
        return None
    site = segments[0]
    job_path = ""
    rest = segments[1:]
    if rest and rest[0] in ("job", "details") and len(rest) >= 2:
        tail = [s for s in rest[1:] if s not in ("apply", "applyManually", "autofillWithResume", "useMyLastApplication")]
        if tail:
            job_path = "/job/" + "/".join(tail)
    return {"token": f"{host}|{tenant}|{site}", "job_id": job_path, "job_path": job_path}


def _split(token: str) -> tuple[str, str, str]:
    host, tenant, site = token.split("|")
    return host, tenant, site


def page_url(token: str, job_path: str) -> str:
    host, _tenant, site = _split(token)
    return f"https://{host}/{site}{job_path}"


async def search(web: Web, board: Board, words: list[str]) -> list[Posting]:
    """Postings whose title matches the search words, title and place only (no description yet)."""
    host, tenant, site = _split(board.token)
    found: dict[str, Posting] = {}
    for text in (words or [""])[:3]:
        for page in range(MAX_PAGES):
            data = (await web.post_json(
                f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                {"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": text},
                timeout=25,
            )).json()
            if not isinstance(data, dict):
                raise FetchError("http", board.token, "Workday sent something other than a job list.")
            items = data.get("jobPostings") or []
            for item in items:
                path = item.get("externalPath") or ""
                if not path or path in found:
                    continue
                found[path] = Posting(
                    company=board.company or pretty_name(tenant),
                    title=(item.get("title") or "").strip(),
                    location=item.get("locationsText") or "",
                    url=page_url(board.token, path),
                    source="workday",
                    found_via=f"{board.company or pretty_name(tenant)} careers (Workday)",
                    board=f"workday:{board.token.lower()}",
                    external_id=path,
                    open=True,
                )
            total = data.get("total") or 0
            if len(items) < PAGE or (page + 1) * PAGE >= total:
                break
    return list(found.values())


async def read_job(web: Web, token: str, job_path: str, company: str = "") -> Posting:
    host, tenant, site = _split(token)
    data = (await web.get(f"https://{host}/wday/cxs/{tenant}/{site}{job_path}", timeout=25)).json()
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    if not isinstance(info, dict) or not info.get("title"):
        raise FetchError("http", job_path, "Workday sent something other than a job.")
    places = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    organisation = (data.get("hiringOrganization") or {}).get("name") or ""
    description = html_to_text(info.get("jobDescription") or "")
    notes = []
    if info.get("startDate"):
        notes.append(f"Workday lists a start date of {info['startDate']}")
    return Posting(
        company=company or organisation or pretty_name(tenant),
        title=info["title"].strip(),
        location="; ".join(dict.fromkeys(p for p in places if p)),
        url=info.get("externalUrl") or page_url(token, job_path),
        source="workday",
        found_via=f"{company or organisation or pretty_name(tenant)} careers (Workday)",
        board=f"workday:{token.lower()}",
        external_id=job_path,
        type=info.get("timeType") or "",
        description=description,
        full=bool(description),
        open=info.get("canApply", True) is not False,
        notes=notes,
    )
