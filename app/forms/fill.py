"""Filling fields, the way each kind of control needs, and checking that every value took.

Each fill starts from a fresh reading, so the fields are found where they
are now, not where they were. After filling, the page is read again and each
field's value is compared with what was meant. Fields the rules in
classify.py mark as never filled or left for you are refused here, whoever
asks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel

from .model import Field, Reading, is_empty
from .read import read_page

log = logging.getLogger("jobapp.forms")

OPTION_WAIT = 5.0


class FillResult(BaseModel):
    key: str
    label: str = ""
    kind: str = ""
    status: str                     # ok, mismatch, failed, refused, missing
    wanted: Any = None
    now: Any = None
    message: str = ""


class FillError(Exception):
    pass


# --- matching a wanted value to the options on offer -----------------------------------------------

_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
    "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey",
    "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina", "sd": "south dakota",
    "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
    "us": "united states", "usa": "united states",
}


def norm(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", str(text or "").lower()).split())


def _tokens(text: str) -> set[str]:
    out = set()
    for word in norm(text).split():
        out.update(_STATES.get(word, word).split())
    return out


def match_option(wanted: Any, options: Iterable[str]) -> Optional[str]:
    """The one option that means the wanted value, or None if there is no single clear match."""
    options = [o for o in options if o]
    w = norm(wanted)
    if not w:
        return None
    for test in (
        lambda o: norm(o) == w,
        lambda o: norm(o).startswith(w + " "),
        lambda o: f" {w} " in f" {norm(o)} ",
        lambda o: w.startswith(norm(o) + " ") and len(norm(o)) >= 3,
        lambda o: _tokens(wanted) <= _tokens(o),
    ):
        found = [o for o in options if test(o)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            exact = [o for o in found if norm(o) == w]
            if len(exact) == 1:
                return exact[0]
            return None
    return None


def search_text(wanted: str) -> str:
    """What to type into a searchable dropdown: enough to narrow it, not so much that nothing matches."""
    text = str(wanted).strip()
    head = text.split(",")[0].strip()
    return (head or text)[:40]


def _as_date(value: str) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    for fmt in ("%m/%d/%Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise FillError(f"'{text}' is not a date this field accepts (it needs a full date).")


# --- the strategies --------------------------------------------------------------------------------


def _css(key: str) -> str:
    return f'[data-jobapp-key="{key}"]'


async def _click_js(locator) -> None:
    await locator.evaluate("e => e.click()")


async def _click(locator) -> None:
    """A normal click, or the click event itself for controls the browser will not click."""
    try:
        await locator.click(timeout=4000)
    except Exception:  # noqa: BLE001
        await _click_js(locator)


async def _fill_text(frame, field: Field, value: Any) -> None:
    loc = frame.locator(_css(field.key)).first
    text = "" if value is None else str(value)
    if field.kind == "date":
        text = _as_date(text)
    if field.kind == "textarea" and await loc.evaluate("e => e.isContentEditable && e.tagName !== 'TEXTAREA'"):
        await _click(loc)
        await loc.evaluate("e => { e.innerText = ''; }")
        await loc.press_sequentially(text)
    else:
        await loc.fill(text, timeout=8000)
    try:
        await loc.blur()
    except Exception:  # noqa: BLE001 - validation on blur is a nicety
        pass


async def _fill_select(frame, field: Field, value: Any) -> None:
    loc = frame.locator(_css(field.key)).first
    if field.kind == "multiselect":
        wanted = value if isinstance(value, list) else [value]
        chosen = []
        for item in wanted:
            option = match_option(item, field.options)
            if option is None:
                raise FillError(f"None of the choices matches '{item}'.")
            chosen.append(option)
        await loc.select_option(label=chosen, timeout=8000)
        return
    option = match_option(value, field.options)
    if option is None:
        raise FillError(f"None of the choices matches '{value}'.")
    await loc.select_option(label=option, timeout=8000)


async def _check(frame, selector: str, state: bool) -> None:
    loc = frame.locator(selector).first
    try:
        await loc.set_checked(state, timeout=3000)
    except Exception:  # noqa: BLE001 - inputs hidden behind styled labels
        if await loc.evaluate("e => e.checked === undefined ? e.getAttribute('aria-checked') === 'true' : e.checked") != state:
            await _click_js(loc)


async def _fill_radio(frame, field: Field, value: Any) -> None:
    option = match_option(value, field.options)
    if option is None:
        raise FillError(f"None of the choices matches '{value}'.")
    index = field.options.index(option)
    selector = _css(f"{field.key}#{index}")
    loc = frame.locator(selector).first
    if await loc.evaluate("e => e.getAttribute('role') === 'radio'"):
        await _click(loc)
    else:
        await _check(frame, selector, True)


async def _fill_checkbox(frame, field: Field, value: Any) -> None:
    state = value is True or norm(value) in ("yes", "true", "1", "checked", "on")
    loc = frame.locator(_css(field.key)).first
    if await loc.evaluate("e => e.getAttribute('role') === 'checkbox'"):
        if (await loc.get_attribute("aria-checked") == "true") != state:
            await _click(loc)
        return
    await _check(frame, _css(field.key), state)


async def _fill_checkbox_group(frame, field: Field, value: Any) -> None:
    wanted = value if isinstance(value, list) else [value]
    chosen = set()
    for item in wanted:
        option = match_option(item, field.options)
        if option is None:
            raise FillError(f"None of the choices matches '{item}'.")
        chosen.add(option)
    for index, option in enumerate(field.options):
        await _check(frame, _css(f"{field.key}#{index}"), option in chosen)


async def _fill_choice(frame, field: Field, value: Any) -> None:
    option = match_option(value, field.options)
    if option is None:
        raise FillError(f"None of the choices matches '{value}'.")
    if norm(field.value) == norm(option):
        return  # already chosen; clicking again would undo it on toggle buttons
    await _click(frame.locator(_css(f"{field.key}#{field.options.index(option)}")).first)


async def _visible_options(frame, control) -> tuple[Any, list[str]]:
    listbox = await control.get_attribute("aria-controls") or await control.get_attribute("aria-owns")
    candidates = []
    if listbox:
        candidates.append(frame.locator(f'[id="{listbox}"] [role=option]'))
    candidates.append(frame.locator("[role=listbox]:visible [role=option]"))
    candidates.append(frame.locator("[role=option]:visible"))
    candidates.append(frame.locator('[class*="menu"] [class*="option"]:visible'))
    for locator in candidates:
        try:
            texts = [" ".join(t.split()) for t in await locator.all_inner_texts()]
        except Exception:  # noqa: BLE001
            continue
        if any(texts):
            return locator, texts
    return None, []


async def _click_option(option) -> None:
    """Click a choice in an open list. Lists that move while the page scrolls get the click events directly."""
    try:
        await option.scroll_into_view_if_needed(timeout=2000)
        await option.click(timeout=2500)
    except Exception:  # noqa: BLE001
        await option.evaluate("""e => {
            for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                e.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
            }
        }""")


async def _fill_combobox(frame, field: Field, value: Any) -> str:
    control = frame.locator(_css(field.key)).first
    tag = await control.evaluate("e => e.tagName")
    typable = tag == "INPUT" and not await control.evaluate("e => e.readOnly")
    await control.click(timeout=5000)
    seen: list[str] = []
    # First type a few words to narrow the list (needed for search-as-you-type lists);
    # if nothing fits, clear the box and look through the full list.
    for typed in ([search_text(value), ""] if typable else [None]):
        if typed is not None:
            await control.fill("")
            if typed:
                await control.press_sequentially(typed, delay=20)
        deadline = asyncio.get_running_loop().time() + OPTION_WAIT / 2
        while True:
            locator, texts = await _visible_options(frame, control)
            seen = texts or seen
            option = match_option(value, texts) if texts else None
            if option is not None:
                await _click_option(locator.nth(texts.index(option)))
                return option
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.2)
    try:
        await control.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    if seen:
        shown = ", ".join(seen[:8])
        raise FillError(f"None of the choices matches '{value}'. Choices shown: {shown}.")
    raise FillError("The list of choices did not open.")


async def _fill_file(frame, field: Field, value: Any, allowed: list[Path]) -> None:
    path = Path(str(value)).resolve()
    if not any(path == root or root in path.parents for root in allowed):
        raise FillError("Only files from your profile folder or this application's drafts are attached.")
    if not path.is_file():
        raise FillError(f"The file {path.name} was not found.")
    await frame.locator(_css(field.key)).first.set_input_files(str(path), timeout=8000)


# --- checking the result -------------------------------------------------------------------------


def _same(field: Field, wanted: Any, now: Any) -> bool:
    if field.kind == "file":
        return Path(str(wanted)).name in (now or [])
    if field.kind == "checkbox":
        return bool(now) == (wanted is True or norm(wanted) in ("yes", "true", "1", "checked", "on"))
    if field.kind in ("multiselect", "checkbox_group"):
        wanted_list = wanted if isinstance(wanted, list) else [wanted]
        return sorted(norm(x) for x in (now or [])) == sorted(norm(match_option(w, field.options) or w) for w in wanted_list)
    if field.kind == "tel":
        a, b = re.sub(r"\D", "", str(wanted)), re.sub(r"\D", "", str(now))
        return bool(a) and (a.endswith(b[-10:]) or b.endswith(a[-10:])) and len(b) >= min(7, len(a))
    if field.kind == "date":
        try:
            return _as_date(str(wanted)) == str(now)
        except FillError:
            return False
    if field.kind in ("select", "radio", "choice", "combobox"):
        option = match_option(wanted, field.options) if field.options else None
        return norm(now) == norm(option or wanted) or (bool(norm(now)) and norm(now) == norm(match_option(wanted, [now]) or ""))
    return norm(now) == norm(wanted) and " ".join(str(now).split()) == " ".join(str(wanted).split())


# --- the whole fill ---------------------------------------------------------------------------------


async def fill(page, values: dict[str, Any], *, allowed_files: Optional[list[Path]] = None) -> list[FillResult]:
    """Fill the given fields (field key -> value) and check each one. Returns one result per key."""
    allowed = [Path(p).resolve() for p in (allowed_files or [])]
    reading = await read_page(page)
    results: dict[str, FillResult] = {}
    chosen: dict[str, str] = {}
    for key, value in values.items():
        field = reading.field(key)
        if field is None:
            results[key] = FillResult(key=key, status="missing", wanted=value,
                                      message="That field is no longer on the page.")
            continue
        base = {"key": key, "label": field.label, "kind": field.kind, "wanted": value}
        if field.rule:
            results[key] = FillResult(**base, status="refused", message=field.rule_reason)
            continue
        if field.disabled or field.readonly:
            results[key] = FillResult(**base, status="failed", message="The field cannot be changed.")
            continue
        frame = reading._frames.get(field.frame)
        try:
            if field.kind in ("text", "email", "tel", "url", "number", "date", "month", "time", "textarea"):
                await _fill_text(frame, field, value)
            elif field.kind in ("select", "multiselect"):
                await _fill_select(frame, field, value)
            elif field.kind == "radio":
                await _fill_radio(frame, field, value)
            elif field.kind == "checkbox":
                await _fill_checkbox(frame, field, value)
            elif field.kind == "checkbox_group":
                await _fill_checkbox_group(frame, field, value)
            elif field.kind == "choice":
                await _fill_choice(frame, field, value)
            elif field.kind == "combobox":
                chosen[key] = await _fill_combobox(frame, field, value)
            elif field.kind == "file":
                await _fill_file(frame, field, value, allowed)
            else:
                raise FillError(f"Fields of kind {field.kind} cannot be filled.")
        except FillError as exc:
            results[key] = FillResult(**base, status="failed", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - one field's trouble must not stop the rest
            first = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
            log.info("Filling %s (%s) failed: %s", field.label, field.kind, first)
            results[key] = FillResult(**base, status="failed", message=f"The page did not accept it ({first}).")
    await asyncio.sleep(0.15)  # let the page finish redrawing
    after = await read_page(page)
    for key, value in values.items():
        if key in results:
            continue
        before = reading.field(key)
        field = after.field(key)
        if field is None:
            results[key] = FillResult(key=key, label=before.label, kind=before.kind, status="mismatch", wanted=value,
                                      message="The field disappeared after it was filled.")
            continue
        if key in chosen:
            field.options = list(dict.fromkeys(field.options + [chosen[key]]))
        ok = _same(field, chosen.get(key, value), field.value)
        message = ""
        if not ok:
            message = "The page shows a different value."
            if field.maxlength and isinstance(value, str) and len(value) > field.maxlength:
                message = f"The field takes at most {field.maxlength} characters."
            if field.error:
                message += f" It says: {field.error}"
        results[key] = FillResult(key=key, label=field.label, kind=field.kind, status="ok" if ok else "mismatch",
                                  wanted=value, now=field.value, message=message)
    return [results[k] for k in values]


async def click(page, key: str, *, allow: tuple[str, ...] = ("next", "back", "add", "upload", "open_application")):
    """Click a button by key. Final-submit buttons, and buttons whose role is unclear, are refused."""
    reading = await read_page(page)
    button = reading.button(key)
    if button is None:
        raise FillError("That button is no longer on the page.")
    if button.role == "submit":
        raise FillError("Job App never submits an application. Review it and submit it yourself.")
    if button.role not in allow:
        raise FillError(f"It is not clear what '{button.text}' does, so Job App will not click it.")
    frame = reading._frames.get(button.frame)
    await frame.locator(f'[data-jobapp-button="{key}"]').first.click()
    return button
