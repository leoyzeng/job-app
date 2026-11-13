"""Checks that this computer can run every part of Job App (PLAN.md, Phase 0)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Result:
    name: str
    status: str            # "ok", "warn", "fail" or "skip"
    summary: str           # one plain sentence
    fix: str = ""          # what to do about it, if anything
    details: dict = field(default_factory=dict)  # goes to the log file only
