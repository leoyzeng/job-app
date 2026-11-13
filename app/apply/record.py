"""data/applications/<job id>/: everything about one application (PLAN.md 4.3).

* fields.json: every field met, its value, where the value came from, and
  whether you changed it;
* state.json: page, the "needs you" list, the agent's summary, review notes;
* snapshot.json: the form as the agent left it, which Continue compares with;
* latest.json: the form as the watcher last saw it, so typing survives a crash;
* events.log: every action, one line each;
* drafts/: drafted cover letters and long answers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.jsonfile import JsonCorrupt, read_json, write_json_atomic
from ..core.models import now_iso
from ..forms.model import Reading

log = logging.getLogger("jobapp.apply")

SOURCES = ("profile", "resume", "saved_answer", "ai_draft", "user", "site_prefill", "inferred")


class FieldRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    page: int = 1
    label: str = ""
    kind: str = ""
    category: str = "other"
    options: list[str] = Field(default_factory=list)
    required: bool = False
    value: Any = ""
    source: str = ""
    answer_id: str = ""
    match: str = ""             # exact or similar, for saved answers
    filled_by: str = ""         # code, agent, user, site
    verified: Optional[bool] = None
    user_changed: bool = False
    reason: str = ""
    updated_at: str = Field(default_factory=now_iso)


class NeedItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    reason: str = ""
    key: str = ""
    page: int = 1
    required: bool = True
    kind: str = "field"         # field or task


class AppState(BaseModel):
    model_config = ConfigDict(extra="allow")

    page: int = 1
    pages_done: list[int] = Field(default_factory=list)
    needs: list[NeedItem] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)
    summary: str = ""
    legs: int = 0
    cost_usd: float = 0.0
    looks_submitted: bool = False
    url: str = ""
    started_at: str = ""
    updated_at: str = ""
    questions: dict = Field(default_factory=dict)   # field key -> what the question matcher read in it


class ApplicationRecord:
    def __init__(self, folder: Path):
        self.folder = folder
        self.drafts = folder / "drafts"
        folder.mkdir(parents=True, exist_ok=True)
        self.fields: list[FieldRecord] = self._read_list("fields.json")
        self.state = self._read_state()

    # --- files ------------------------------------------------------------------------

    def _read_list(self, name: str) -> list[FieldRecord]:
        try:
            data = read_json(self.folder / name)
            return [FieldRecord.model_validate(x) for x in data.get("fields", [])]
        except FileNotFoundError:
            return []
        except (JsonCorrupt, ValidationError, AttributeError) as exc:
            log.error("%s/%s could not be read: %s", self.folder.name, name, exc)
            return []

    def _read_state(self) -> AppState:
        try:
            return AppState.model_validate(read_json(self.folder / "state.json"))
        except FileNotFoundError:
            return AppState(started_at=now_iso())
        except (JsonCorrupt, ValidationError) as exc:
            log.error("%s/state.json could not be read: %s", self.folder.name, exc)
            return AppState(started_at=now_iso())

    def save(self) -> None:
        self.state.updated_at = now_iso()
        write_json_atomic(self.folder / "fields.json", {"fields": [f.model_dump(mode="json") for f in self.fields]},
                          backup=False)
        write_json_atomic(self.folder / "state.json", self.state.model_dump(mode="json"), backup=False)

    def log(self, text: str) -> None:
        try:
            with (self.folder / "events.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{now_iso()} {' '.join(str(text).split())[:600]}\n")
        except OSError as exc:
            log.warning("Could not write events.log: %s", exc)

    def events(self, last: int = 12) -> list[str]:
        try:
            lines = (self.folder / "events.log").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return lines[-last:]

    def save_reading(self, name: str, reading: Reading) -> None:
        write_json_atomic(self.folder / name, reading.model_dump(mode="json"), backup=False)

    def load_reading(self, name: str) -> Optional[Reading]:
        try:
            return Reading.model_validate(read_json(self.folder / name))
        except (FileNotFoundError, JsonCorrupt, ValidationError):
            return None

    # --- fields -------------------------------------------------------------------------

    def field(self, key: str) -> Optional[FieldRecord]:
        return next((f for f in self.fields if f.key == key), None)

    def upsert(self, key: str, **values) -> FieldRecord:
        record = self.field(key)
        if record is None:
            record = FieldRecord(key=key, page=self.state.page)
            self.fields.append(record)
        for name, value in values.items():
            setattr(record, name, value)
        record.updated_at = now_iso()
        return record

    # --- the "needs you" list ---------------------------------------------------------------

    def add_need(self, need: NeedItem) -> None:
        for existing in self.state.needs:
            if (need.key and existing.key == need.key) or (not need.key and existing.label == need.label):
                existing.reason, existing.required = need.reason or existing.reason, need.required
                return
        self.state.needs.append(need)

    def drop_need(self, key: str = "", label: str = "") -> None:
        self.state.needs = [n for n in self.state.needs
                            if not ((key and n.key == key) or (label and not n.key and n.label == label))]

    def required_needs(self) -> list[NeedItem]:
        return [n for n in self.state.needs if n.required]

    # --- drafts ---------------------------------------------------------------------------

    def save_draft(self, name: str, text: str) -> Path:
        self.drafts.mkdir(parents=True, exist_ok=True)
        path = self.drafts / name
        path.write_text(text, encoding="utf-8")
        return path

    def summary_json(self) -> str:
        return json.dumps(self.state.model_dump(mode="json"), indent=1)
