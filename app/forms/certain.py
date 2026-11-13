"""Fields plain code can fill with certainty from your confirmed profile (PLAN.md 8.1, step 3).

Only these: first, last and full name, email, phone, LinkedIn, GitHub,
website, city, country, and your resume file. Everything else (education
choices, questions, anything sensitive) is for the application agent or for
you. A field the site has already filled is left as it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from ..profile.models import Facts
from .model import Reading


class Planned(BaseModel):
    key: str
    label: str
    value: Any
    source: str = "profile"
    what: str = ""


def _link(value: str) -> str:
    value = (value or "").strip()
    if value and not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    return value


def certain_values(reading: Reading, facts: Facts, resume: Optional[Path]) -> tuple[list[Planned], list[dict]]:
    """(what to fill, fields left as the site already filled them)."""
    name, contact, links = facts.name, facts.contact, facts.links
    full = name.full or " ".join(p for p in (name.first, name.last) if p)
    place = ", ".join(p for p in (contact.city, contact.state) if p)
    by_category = {
        "first_name": (name.first, "first name"),
        "last_name": (name.last, "last name"),
        "full_name": (full, "full name"),
        "email": (contact.email, "email"),
        "phone": (contact.phone, "phone"),
        "linkedin": (_link(links.linkedin), "LinkedIn"),
        "github": (_link(links.github), "GitHub"),
        "website": (_link(links.website), "website"),
        "city": (place, "city"),
        "country": (contact.country, "country"),
    }
    planned, kept = [], []
    seen_categories: dict[str, int] = {}
    for field in reading.fields:
        if field.rule or field.disabled or field.readonly:
            continue
        category = field.category
        if category == "resume" and field.kind == "file" and resume is not None:
            if field.empty:
                planned.append(Planned(key=field.key, label=field.label, value=str(resume), source="resume",
                                       what="resume file"))
            continue
        if category not in by_category:
            continue
        value, what = by_category[category]
        if not value:
            continue
        if category == "country" and field.kind not in ("select", "combobox"):
            continue
        if field.kind in ("checkbox", "checkbox_group", "radio", "choice", "multiselect", "file"):
            continue
        # The same kind of field twice (say, two "Website" fields): only the first is certain.
        seen_categories[category] = seen_categories.get(category, 0) + 1
        if seen_categories[category] > 1:
            continue
        if not field.empty:
            kept.append({"key": field.key, "label": field.label, "value": field.value})
            continue
        planned.append(Planned(key=field.key, label=field.label, value=value, what=what))
    return planned, kept
