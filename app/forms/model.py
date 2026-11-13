"""What the form engine knows about a page: its fields, buttons and step."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field as F, PrivateAttr

# Field kinds: text, email, tel, url, number, date, month, time, password, textarea,
# select, multiselect, radio, checkbox, checkbox_group, file, combobox, choice.
TEXT_KINDS = {"text", "email", "tel", "url", "number", "date", "month", "time", "password", "textarea"}
ONE_OF_KINDS = {"select", "radio", "combobox", "choice"}
MANY_OF_KINDS = {"multiselect", "checkbox_group"}


class Field(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    kind: str
    label: str = ""
    required: bool = False
    options: list[str] = F(default_factory=list)
    options_known: bool = False
    value: Any = ""
    name: str = ""
    id: str = ""
    placeholder: str = ""
    autocomplete: str = ""
    maxlength: Optional[int] = None
    disabled: bool = False
    readonly: bool = False
    visible: bool = True
    section: str = ""
    error: str = ""
    frame: str = "main"
    automation_id: str = ""
    multiple: bool = False
    accept: str = ""
    # Set by classify.py
    category: str = "other"
    rule: str = ""              # "never": never filled; "user": left for you; "" otherwise
    rule_reason: str = ""

    @property
    def empty(self) -> bool:
        return is_empty(self.value)


class Button(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    text: str
    tag: str = "button"
    type: str = ""
    disabled: bool = False
    automation_id: str = ""
    href: str = ""
    in_form: bool = False
    frame: str = "main"
    role: str = "other"         # submit, next, back, open_application, add, upload, other


class Reading(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = ""
    title: str = ""
    read_at: str = ""
    fields: list[Field] = F(default_factory=list)
    buttons: list[Button] = F(default_factory=list)
    headings: list[str] = F(default_factory=list)
    progress: str = ""
    captcha: bool = False
    frames: list[str] = F(default_factory=list)
    _frames: dict = PrivateAttr(default_factory=dict)   # frame key -> Playwright Frame, for the filler

    def field(self, key: str) -> Optional[Field]:
        return next((f for f in self.fields if f.key == key), None)

    def button(self, key: str) -> Optional[Button]:
        return next((b for b in self.buttons if b.key == key), None)

    def values(self) -> dict[str, Any]:
        return {f.key: f.value for f in self.fields}


def is_empty(value: Any) -> bool:
    return value in (None, "", False) or (isinstance(value, list) and not value)
