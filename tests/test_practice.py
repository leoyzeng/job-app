"""Trying the form engine from the dashboard: through the real server, with Job App's browser window headless."""

from __future__ import annotations

import time


def wait_practice(running, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        practice = running.state()["practice"]
        if practice["status"] != "working":
            return practice
        time.sleep(0.1)
    raise AssertionError("the practice form did not finish")


def test_practice_needs_a_known_form(running):
    status, body, _ = running.call("POST", "/api/practice", {"style": "nonsense"})
    assert status == 422
    status, body, _ = running.call("POST", "/api/practice/check")
    assert status == 409 and body["message"] == "Open a practice form first."
    # The stand-in browsers used by most tests have no window to open it in.
    status, body, _ = running.call("POST", "/api/practice", {"style": "lever"})
    assert status == 409 and "Job App's browser window" in body["message"]


def test_practice_pages_are_served_without_the_cookie(running):
    status, body, _ = running.call("GET", "/practice/lever.html", signed_in=False)
    assert status == 200 and "Nothing typed here leaves this computer." in body


def test_practice_fills_from_the_profile_and_finds_your_changes(running_with_browser):
    running = running_with_browser
    running.ready_profile()
    (running.paths.profile / "resume.pdf").write_bytes(b"%PDF-1.4 sample")
    status, body, _ = running.call("POST", "/api/practice", {"style": "lever"})
    assert status == 202 and body["practice"]["status"] == "working"
    practice = wait_practice(running)
    assert practice["status"] == "ready", practice["message"]
    filled = {f["label"]: (f["value"], f["status"]) for f in practice["filled"]}
    assert filled == {
        "Resume/CV": ("resume.pdf", "ok"), "Full name": ("Sam Rivera", "ok"), "Email": ("sam.rivera@example.com", "ok"),
        "Phone": ("555-0142", "ok"), "Current location": ("Pittsburgh, PA", "ok"),
        "LinkedIn URL": ("https://linkedin.com/in/sam-rivera-example", "ok"),
        "GitHub URL": ("https://github.com/samrivera-example", "ok"),
        "Portfolio URL": ("https://samrivera.example.com", "ok"),
    }
    assert practice["message"].startswith("Filled 8 of 19 fields from your profile.")
    left = {f["label"]: f["why"] for f in practice["left"]}
    assert left["Are you at least 18 years of age?"].startswith("A sensitive question")
    assert left["I agree to the processing of my personal data as described in the privacy notice."].startswith("Agreements")
    assert left["Current company"] == "For the application agent (when you click Prepare on a listing) or for you."

    # Nothing changed yet.
    status, body, _ = running.call("POST", "/api/practice/check")
    assert status == 200 and body["practice"]["changes"] == [] and body["practice"]["message"] == "No changes found."

    # You type into the form in Job App's browser window.
    import asyncio

    service = running.app.app.state.practice
    asyncio.run_coroutine_threadsafe(_edit(service.page), service.loop).result(30)
    status, body, _ = running.call("POST", "/api/practice/check")
    changes = {(c["label"], c["change"], str(c["after"])) for c in body["practice"]["changes"]}
    assert changes == {("Current company", "filled", "Practice Corp"), ("Full name", "changed", "Samuel Rivera")}
    assert body["practice"]["message"] == "You changed 2 fields."


async def _edit(page):
    await page.fill("input[name=org]", "Practice Corp")
    await page.fill("input[name=name]", "Samuel Rivera")


def test_practice_without_a_confirmed_profile_fills_nothing(running_with_browser):
    running = running_with_browser
    running.call("POST", "/api/practice", {"style": "ashby"})
    practice = wait_practice(running)
    assert practice["status"] == "ready" and practice["filled"] == []
    assert practice["message"].startswith("Your profile is not confirmed yet")
    assert len(practice["left"]) == 12


def test_the_practice_dialog(browser, running_with_browser):
    running = running_with_browser
    running.ready_profile()
    context = browser.new_context(viewport={"width": 1120, "height": 900})
    errors = []
    page = context.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(f"{running.base}/?t={running.token}")
        page.wait_for_selector("#views .view-link")
        page.click("#btn-settings")
        page.click("#btn-practice")
        assert page.locator("#dlg-practice").evaluate("d => d.open")
        assert page.locator("#btn-practice-check").is_disabled()
        page.locator("label.choice-row", has=page.locator("input[value=greenhouse]")).click()
        page.click("#btn-practice-open")
        page.wait_for_selector("#practice-filled", timeout=60_000)
        filled = page.inner_text("#practice-filled")
        assert "First Name Sam" in filled and "Location (City) Pittsburgh, PA" in filled
        left = page.inner_text("#practice-left")
        assert "Are you legally authorized to work in the United States? (required)" in left
        assert "A sensitive question" in left
        assert not page.locator("#btn-practice-check").is_disabled()

        import asyncio

        service = running.app.app.state.practice

        async def edit():
            await service.page.fill("#first_name", "Samuel")

        asyncio.run_coroutine_threadsafe(edit(), service.loop).result(30)
        page.click("#btn-practice-check")
        page.wait_for_selector("#practice-changes")
        assert page.inner_text("#practice-changes") == "What you changed\nFirst Name\nSam → Samuel"
        assert errors == []
    finally:
        context.close()
