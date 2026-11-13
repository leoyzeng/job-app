"""Shapes used while searching: a candidate posting, a company's job board, the plan, and the run log."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


def iso_day(value) -> str:
    """A date as YYYY-MM-DD from the many shapes the sources use, or ""."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            number = float(value)
            if number > 10_000_000_000:  # milliseconds
                number /= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc).date().isoformat()
        text = str(value).strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
    except (ValueError, OSError, OverflowError):
        return ""
    return ""


def pretty_name(token: str) -> str:
    """"northwind-ai" -> "Northwind AI": a board's name, readable, for when the source gives no company name."""
    words = [w for w in re.split(r"[-_.\s]+", token or "") if w]
    return " ".join(w.upper() if len(w) <= 2 else w[:1].upper() + w[1:] for w in words)


class Posting(_M):
    """One job found by the search, before it becomes a tracker entry."""

    company: str
    title: str
    location: str = ""
    url: str                          # the posting page, the employer's own when known
    apply_url: str = ""
    source: str = "web"               # greenhouse, lever, ashby, workday, list, web, page
    found_via: str = ""               # in plain words, for the listing
    board: str = ""                   # "greenhouse:anduril" when it came from or maps to a known board
    external_id: str = ""
    posted_at: str = ""
    pay: str = ""
    type: str = ""                    # internship, full time... as the source states it
    remote: str = ""                  # remote, hybrid, on site, as stated
    description: str = ""             # plain text
    full: bool = False                # True when description is the whole posting
    open: Optional[bool] = None       # None until checked
    terms: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)   # facts the source adds, e.g. "Does not offer sponsorship"
    hint: dict = Field(default_factory=dict)         # what a web agent said about it

    def label(self) -> str:
        return f"{self.title} at {self.company}"


class Board(_M):
    """A company's job board on a system the app can read."""

    ats: Literal["greenhouse", "lever", "ashby", "workday"]
    token: str                        # board name; for Workday "host|tenant|site"
    company: str = ""
    region: str = ""                  # Lever: "eu" for the EU host
    follow: bool = False              # read on every search
    source: str = ""                  # watchlist, suggested, found, added
    verified_at: str = ""
    last_read_at: str = ""
    last_count: Optional[int] = None
    last_error: str = ""

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.token.lower()}"


class CompanyEntry(_M):
    name: str
    boards: list[str] = Field(default_factory=list)   # board keys
    checked_at: str = ""                              # last time boards were looked for
    note: str = ""


class BoardsFile(_M):
    version: int = 1
    boards: dict[str, Board] = Field(default_factory=dict)
    companies: dict[str, CompanyEntry] = Field(default_factory=dict)


class SearchPlan(_M):
    """What the planner agent derived from the requirements, checked by code."""

    queries: list[str] = Field(default_factory=list)
    kind_words: list[str] = Field(default_factory=list)      # a title must contain one, if any are given
    role_words: list[str] = Field(default_factory=list)      # a title must contain one, if any are given
    exclude_words: list[str] = Field(default_factory=list)   # a title with any of these is dropped
    countries: list[str] = Field(default_factory=list)       # "US"; empty means anywhere
    years: list[str] = Field(default_factory=list)           # a title naming another year is dropped
    terms: list[str] = Field(default_factory=list)           # "Summer 2027", for internship lists
    lists: list[str] = Field(default_factory=list)           # "internships", "new_grad"
    companies: list[dict] = Field(default_factory=list)      # {"name", "why"}
    notes: str = ""


class PlanFile(_M):
    version: int = 1
    fingerprint: str = ""
    made_at: str = ""
    plan: SearchPlan = Field(default_factory=SearchPlan)


class SeenEntry(_M):
    outcome: str                      # rejected, closed, unreadable, unverified
    reason: str = ""
    at: str = ""
    plan: str = ""                    # requirements fingerprint it was judged against
    label: str = ""


class SeenFile(_M):
    version: int = 1
    entries: dict[str, SeenEntry] = Field(default_factory=dict)
