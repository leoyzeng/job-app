"""Global settings, kept in ``config/settings.json``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .jsonfile import JsonCorrupt, read_json, write_json_atomic
from .models import Settings

log = logging.getLogger("jobapp.settings")


class BadSettings(ValueError):
    """A change was rejected. ``problems`` maps a setting to what is wrong."""

    def __init__(self, problems: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems


def _problems(exc: ValidationError) -> dict[str, str]:
    return {".".join(str(part) for part in err["loc"]): err["msg"] for err in exc.errors()}


def load_settings(path: Path) -> Settings:
    """Read the settings, creating the file with defaults the first time.

    If the file is damaged the defaults are used for this run and the file is
    left alone, so a typo made while hand-editing is not silently overwritten.
    """
    try:
        raw = read_json(path)
    except FileNotFoundError:
        settings = Settings()
        write_json_atomic(path, settings.model_dump(mode="json"), backup=False)
        return settings
    except JsonCorrupt as exc:
        log.error("Settings file could not be read, using defaults: %s", exc)
        return Settings()
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        log.error("Settings file has invalid values, using defaults: %s", _problems(exc))
        return Settings()


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def update_settings(path: Path, patch: dict[str, Any]) -> Settings:
    """Apply a partial change, check it, and save. Raises BadSettings."""
    if not isinstance(patch, dict):
        raise BadSettings({"settings": "Expected an object of settings to change."})
    current = load_settings(path).model_dump(mode="json")
    unknown = [key for key in patch if key not in current]
    if unknown:
        raise BadSettings({key: "There is no setting with this name." for key in unknown})
    try:
        settings = Settings.model_validate(_merge(current, patch))
    except ValidationError as exc:
        raise BadSettings(_problems(exc)) from exc
    write_json_atomic(path, settings.model_dump(mode="json"))
    return settings
