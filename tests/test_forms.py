"""The form engine against the practice forms, in a real (headless) browser."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from app.forms import FillError, changes, click, fill, match_option, read_page
from app.forms.certain import certain_values
from app.forms.classify import classify_button, classify_field
from app.forms.model import Button, Field
from app.forms.watch import FormWatcher
from app.profile.demo import SAMPLE_FACTS
from app.profile.models import Facts

from conftest import REPO, free_port

PRACTICE = REPO / "ui" / "practice"


class Env:
    """A browser and a local server for the practice forms, in their own thread and event loop."""

    def __init__(self, tmp: Path):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.port = free_port()
        self.tmp = tmp
        self.resume = tmp / "resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4 practice resume")

    def run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def start(self):
        import uvicorn
        from playwright.async_api import async_playwright
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles

        app = Starlette(routes=[Mount("/practice", app=StaticFiles(directory=str(PRACTICE)))])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None,
                                                    access_log=False))
        self.server_task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()

    async def stop(self):
        await self.browser.close()
        await self.playwright.stop()
        self.server.should_exit = True
        await self.server_task

    async def open(self, name: str):
        page = await self.browser.new_page()
        await page.goto(f"http://127.0.0.1:{self.port}/practice/{name}.html")
        await page.wait_for_load_state("networkidle")
        return page


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    pytest.importorskip("playwright.async_api")
    e = Env(tmp_path_factory.mktemp("forms"))
    try:
        e.run(e.start())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"No browser for the form engine tests: {exc}")
    yield e
    e.run(e.stop())
    e.loop.call_soon_threadsafe(e.loop.stop)
    e.thread.join(timeout=5)


def keys(reading):
    return {f.label: f.key for f in reading.fields}


def by_label(reading, label):
    return next(f for f in reading.fields if f.label == label)


async def fill_by_label(page, values, allowed):
    reading = await read_page(page)
    k = keys(reading)
    results = await fill(page, {k[label]: value for label, value in values.items()}, allowed_files=allowed)
    return {r.label: r for r in results}


def values_for(env):
    from app.forms.practice import practice_values

    return {name: practice_values(name, str(env.resume)) for name in ("greenhouse", "lever", "ashby", "workday")}


# --- reading -------------------------------------------------------------------------------------


def test_reading_the_greenhouse_style_form(env):
    async def go():
        page = await env.open("greenhouse")
        try:
            return await read_page(page)
        finally:
            await page.close()

    reading = env.run(go())
    assert len(reading.fields) == 25 and reading.frames == ["main"]
    first = by_label(reading, "First Name")
    assert (first.kind, first.required, first.category, first.section) == ("text", True, "first_name", "Apply for this job")
    assert by_label(reading, "Preferred First Name").category == "preferred_name"
    resume = by_label(reading, "Resume/CV")
    assert (resume.kind, resume.required, resume.category, resume.visible) == ("file", True, "resume", True)
    auth = by_label(reading, "Are you legally authorized to work in the United States?")
    assert (auth.kind, auth.required, auth.category, auth.value, auth.options_known) == (
        "combobox", True, "sensitive:work_authorization", "", False)
    assert by_label(reading, "Gender").section == "Voluntary Self-Identification"
    month = by_label(reading, "End date month")
    assert month.kind == "select" and month.options[:2] == ["January", "February"] and month.value == ""
    consent = by_label(reading, "I acknowledge that I have read the candidate privacy notice.")
    assert (consent.kind, consent.rule, consent.required) == ("checkbox", "user", True)
    assert [(b.text, b.role) for b in reading.buttons][-1] == ("Submit application", "submit")
    assert {b.role for b in reading.buttons[:-1]} == {"upload"}


def test_reading_lever_ashby_workday_and_a_frame(env):
    async def go():
        out = {}
        for name in ("lever", "ashby", "workday", "embedded"):
            page = await env.open(name)
            out[name] = await read_page(page)
            await page.close()
        return out

    r = env.run(go())
    lever = r["lever"]
    assert [f.kind for f in lever.fields].count("radio") == 1
    age = by_label(lever, "Are you at least 18 years of age?")
    assert age.options == ["Yes", "No"] and age.required and age.category == "sensitive:age"
    assert by_label(lever, "Which teams interest you?").options == ["Autonomy", "Perception", "Controls", "Embedded"]
    assert by_label(lever, "Gender").options == ["Male", "Female", "Decline to self-identify"]  # label wraps the select
    assert by_label(lever, "Resume/CV").label == "Resume/CV"  # not "ATTACH RESUME/CV"

    ashby = r["ashby"]
    enrolled = by_label(ashby, "Are you currently enrolled in a degree program?")
    assert (enrolled.kind, enrolled.options, enrolled.required) == ("choice", ["Yes", "No"], True)
    assert by_label(ashby, "Which of these have you used?").kind == "checkbox_group"
    assert by_label(ashby, "Portfolio or personal website").kind == "url"     # inside a web component
    assert by_label(ashby, "Phone Number").category == "phone"                 # despite its random id

    workday = r["workday"]
    assert workday.progress == "current step 1 of 3"
    assert by_label(workday, "How Did You Hear About Us?").kind == "combobox"
    assert by_label(workday, "Have you previously been employed by Practice Robotics?").section == "My Information"
    assert [(b.text, b.role) for b in workday.buttons] == [("Back", "back"), ("Save and Continue", "next")]
    assert workday.buttons[0].disabled

    embedded = r["embedded"]
    assert len(embedded.fields) == 25 and embedded.frames[0].endswith("/practice/greenhouse.html#1")


def test_keys_stay_the_same_after_a_reload(env):
    async def go():
        page = await env.open("greenhouse")
        first = await read_page(page)
        await page.reload()
        await page.wait_for_load_state("networkidle")
        second = await read_page(page)
        await page.close()
        return first, second

    first, second = env.run(go())
    assert [f.key for f in first.fields] == [f.key for f in second.fields]
    assert len({f.key for f in first.fields}) == len(first.fields)


# --- filling ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["greenhouse", "lever", "ashby", "workday", "embedded"])
def test_every_kind_of_field_is_filled_and_checked(env, name):
    values = values_for(env)["greenhouse" if name == "embedded" else name]

    async def go():
        page = await env.open(name)
        try:
            results = await fill_by_label(page, values, [env.tmp])
            after = await read_page(page)
            return results, after
        finally:
            await page.close()

    results, after = env.run(go())
    problems = {label: (r.status, r.now, r.message) for label, r in results.items() if r.status != "ok"}
    assert problems == {}
    if name in ("greenhouse", "embedded"):
        assert by_label(after, "Location (City)").value == "Pittsburgh, Pennsylvania, United States"
        assert by_label(after, "Degree").value == "Master's Degree"
        assert by_label(after, "Resume/CV").value == ["resume.pdf"]
    if name == "ashby":
        assert by_label(after, "When can you start?").value == "2027-05-17"


def test_values_survive_the_page_redrawing_itself(env):
    """The practice pages redraw fields from their own copy of each value, as React forms do."""

    async def go():
        page = await env.open("greenhouse")
        try:
            await fill_by_label(page, {"First Name": "Sam"}, [])
            # A value set without the events a person's typing makes is lost at the next redraw...
            await page.evaluate("document.getElementById('last_name').value = 'Ghost'")
            await fill_by_label(page, {"Email": "sam@example.com"}, [])
            reading = await read_page(page)
            return by_label(reading, "First Name").value, by_label(reading, "Last Name").value
        finally:
            await page.close()

    first, last = env.run(go())
    assert first == "Sam"   # ...but what the engine filled stays
    assert last == ""       # proves the practice page really is that strict


def test_choices_that_do_not_exist_fail_cleanly(env):
    async def go():
        page = await env.open("greenhouse")
        try:
            results = await fill_by_label(page, {
                "Are you legally authorized to work in the United States?": "Maybe",
                "End date month": "Smarch",
                "First Name": "Sam",
            }, [])
            open_menus = await page.locator("[role=listbox]").count()
            return results, open_menus
        finally:
            await page.close()

    results, open_menus = env.run(go())
    combo = results["Are you legally authorized to work in the United States?"]
    assert combo.status == "failed" and "None of the choices matches 'Maybe'. Choices shown: Yes, No." == combo.message
    assert results["End date month"].status == "failed"
    assert results["First Name"].status == "ok"      # one bad field does not stop the rest
    assert open_menus == 0                            # the menu was closed again


def test_files_only_come_from_allowed_folders(env, tmp_path):
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4")

    async def go():
        page = await env.open("lever")
        try:
            return await fill_by_label(page, {"Resume/CV": str(outside)}, [env.tmp])
        finally:
            await page.close()

    result = env.run(go())["Resume/CV"]
    assert result.status == "failed" and "Only files from your profile folder" in result.message


SENSITIVE_PAGE = """<form>
<label for=pw>Password</label><input id=pw type=password>
<label for=cc>Card number</label><input id=cc autocomplete=cc-number>
<label for=ssn>Social Security Number</label><input id=ssn>
<label for=dl>Do you have a valid driver's license?</label><select id=dl><option value=''>Select</option><option>Yes</option><option>No</option></select>
<label for=sig>Signature (type your full name)</label><input id=sig>
<input type=checkbox id=terms><label for=terms>I certify that the information above is true.</label>
<div role=radiogroup aria-label="Preferred shift"><div role=radio aria-checked=false tabindex=0 onclick="this.parentElement.querySelectorAll('[role=radio]').forEach(r=>r.setAttribute('aria-checked', r===this))">Day</div><div role=radio aria-checked=false tabindex=0 onclick="this.parentElement.querySelectorAll('[role=radio]').forEach(r=>r.setAttribute('aria-checked', r===this))">Night</div></div>
<div role=checkbox aria-checked=false tabindex=0 aria-label="Open to relocation" style="width:16px;height:16px;border:1px solid" onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked')!=='true')"></div>
<div>Notes</div><div role=textbox contenteditable=true style="min-height:20px;border:1px solid"></div>
</form>"""


def test_some_fields_are_never_filled_and_custom_controls_work(env):
    async def go():
        page = await env.browser.new_page()
        try:
            await page.set_content(SENSITIVE_PAGE)
            reading = await read_page(page)
            k = keys(reading)
            results = await fill(page, {
                k["Password"]: "hunter2", k["Card number"]: "4111111111111111", k["Social Security Number"]: "123-45-6789",
                k["Signature (type your full name)"]: "Sam Rivera", k["I certify that the information above is true."]: True,
                k["Do you have a valid driver's license?"]: "Yes", k["Preferred shift"]: "Night",
                k["Open to relocation"]: True, k["Notes"]: "Hello",
            })
            typed = await page.evaluate("[pw.value, cc.value, ssn.value, sig.value, terms.checked]")
            return reading, {r.label: r for r in results}, typed
        finally:
            await page.close()

    reading, results, typed = env.run(go())
    for label in ("Password", "Card number", "Social Security Number"):
        assert results[label].status == "refused" and "never filled" in results[label].message
    for label in ("Signature (type your full name)", "I certify that the information above is true."):
        assert results[label].status == "refused" and "left for you" in results[label].message.lower() or \
            results[label].message == "Signatures are left for you."
    assert typed == ["", "", "", "", False]
    assert by_label(reading, "Do you have a valid driver's license?").rule == ""   # a yes/no question, not an ID number
    for label in ("Do you have a valid driver's license?", "Preferred shift", "Open to relocation", "Notes"):
        assert results[label].status == "ok", (label, results[label])


# --- what you changed ------------------------------------------------------------------------


def test_every_change_you_make_is_found(env):
    async def go():
        page = await env.open("greenhouse")
        try:
            await fill_by_label(page, {"First Name": "Sam", "Email": "sam@example.com", "Gender": "Male"}, [])
            before = await read_page(page)
            # You type, pick, tick and clear in the browser.
            await page.fill("#first_name", "Samuel")
            await page.fill("#email", "")
            await page.fill("#question_31002", "https://sam.example.com")
            await page.click("#question_31005")
            await page.locator("[role=option]", has_text="No").click()
            await page.click("#gender")
            for key in ("ArrowDown", "ArrowDown", "Enter"):
                await page.keyboard.press(key)
            await page.check("#privacy")
            await page.select_option("#end-month", label="May")
            after = await read_page(page)
            return changes(before, after)
        finally:
            await page.close()

    found = {(c.label, c.change, str(c.before), str(c.after)) for c in env.run(go())}
    assert found == {
        ("First Name", "changed", "Sam", "Samuel"),
        ("Email", "cleared", "sam@example.com", ""),
        ("Website", "filled", "", "https://sam.example.com"),
        ("Will you now or in the future require sponsorship for employment visa status (e.g. H-1B)?", "filled", "", "No"),
        ("Gender", "changed", "Male", "Decline To Self Identify"),
        ("I acknowledge that I have read the candidate privacy notice.", "filled", "False", "True"),
        ("End date month", "filled", "", "May"),
    }


def test_changes_on_lever_and_ashby_controls(env):
    async def go():
        out = {}
        page = await env.open("lever")
        before = await read_page(page)
        await page.check("input[value=Perception]")
        await page.check("input[name='cards[7d1e][field0]'][value=No]")
        out["lever"] = changes(before, await read_page(page))
        await page.close()
        page = await env.open("ashby")
        before = await read_page(page)
        await page.click("button.toggle >> text=Yes >> nth=1")
        await page.locator("jobapp-field input").fill("https://me.example.com")
        out["ashby"] = changes(before, await read_page(page))
        await page.close()
        return out

    out = env.run(go())
    assert [(c.label, c.after) for c in out["lever"]] == [
        ("Are you at least 18 years of age?", "No"), ("Which teams interest you?", ["Perception"])]
    assert [(c.label, c.after) for c in out["ashby"]] == [
        ("Will you require visa sponsorship to work in the US?", "Yes"), ("Portfolio or personal website", "https://me.example.com")]


# --- several steps, errors, and never submitting -----------------------------------------------------


def test_a_multi_step_form_up_to_but_never_past_review(env):
    values = values_for(env)["workday"]

    async def go():
        page = await env.open("workday")
        notes = {}
        try:
            partial = {k: v for k, v in values.items() if k not in ("Email Address", "Phone Number")}
            await fill_by_label(page, partial, [])
            reading = await read_page(page)
            await click(page, next(b.key for b in reading.buttons if b.role == "next"))
            stuck = await read_page(page)
            notes["errors"] = {f.label: f.error for f in stuck.fields if f.error}
            notes["progress_stuck"] = stuck.progress
            await fill_by_label(page, {"Email Address": "sam@example.com", "Phone Number": "4125550142"}, [])
            await click(page, next(b.key for b in (await read_page(page)).buttons if b.role == "next"))
            step2 = await read_page(page)
            notes["step2"] = [(f.label, f.kind) for f in step2.fields]
            notes["progress2"] = step2.progress
            results = await fill_by_label(page, {
                "Are you legally authorized to work in the country in which this job is located?": "Yes",
                "Month": "12", "Year": "2027", "Tell us about a project you are proud of.": "A rover.",
            }, [])
            notes["step2_results"] = {k: r.status for k, r in results.items()}
            await click(page, next(b.key for b in (await read_page(page)).buttons if b.role == "next"))
            notes["errors2"] = {f.label: f.error for f in (await read_page(page)).fields if f.error}
            await fill_by_label(page, {"Will you now or in the future require sponsorship for employment visa status?": "No"}, [])
            await click(page, next(b.key for b in (await read_page(page)).buttons if b.role == "next"))
            review = await read_page(page)
            notes["review_buttons"] = [(b.text, b.role) for b in review.buttons]
            submit = next(b for b in review.buttons if b.role == "submit")
            try:
                await click(page, submit.key)
                notes["refused"] = None
            except FillError as exc:
                notes["refused"] = str(exc)
            notes["sent"] = await page.locator("#practice-sent").count()
            return notes
        finally:
            await page.close()

    notes = env.run(go())
    assert notes["errors"] == {"Email Address": "The field is required and must have a value.",
                               "Phone Number": "The field is required and must have a value."}
    assert notes["progress_stuck"] == "current step 1 of 3" and notes["progress2"] == "current step 2 of 3"
    assert notes["step2"] == [
        ("Are you legally authorized to work in the country in which this job is located?", "combobox"),
        ("Will you now or in the future require sponsorship for employment visa status?", "combobox"),
        ("Month", "text"), ("Year", "text"), ("Tell us about a project you are proud of.", "textarea")]
    assert set(notes["step2_results"].values()) == {"ok"}
    assert notes["errors2"] == {"Will you now or in the future require sponsorship for employment visa status?":
                                "The field is required and must have a value."}
    assert notes["review_buttons"] == [("Back", "back"), ("Submit", "submit")]
    assert notes["refused"] == "Job App never submits an application. Review it and submit it yourself."
    assert notes["sent"] == 0


def test_unclear_buttons_are_not_clicked(env):
    async def go():
        page = await env.browser.new_page()
        try:
            await page.set_content("<form><label for=a>Name</label><input id=a><button type=button>Done</button>"
                                   "<button type=button>Apply</button></form>")
            reading = await read_page(page)
            roles = [(b.text, b.role) for b in reading.buttons]
            errors = []
            for b in reading.buttons:
                try:
                    await click(page, b.key)
                except FillError as exc:
                    errors.append(str(exc))
            return roles, errors
        finally:
            await page.close()

    roles, errors = env.run(go())
    assert roles == [("Done", "other"), ("Apply", "submit")]   # "Apply" on a form with fields might send it
    assert errors == ["It is not clear what 'Done' does, so Job App will not click it.",
                      "Job App never submits an application. Review it and submit it yourself."]


# --- certain fields from the profile, and the watcher -------------------------------------------------


def test_certain_fields_come_from_the_confirmed_profile(env):
    facts = Facts.model_validate(SAMPLE_FACTS)

    async def go():
        out = {}
        for name in ("greenhouse", "lever"):
            page = await env.open(name)
            reading = await read_page(page)
            planned, kept = certain_values(reading, facts, env.resume)
            results = await fill(page, {p.key: p.value for p in planned}, allowed_files=[env.tmp])
            out[name] = ({p.label: p.value for p in planned}, {r.label: r.status for r in results})
            await page.close()
        return out

    out = env.run(go())
    planned, results = out["greenhouse"]
    assert planned == {
        "First Name": "Sam", "Last Name": "Rivera", "Email": "sam.rivera@example.com", "Country": "United States",
        "Phone": "555-0142", "Location (City)": "Pittsburgh, PA", "Resume/CV": str(env.resume),
        "LinkedIn Profile": "https://linkedin.com/in/sam-rivera-example", "Website": "https://samrivera.example.com",
    }
    assert set(results.values()) == {"ok"}
    planned, results = out["lever"]
    assert planned == {
        "Resume/CV": str(env.resume), "Full name": "Sam Rivera", "Email": "sam.rivera@example.com", "Phone": "555-0142",
        "Current location": "Pittsburgh, PA", "LinkedIn URL": "https://linkedin.com/in/sam-rivera-example",
        "GitHub URL": "https://github.com/samrivera-example", "Portfolio URL": "https://samrivera.example.com",
    }
    assert set(results.values()) == {"ok"}


def test_fields_the_site_already_filled_are_kept(env):
    facts = Facts.model_validate(SAMPLE_FACTS)

    async def go():
        page = await env.open("greenhouse")
        try:
            await page.fill("#email", "parsed.from.resume@example.com")
            return certain_values(await read_page(page), facts, None)
        finally:
            await page.close()

    planned, kept = env.run(go())
    assert "Email" not in {p.label for p in planned} and "Resume/CV" not in {p.label for p in planned}
    assert kept == [{"key": kept[0]["key"], "label": "Email", "value": "parsed.from.resume@example.com"}]


def test_the_watcher_keeps_the_latest_reading_until_the_page_closes(env):
    async def go():
        page = await env.open("lever")
        watcher = FormWatcher(page, interval=0.1)
        watcher.start()
        await page.fill("input[name=name]", "Sam Typing")
        for _ in range(50):
            await asyncio.sleep(0.05)
            if watcher.latest and by_label(watcher.latest, "Full name").value == "Sam Typing":
                break
        seen = by_label(watcher.latest, "Full name").value
        await page.close()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if watcher.closed:
                break
        await watcher.stop()
        return seen, watcher.closed, watcher.readings

    seen, closed, readings = env.run(go())
    assert seen == "Sam Typing" and closed and readings >= 1


# --- no browser needed ------------------------------------------------------------------------


@pytest.mark.parametrize("wanted, options, expected", [
    ("Yes", ["Yes", "No"], "Yes"),
    ("yes", ["Yes, I am authorized", "No"], "Yes, I am authorized"),
    ("No", ["No", "Not applicable"], "No"),
    ("Master's", ["Bachelor's Degree", "Master's Degree", "Master of Business Administration (M.B.A.)"], "Master's Degree"),
    ("Pittsburgh, PA", ["Pittsburgh, Pennsylvania, United States", "Pittsburg, Kansas, United States"],
     "Pittsburgh, Pennsylvania, United States"),
    ("Asian", ["Asian (Not Hispanic or Latino)", "White (Not Hispanic or Latino)"], "Asian (Not Hispanic or Latino)"),
    ("Engineering", ["Electrical Engineering", "Mechanical Engineering"], None),   # two could fit: no guess
    ("Maybe", ["Yes", "No"], None),
    ("", ["Yes"], None),
])
def test_matching_a_value_to_the_choices(wanted, options, expected):
    assert match_option(wanted, options) == expected


@pytest.mark.parametrize("label, kind, category, rule", [
    ("Are you legally authorized to work in the United States?", "combobox", "sensitive:work_authorization", ""),
    ("Will you now or in the future require sponsorship?", "radio", "sensitive:sponsorship", ""),
    ("Desired salary", "text", "sensitive:salary", ""),
    ("Have you ever been convicted of a felony?", "radio", "sensitive:criminal", ""),
    ("Pronouns", "text", "sensitive:demographic", ""),
    ("Passport number", "text", "government_ID", "never"),
    ("Driver's license number", "text", "government_ID", "never"),
    ("Bank account number", "text", "payment", "never"),
    ("By checking this box I consent to the terms of use", "checkbox", "consent", "user"),
    ("Yes, I agree to receive text messages", "choice", "consent", "user"),
    ("Electronic signature", "text", "signature", "user"),
    ("Phone Device Type", "combobox", "other", ""),
    ("Mobile number", "tel", "phone", ""),
    ("Current company", "text", "other", ""),
])
def test_what_each_question_is(label, kind, category, rule):
    field = classify_field(Field(key="f", kind=kind, label=label))
    assert (field.category, field.rule) == (category, rule)


@pytest.mark.parametrize("text, has_fields, role", [
    ("Submit application", True, "submit"), ("Submit", True, "submit"), ("Send", True, "submit"),
    ("Save and Continue", True, "next"), ("Next", True, "next"), ("Review", True, "next"),
    ("Apply for this job", False, "open_application"), ("Apply", True, "submit"),
    ("Back", True, "back"), ("+ Add another", True, "add"), ("Attach", True, "upload"), ("Done", True, "other"),
])
def test_what_each_button_does(text, has_fields, role):
    assert classify_button(Button(key="b", text=text), has_fields).role == role
