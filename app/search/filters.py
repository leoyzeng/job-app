"""Quick checks on a posting's title and place, made before anything costs an agent's time.

They only drop postings that clearly cannot fit; everything else goes on to
be read in full and judged by the fit evaluator.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .models import Posting, SearchPlan
from .text import in_us


def _has(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    # Whole words, so "ml" does not match "html" and "intern" does not match "internal".
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def _has_any(text: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if _has(text, p)]


def title_problem(plan: SearchPlan, posting: Posting) -> Optional[str]:
    """Why the title cannot fit, or None."""
    title = " ".join(posting.title.lower().replace("_", " ").split())
    if not title:
        return "no title"
    if plan.kind_words:
        kind_text = f"{title} {posting.type.lower()}"
        if not _has_any(kind_text, plan.kind_words):
            return "title is not the kind of job you want"
    if plan.role_words and not _has_any(title, plan.role_words):
        return "title is outside the fields you want"
    excluded = _has_any(title, plan.exclude_words)
    if excluded:
        return f"title says {excluded[0]}"
    if plan.years:
        named = set(re.findall(r"(?<!\d)(20\d\d)(?!\d)", title))
        if named and not named.intersection(plan.years):
            return f"title names {', '.join(sorted(named))}"
    return None


def place_problem(plan: SearchPlan, posting: Posting) -> Optional[str]:
    if "US" not in plan.countries or len(plan.countries) != 1:
        return None
    if in_us(posting.location) is False:
        return "outside the US"
    return None


def problem(plan: SearchPlan, posting: Posting) -> Optional[str]:
    return title_problem(plan, posting) or place_problem(plan, posting)


def role_matches(plan: SearchPlan, posting: Posting) -> int:
    return len(_has_any(posting.title.lower(), plan.role_words))


def priority(plan: SearchPlan, posting: Posting, *, watched: bool, today: Optional[date] = None) -> float:
    """Which candidates to read and judge first, when there are more than one search can afford."""
    today = today or date.today()
    score = 0.0
    if watched:
        score += 4
    if posting.source in ("greenhouse", "lever", "ashby", "workday"):
        score += 1.5
    if posting.full:
        score += 0.5
    score += min(2, role_matches(plan, posting))
    if posting.posted_at:
        try:
            age = (today - date.fromisoformat(posting.posted_at[:10])).days
        except ValueError:
            age = None
        if age is not None:
            score += 2 if age <= 14 else 1 if age <= 45 else 0 if age <= 120 else -1
    if in_us(posting.location) is True and "US" in plan.countries:
        score += 0.5
    return score
