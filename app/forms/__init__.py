"""The form engine: reads, fills and compares application forms with plain code (PLAN.md 8, Phase 4)."""

from .classify import blockers, classify
from .compare import Change, changes
from .fill import FillError, FillResult, click, fill, match_option
from .model import Button, Field, Reading
from .read import describe, read_page

__all__ = ["Button", "Change", "Field", "FillError", "FillResult", "Reading", "blockers", "changes", "classify",
           "click", "describe", "fill", "match_option", "read_page"]
