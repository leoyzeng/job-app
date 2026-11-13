"""Opening postings: the browser choice, the dedicated window, and your own Chrome."""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.browser.manager import Browsers, OpenFailed, _web_link
from app.checks.testpage import LocalFormSite

from conftest import free_port


def run_async(make_coro, timeout=90):
    """Run a coroutine in its own thread and event loop (see run_ask in test_profile)."""
    box = {}

    def work():
        try:
            box["value"] = asyncio.run(make_coro())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "timed out"
    if "error" in box:
        raise box["error"]
    return box["value"]


# --- what is allowed to open ------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "file:///C:/Windows/win.ini", "--remote-debugging-port=9222",
    "chrome://settings", "", "   ",
])
def test_only_web_links_are_opened(bad):
    with pytest.raises(OpenFailed):
        _web_link(bad)


def test_web_links_pass_through_unchanged():
    assert _web_link(" https://jobs.example.com/a?b=1 ") == "https://jobs.example.com/a?b=1"
    assert _web_link("HTTP://example.com") == "HTTP://example.com"


# --- your own Chrome ------------------------------------------------------------------------


def test_own_chrome_hands_the_link_to_chrome(tmp_path):
    handed = []

    def fake_chrome(url):
        handed.append(url)
        return "chrome"

    browsers = Browsers(tmp_path, own_chrome=fake_chrome, chrome_finder=lambda: tmp_path / "chrome.exe")
    opened = run_async(lambda: browsers.open("https://jobs.example.com/1", mode="own_chrome"))
    assert handed == ["https://jobs.example.com/1"]
    assert opened.where == "own_chrome" and opened.message == "Opened in your Chrome."
    # Nothing about the dedicated window was started.
    assert browsers.info() == {"window_open": False, "window_browser": None, "chrome_found": True}


def test_own_chrome_falls_back_to_the_default_browser(tmp_path):
    browsers = Browsers(tmp_path, own_chrome=lambda url: "default", chrome_finder=lambda: None)
    opened = run_async(lambda: browsers.open("https://jobs.example.com/1", mode="own_chrome"))
    assert opened.message == "Opened in your default browser."
    assert browsers.info()["chrome_found"] is False


def test_a_bad_link_never_reaches_a_browser(tmp_path):
    handed = []
    browsers = Browsers(tmp_path, own_chrome=handed.append)
    with pytest.raises(OpenFailed):
        run_async(lambda: browsers.open("--load-extension=C:/evil", mode="own_chrome"))
    with pytest.raises(OpenFailed):
        run_async(lambda: browsers.open("file:///C:/secret.txt", mode="dedicated"))
    assert handed == [] and not browsers.window.is_open


def test_an_unknown_mode_is_refused(tmp_path):
    browsers = Browsers(tmp_path, own_chrome=lambda url: "chrome")
    with pytest.raises(OpenFailed):
        run_async(lambda: browsers.open("https://jobs.example.com/1", mode="somewhere"))


# --- Job App's browser window, for real (headless) -------------------------------------------


def _browser_or_skip(exc):
    pytest.skip(f"No browser available for the dedicated-window test: {exc}")


def test_dedicated_window_opens_reuses_and_comes_back(tmp_path):
    pytest.importorskip("playwright.async_api")
    dead = f"http://127.0.0.1:{free_port()}/nothing-here"

    async def scenario():
        browsers = Browsers(tmp_path / "browser_profile", headless=True)
        notes = {}
        try:
            async with LocalFormSite() as site:
                try:
                    first = await browsers.open(site.url, mode="dedicated", channel="chromium")
                except OpenFailed as exc:
                    return {"skip": str(exc)}
                context = browsers.window._context
                notes["first"] = (first.where, first.loaded, first.message)
                notes["pages_after_first"] = len(context.pages)
                notes["first_url"] = context.pages[0].url
                notes["info"] = browsers.info()

                second = await browsers.open(site.url + "?second", mode="dedicated", channel="chromium")
                notes["second_loaded"] = second.loaded
                notes["pages_after_second"] = len(context.pages)
                notes["same_window"] = browsers.window._context is context

                broken = await browsers.open(dead, mode="dedicated", channel="chromium")
                notes["broken"] = (broken.loaded, broken.message)

                # You close the window. The next posting opens a fresh one.
                await context.close()
                await asyncio.sleep(0.2)
                notes["open_after_close"] = browsers.window.is_open
                again = await browsers.open(site.url + "?again", mode="dedicated", channel="chromium")
                notes["again_loaded"] = again.loaded
                notes["new_window"] = browsers.window._context is not context
                notes["pages_in_new_window"] = len(browsers.window._context.pages)
        finally:
            await browsers.close()
        notes["closed_at_end"] = not browsers.window.is_open
        return notes

    notes = run_async(scenario)
    if "skip" in notes:
        _browser_or_skip(notes["skip"])
    assert notes["first"] == ("dedicated", True, "Opened in Job App's browser window.")
    # The empty tab a new window starts with is used for the first posting.
    assert notes["pages_after_first"] == 1 and notes["first_url"].startswith("http://127.0.0.1")
    assert notes["info"] == {"window_open": True, "window_browser": "the test browser", "chrome_found": notes["info"]["chrome_found"]}
    # Later postings get their own tabs in the same window.
    assert notes["second_loaded"] and notes["pages_after_second"] == 2 and notes["same_window"]
    loaded, message = notes["broken"]
    assert loaded is False and "did not load" in message
    assert notes["open_after_close"] is False
    assert notes["again_loaded"] and notes["new_window"] and notes["pages_in_new_window"] == 1
    assert notes["closed_at_end"]


def test_dedicated_window_reports_when_no_browser_starts(tmp_path, monkeypatch):
    pytest.importorskip("playwright.async_api")
    from app.browser import launch, manager

    async def no_browser(playwright, profile_root, channel="chrome", headless=False):
        raise launch.BrowserLaunchError({"chrome": "Executable doesn't exist", "msedge": "not found"})

    monkeypatch.setattr(manager, "launch_dedicated", no_browser)
    browsers = Browsers(tmp_path, headless=True)

    async def scenario():
        try:
            await browsers.open("https://jobs.example.com/1", mode="dedicated")
        finally:
            await browsers.close()

    with pytest.raises(OpenFailed) as caught:
        run_async(scenario)
    assert "could not be opened" in str(caught.value) and "Executable doesn't exist" in str(caught.value)
