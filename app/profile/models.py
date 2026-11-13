"""Shapes of the profile files. PLAN.md 4.1 and 4.5.

``profile/profile.json``         facts about you, read from the resume and confirmed by you
``profile/search_profile.json``  the app's structured reading of your requirements
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _In(BaseModel):
    """Tolerant input: unknown keys are dropped and a null means "not given"."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _nulls_are_missing(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if item is not None}
        return value


def _clean_list(value: Any) -> Any:
    """Accept a list of strings, or one string with commas or line breaks."""
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    if isinstance(value, list):
        seen, out = set(), []
        for item in value:
            text = " ".join(str(item).split())
            if text and text.lower() not in seen:
                seen.add(text.lower())
                out.append(text)
        return out
    return value


def _clean_lines(value: Any) -> Any:
    """Like _clean_list, but a string is split on line breaks only, so an entry may contain commas."""
    if isinstance(value, str):
        value = value.split("\n")
    if isinstance(value, list):
        seen, out = set(), []
        for item in value:
            text = " ".join(str(item).split()).lstrip("-•* ").strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                out.append(text)
        return out
    return value


# --- facts about you -----------------------------------------------------------------


class Name(_In):
    first: str = ""
    last: str = ""
    full: str = ""


class Contact(_In):
    email: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    country: str = ""


class Links(_In):
    linkedin: str = ""
    github: str = ""
    website: str = ""


class Education(_In):
    school: str = ""
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    end_is_expected: bool = False
    gpa: str = ""
    honors: list[str] = Field(default_factory=list)

    _tidy = field_validator("honors", mode="before")(_clean_lines)


class Experience(_In):
    employer: str = ""
    title: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    current: bool = False
    highlights: list[str] = Field(default_factory=list)

    _tidy = field_validator("highlights", mode="before")(_clean_lines)


class Project(_In):
    name: str = ""
    summary: str = ""
    tools: list[str] = Field(default_factory=list)

    _tidy = field_validator("tools", mode="before")(_clean_list)


class Facts(_In):
    name: Name = Field(default_factory=Name)
    contact: Contact = Field(default_factory=Contact)
    links: Links = Field(default_factory=Links)
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    _tidy = field_validator("skills", mode="before")(_clean_list)
    _tidy_lines = field_validator("certifications", mode="before")(_clean_lines)


class ToConfirm(_In):
    field: str = ""
    question: str


class BuilderOutput(Facts):
    """What the profile builder agent returns."""

    to_confirm: list[ToConfirm] = Field(default_factory=list)


class ResumeSource(_In):
    file: str = ""
    sha256: str = ""
    read_at: str = ""


class ProfileFile(_In):
    version: int = 1
    source: ResumeSource = Field(default_factory=ResumeSource)
    confirmed: bool = False
    confirmed_at: Optional[str] = None
    facts: Facts = Field(default_factory=Facts)
    to_confirm: list[ToConfirm] = Field(default_factory=list)


# --- your requirements, as the app reads them -------------------------------------------


class HardItem(_In):
    text: str
    test: str = ""


class SoftItem(_In):
    text: str
    weight: int = 2

    @field_validator("weight", mode="before")
    @classmethod
    def _in_range(cls, value: Any) -> Any:
        try:
            return min(3, max(1, int(value)))
        except (TypeError, ValueError):
            return 2


class Ambiguous(_In):
    text: str
    question: str = ""


class SearchReading(_In):
    hard: list[HardItem] = Field(default_factory=list)
    soft: list[SoftItem] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: Optional[bool] = None
    pay_floor: str = ""
    types: list[str] = Field(default_factory=list)
    ambiguous: list[Ambiguous] = Field(default_factory=list)

    _tidy = field_validator("titles", "keywords", "types", mode="before")(_clean_list)
    _tidy_lines = field_validator("locations", mode="before")(_clean_lines)

    @field_validator("pay_floor", mode="before")
    @classmethod
    def _pay_as_text(cls, value: Any) -> Any:
        return "" if value is None else str(value)


class ReadingFile(_In):
    version: int = 1
    source_sha256: str = ""
    read_at: str = ""
    confirmed: bool = False
    confirmed_at: Optional[str] = None
    # Lines you have said are in the right box, so they are not questioned again.
    settled: list[str] = Field(default_factory=list)
    reading: SearchReading = Field(default_factory=SearchReading)


# --- JSON schema for the agents -----------------------------------------------------------


def agent_schema(model: type[BaseModel]) -> dict:
    """The model's JSON schema with references inlined and every key required.

    Structured-output validators differ in how much of JSON Schema they
    support, so the schema handed to the agent uses only the plainest parts.
    """
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].split("/")[-1]])
        out = {key: walk(value) for key, value in node.items() if key not in ("title", "default", "$defs")}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(raw)
