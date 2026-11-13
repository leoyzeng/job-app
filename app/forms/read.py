"""Reading a whole page: every frame, merged into one Reading."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from ..core.models import now_iso
from .classify import classify
from .model import Button, Field, Reading

log = logging.getLogger("jobapp.forms")

READER = (Path(__file__).with_name("reader.js")).read_text(encoding="utf-8")


def _frame_keys(frames) -> list[str]:
    keys, seen = [], {}
    for index, frame in enumerate(frames):
        if index == 0:
            keys.append("main")
            continue
        parts = urlsplit(frame.url or "")
        base = f"{parts.netloc}{parts.path}" or "about:blank"
        seen[base] = seen.get(base, 0) + 1
        keys.append(f"{base}#{seen[base]}")
    return keys


async def read_page(page) -> Reading:
    """Describe every field, button and heading on the page, in every frame."""
    frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame and not f.is_detached()]
    keys = _frame_keys(frames)
    reading = Reading(url=page.url, read_at=now_iso())
    for frame, key in zip(frames, keys):
        try:
            data = await frame.evaluate(READER, {"frameKey": key})
        except Exception as exc:  # noqa: BLE001 - a frame that is loading or gone has nothing to read
            log.debug("Could not read frame %s: %s", key, str(exc).splitlines()[0][:200])
            continue
        if key == "main":
            reading.title = data.get("title", "")
            reading.progress = data.get("progress", "")
        elif not reading.progress and data.get("progress"):
            reading.progress = data["progress"]
        if not data.get("fields") and not data.get("buttons"):
            continue
        reading.frames.append(key)
        reading._frames[key] = frame
        reading.fields.extend(Field.model_validate(f) for f in data.get("fields", []))
        reading.buttons.extend(Button.model_validate(b) for b in data.get("buttons", []))
        reading.headings.extend(h for h in data.get("headings", []) if h not in reading.headings)
        reading.captcha = reading.captcha or bool(data.get("captcha"))
    classify(reading)
    # Passwords, payment details and ID numbers are only ever yours: what you typed into them is not
    # kept in any reading (and so never saved to disk, shown to an agent, or learned as an answer).
    for field in reading.fields:
        if field.rule == "never" and not field.empty:
            field.value = "(entered)"
    return reading


def describe(reading: Reading) -> dict:
    """A compact, JSON-friendly description of the page, for logs and (later) for the application agent."""
    return {
        "url": reading.url, "title": reading.title, "progress": reading.progress, "captcha": reading.captcha,
        "headings": reading.headings[:20],
        "fields": [
            {k: v for k, v in {
                "key": f.key, "label": f.label, "kind": f.kind, "required": f.required or None,
                "options": f.options or None, "value": f.value if not f.empty else None, "section": f.section or None,
                "error": f.error or None, "category": f.category if f.category != "other" else None,
                "rule": f.rule or None, "hidden": True if not f.visible else None,
            }.items() if v is not None}
            for f in reading.fields
        ],
        "buttons": [{"key": b.key, "text": b.text, "role": b.role, "disabled": b.disabled or None} for b in reading.buttons],
    }
