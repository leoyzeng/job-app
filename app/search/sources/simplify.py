"""Community job lists kept on GitHub by Simplify and the Pitt Computer Science Club.

Summer internships: github.com/SimplifyJobs/Summer{YEAR}-Internships (also holds
off-season terms). New-grad roles: github.com/SimplifyJobs/New-Grad-Positions.
Each repository keeps every listing in .github/scripts/listings.json, with
company_name, title, locations, url, terms, active, is_visible, sponsorship,
date_posted (Unix seconds) and more. These are public lists meant to be read.
"""

from __future__ import annotations

import re

from ..http import FetchError, Web
from ..models import Posting, iso_day

RAW = "https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/.github/scripts/listings.json"
MAX_BYTES = 60 * 1024 * 1024


def repos_for(lists: list[str], terms: list[str]) -> list[tuple[str, str]]:
    """(repo, label) pairs to read for the plan's lists and terms."""
    out: list[tuple[str, str]] = []
    if "internships" in lists:
        years = set()
        for term in terms:
            match = re.search(r"(20\d\d)", term)
            if not match:
                continue
            year = int(match.group(1))
            years.add(year)
            if not term.lower().startswith("summer"):
                years.add(year + 1)  # off-season terms are kept with the next summer's list
        for year in sorted(years)[:2]:
            out.append((f"Summer{year}-Internships", f"Summer {year} internship list (Simplify, GitHub)"))
    if "new_grad" in lists:
        out.append(("New-Grad-Positions", "New grad list (Simplify, GitHub)"))
    return out


def _notes(item: dict) -> list[str]:
    notes = []
    sponsorship = str(item.get("sponsorship") or "")
    if sponsorship and sponsorship.lower() not in ("other", "none", "null"):
        notes.append(f"The list says: {sponsorship}")
    degrees = item.get("degrees") or []
    if degrees:
        notes.append("The list says degrees: " + ", ".join(map(str, degrees)))
    return notes


async def read_list(web: Web, repo: str, label: str, terms: list[str]) -> list[Posting]:
    data = (await web.get(RAW.format(repo=repo), max_bytes=MAX_BYTES, timeout=60)).json()
    if not isinstance(data, list):
        raise FetchError("http", repo, "The list was not in the expected format.")
    wanted = {t.lower() for t in terms}
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("active") is False or item.get("is_visible") is False:
            continue
        item_terms = [str(t) for t in item.get("terms") or []]
        if wanted and item_terms and not wanted.intersection(t.lower() for t in item_terms):
            continue
        url = (item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        out.append(Posting(
            company=(item.get("company_name") or "").strip(),
            title=(item.get("title") or "").strip(),
            location="; ".join(str(x) for x in item.get("locations") or []),
            url=url,
            source="list",
            found_via=label,
            posted_at=iso_day(item.get("date_posted")),
            terms=item_terms,
            notes=_notes(item),
            open=True,
        ))
    return out
