"""The shapes of the data files. See PLAN.md section 4."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def now_iso() -> str:
    """Local time with its UTC offset, to the second."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Status(str, Enum):
    NEW = "new"
    SKIPPED = "skipped"
    QUEUED = "queued"
    FILLING = "filling"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_PAGE_REVIEW = "waiting_for_page_review"
    WAITING_FOR_REVIEW = "waiting_for_review"
    APPLIED = "applied"
    CLOSED = "closed"
    ERROR = "error"


class _Open(BaseModel):
    # Unknown keys are kept, so a hand edit or a field added by a later
    # version survives a round trip through this version.
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Source(_Open):
    kind: str = "manual"
    found_via: str = "added by you"
    search_run: Optional[str] = None


class Details(_Open):
    pay: str = ""
    type: str = ""
    remote: str = ""
    deadline: Optional[str] = None


class HardResult(_Open):
    requirement: str
    result: Literal["pass", "fail", "unknown"]
    evidence: str = ""


class Match(_Open):
    hard: list[HardResult] = Field(default_factory=list)
    soft_score: Optional[int] = None            # 0 to 100, for display; 50 is neutral
    soft_points: Optional[int] = None           # weights of preferences met minus those not met: what ranks
    soft_hits: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    # How well the job fits your resume (only ranks jobs that tie on hard and soft): 0 to 100.
    resume_score: Optional[int] = None
    resume_level: str = ""                      # strong, some, little, none
    resume_skills: list[str] = Field(default_factory=list)
    resume_note: str = ""


class Need(_Open):
    label: str
    page: Optional[int] = None
    reason: str = ""


class StatusDetail(_Open):
    message: str = ""
    needs: list[Need] = Field(default_factory=list)


class HistoryEntry(_Open):
    at: str
    from_status: str = Field(alias="from")
    to: str
    by: str = "user"
    note: str = ""


class ApplicationRef(_Open):
    folder: str = ""
    page: Optional[int] = None
    pages_total: Optional[int] = None
    cost_usd: float = 0.0


class Job(_Open):
    id: str
    company: str
    title: str
    location: str = ""
    url: str
    apply_url: str = ""
    source: Source = Field(default_factory=Source)
    posted_at: Optional[str] = None
    found_at: str = Field(default_factory=now_iso)
    description_summary: str = ""
    details: Details = Field(default_factory=Details)
    match: Match = Field(default_factory=Match)
    dedupe_keys: list[str] = Field(default_factory=list)
    status: Status = Status.NEW
    status_detail: StatusDetail = Field(default_factory=StatusDetail)
    history: list[HistoryEntry] = Field(default_factory=list)
    application: ApplicationRef = Field(default_factory=ApplicationRef)

    def to_json(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)

    @property
    def status_since(self) -> str:
        return self.history[-1].at if self.history else self.found_at


# --- settings (PLAN.md 4.6) ---------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BrowserSettings(_Strict):
    mode: Literal["dedicated", "own_chrome"] = "dedicated"
    channel: Literal["chrome", "msedge", "chromium"] = "chrome"


class ModelSettings(_Strict):
    autofill: str = "sonnet"
    search: str = "sonnet"
    writer: str = "opus"


class BudgetSettings(_Strict):
    # With an API key these are real money; with a Claude plan they limit how much of the plan Job App uses.
    per_application: float = Field(default=1.0, ge=0)
    per_search: float = Field(default=1.5, ge=0)
    per_day: float = Field(default=5.0, ge=0)


class SearchSettings(_Strict):
    max_new_per_search: int = Field(default=25, ge=1, le=200)
    strict_hard_requirements: bool = False
    watchlist: list[str] = Field(default_factory=list)
    sources: dict[str, bool] = Field(default_factory=dict)
    # "HH:MM" to search once a day at that time while Job App is running; "" for never.
    daily_at: str = Field(default="", pattern=r"^$|^([01][0-9]|2[0-3]):[0-5][0-9]$")
    # Rank jobs that tie on your hard and soft requirements by how well they fit your resume.
    resume_match: bool = True


class AuthSettings(_Strict):
    # Each person signs in with their own account (see app/agents/auth.py and `jobapp signin`).
    # "subscription": your own Claude plan sign-in and its normal limits.
    # "api_key": your own Anthropic API key, kept in the system's password store; billed to your Console account.
    mode: Literal["subscription", "api_key"] = "subscription"


class Settings(_Strict):
    pause_after_each_page: bool = False
    write_long_answers: bool = True
    # Job App never clicks Next, Continue, Review, Apply or any button that moves to another page.
    never_click_next: bool = False
    # Recognise application questions by meaning (a question matcher agent), not only by wording.
    match_questions_by_meaning: bool = True
    # The Claude runtime keeps a transcript of every agent run under ~/.claude. Job App deletes its own unless this is on.
    keep_agent_transcripts: bool = False
    # Once a day, look at the project's GitHub Releases and show a notice when a newer version exists.
    check_for_updates: bool = True
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    budget_usd: BudgetSettings = Field(default_factory=BudgetSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    max_parallel_applications: int = Field(default=1, ge=1, le=4)
    auth: AuthSettings = Field(default_factory=AuthSettings)
