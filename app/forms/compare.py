"""What changed between two readings of the same form: how Job App learns what you typed (PLAN.md 8.2)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .model import Reading, is_empty


class Change(BaseModel):
    key: str
    label: str
    kind: str
    section: str = ""
    category: str = "other"
    change: str                 # filled, changed, cleared, added
    before: Any = None
    after: Any = None


def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return sorted(" ".join(str(v).split()) for v in value)
    return " ".join(str(value or "").split())


def changes(before: Reading, after: Reading) -> list[Change]:
    """Every field whose value differs, in the order the fields appear on the page now."""
    old = {f.key: f for f in before.fields}
    out = []
    for field in after.fields:
        previous = old.get(field.key)
        if previous is None:
            if not field.empty:
                out.append(Change(key=field.key, label=field.label, kind=field.kind, section=field.section,
                                  category=field.category, change="added", after=field.value))
            continue
        if _norm(previous.value) == _norm(field.value):
            continue
        if is_empty(previous.value):
            kind = "filled"
        elif field.empty:
            kind = "cleared"
        else:
            kind = "changed"
        out.append(Change(key=field.key, label=field.label, kind=field.kind, section=field.section,
                          category=field.category, change=kind, before=previous.value, after=field.value))
    return out
