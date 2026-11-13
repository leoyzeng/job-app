"""Your requirements, in your words (``profile/requirements.md``), and the
rules that keep the app's reading of them faithful to what you wrote.

The agent that reads your requirements may add how each one can be tested,
weights, and search terms. It may not add, drop, reword or move a requirement.
``reconcile`` enforces that in code: the lists always come from your lines.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .models import Ambiguous, HardItem, SearchReading, SoftItem

HARD_HEADING = "## Hard requirements"
SOFT_HEADING = "## Soft preferences"
MAX_LINES = 40
MAX_LINE_CHARS = 400


class BadRequirements(ValueError):
    pass


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9$%+.]+", " ", text.lower()).split())


def clean_lines(lines) -> list[str]:
    """One requirement per line: trimmed, bullets removed, blanks and repeats dropped."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    if not isinstance(lines, list):
        raise BadRequirements("Expected a list of lines.")
    out, seen = [], set()
    for raw in lines:
        if not isinstance(raw, str):
            raise BadRequirements("Each requirement should be a line of text.")
        text = " ".join(raw.split())
        text = re.sub(r"^(?:[-*•]+|\d+[.)])\s*", "", text)
        if not text or norm(text) in seen:
            continue
        if len(text) > MAX_LINE_CHARS:
            raise BadRequirements(f"Keep each line under {MAX_LINE_CHARS} characters. This one is too long: {text[:60]}...")
        seen.add(norm(text))
        out.append(text)
    if len(out) > MAX_LINES:
        raise BadRequirements(f"Keep each box to {MAX_LINES} lines or fewer.")
    return out


def format_requirements(hard: list[str], soft: list[str]) -> str:
    def block(lines: list[str]) -> str:
        return "".join(f"- {line}\n" for line in lines) if lines else ""

    return f"{HARD_HEADING}\n{block(hard)}\n{SOFT_HEADING}\n{block(soft)}"


def parse_requirements(text: str) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    soft: list[str] = []
    bucket = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            bucket = hard if heading.startswith("hard") else soft if heading.startswith("soft") else None
            continue
        if bucket is not None and line:
            bucket.append(line)
    return clean_lines(hard), clean_lines(soft)


def read_requirements(path: Path) -> tuple[list[str], list[str]]:
    try:
        return parse_requirements(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return [], []


def write_requirements(path: Path, hard: list[str], soft: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_requirements(hard, soft), encoding="utf-8", newline="\n")


def fingerprint(hard: list[str], soft: list[str]) -> str:
    """Changes exactly when the meaning of the two boxes changes."""
    joined = "\n".join(["H"] + [norm(line) for line in hard] + ["S"] + [norm(line) for line in soft])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def reconcile(reading: SearchReading, hard: list[str], soft: list[str], settled: list[str]) -> SearchReading:
    """Rebuild the reading's lists from the user's own lines.

    Whatever the agent returned, a hard requirement is exactly a line from the
    hard box and a soft preference exactly a line from the soft box.
    """
    tests = {norm(item.text): item.test for item in reading.hard}
    weights = {norm(item.text): item.weight for item in reading.soft}
    mine = {norm(line) for line in hard + soft}
    done = {norm(line) for line in settled}
    reading.hard = [HardItem(text=line, test=tests.get(norm(line), "")) for line in hard]
    reading.soft = [SoftItem(text=line, weight=weights.get(norm(line), 2)) for line in soft]
    kept: list[Ambiguous] = []
    for item in reading.ambiguous:
        key = norm(item.text)
        if key in mine and key not in done and key not in {norm(a.text) for a in kept}:
            # Show the user's own wording, not the agent's copy of it.
            original = next(line for line in hard + soft if norm(line) == key)
            kept.append(Ambiguous(text=original, question=item.question))
    reading.ambiguous = kept
    return reading
