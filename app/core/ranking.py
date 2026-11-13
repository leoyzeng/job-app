"""The order of the New tab, and of what a search judges first.

Jobs are ranked strictly, in this order of importance:

1. Hard requirements: jobs that pass all of them come before jobs where some
   could not be verified (fewer unverified first).
2. Soft preferences: the weights of the preferences a job meets, minus those
   it clearly misses. One more preference met always outranks any resume match.
3. Resume match (if switched on in Settings): how well the job fits your skills,
   projects and past positions. It only decides between jobs that tie on 1 and 2.

Then companies are spread out: within the same hard-requirement tier, a
company appears again only after COMPANY_SPACING other companies, while there
are other companies to show. Each company's best job keeps its place.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional, Sequence, TypeVar

from .dedupe import company_key
from .models import Job

COMPANY_SPACING = 4

T = TypeVar("T")


def soft_level(job: Job) -> float:
    """What ranks on soft preferences. Jobs listed before 0.8.0 only have a 0-100 score: banded by tens."""
    match = job.match
    if match.soft_points is not None:
        return float(match.soft_points)
    if match.soft_score is not None:
        return float(round((match.soft_score - 50) / 10))
    return 0.0


def hard_tier(job: Job) -> int:
    unverified = getattr(job.match, "unverified", None)
    if unverified is None:
        unverified = [h for h in job.match.hard if h.result == "unknown"]
    return len(unverified)


def rank_key(job: Job, *, resume_match: bool = True) -> tuple:
    resume = (job.match.resume_score or 0) if resume_match else 0
    return (hard_tier(job), -soft_level(job), -resume, _neg_time(job.found_at))


def _neg_time(stamp: str) -> str:
    # Newest first as a tie-break: invert the ISO string's digits so ascending sort puts later times first.
    return "".join(chr(0x7E - ord(c)) if c.isdigit() else c for c in (stamp or ""))


def spread(items: Sequence[T], *, company: Callable[[T], str], tier: Callable[[T], object] = lambda _x: 0,
           spacing: int = COMPANY_SPACING, recent: Iterable[str] = ()) -> list[T]:
    """Reorder ranked ``items`` so a company comes again only after ``spacing`` others, within each tier.

    ``items`` must already be in rank order. ``recent`` are companies to treat as just shown
    (for a search: companies already in your list), so new companies go first.
    """
    if spacing <= 0:
        return list(items)
    out: list[T] = []
    remaining = list(items)
    last: list[str] = [company_key(c) for c in recent][-spacing:]
    while remaining:
        current_tier = tier(remaining[0])
        pick = None
        for index, item in enumerate(remaining):
            if tier(item) != current_tier:
                break
            if company_key(company(item)) not in last:
                pick = index
                break
        if pick is None:
            pick = 0
        item = remaining.pop(pick)
        key = company_key(company(item))
        out.append(item)
        if key in last:
            last.remove(key)
        last.append(key)
        last = last[-spacing:]
    return out


def order_new(jobs: Sequence[Job], *, resume_match: bool = True, spacing: int = COMPANY_SPACING) -> list[Job]:
    ranked = sorted(jobs, key=lambda j: rank_key(j, resume_match=resume_match))
    return spread(ranked, company=lambda j: j.company, tier=hard_tier, spacing=spacing)


def resume_score(level: str, skills: int) -> Optional[int]:
    base = {"strong": 70, "some": 45, "little": 20, "none": 0}.get(level)
    if base is None:
        return None
    return min(100, base + min(30, 6 * skills))


def spread_queue(items: Sequence[T], *, listed: set[str], watched: Sequence[str] = (),
                 company: Callable[[T], str] = lambda item: item[0].company,
                 spacing: int = COMPANY_SPACING) -> list[T]:
    """A search's judging order: companies not yet in your New list first (and companies you watch),
    then the rest, each part spread so a company comes again only after ``spacing`` others."""
    watched_keys = {company_key(w) for w in watched}

    def fresh(item: T) -> bool:
        key = company_key(company(item))
        return key not in listed or key in watched_keys

    first = [item for item in items if fresh(item)]
    later = [item for item in items if not fresh(item)]
    return spread(first, company=company, spacing=spacing) + spread(later, company=company, spacing=spacing)
