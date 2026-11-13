"""The check command's tests for the form engine.

1. The engine fills every kind of field on the five practice forms in
   Job App's browser window (visible, unless --headless), checks every value
   took, and finds a change made afterwards.
2. The reader looks at real application forms on Greenhouse, Lever and
   Ashby, in a hidden browser. Read only: nothing is typed, clicked or sent.
   What it saw is saved in data/logs/form_samples/ so it can be reviewed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ..commands import cmd
from ..core.models import Settings
from ..paths import Paths
from . import Result


class PracticeSite:
    """Serves the practice forms on a spare local port, inside the running event loop."""

    def __init__(self, folder: Path):
        from .testpage import _free_port

        self.folder = folder
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}/practice"

    async def __aenter__(self):
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles

        app = Starlette(routes=[Mount("/practice", app=StaticFiles(directory=str(self.folder)))])
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None,
                                                     access_log=False, timeout_graceful_shutdown=1))
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("The practice forms could not be served.")
        return self

    async def __aexit__(self, *exc):
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            self._task.cancel()


async def practice_check(paths: Paths, settings: Settings, headless: bool) -> Result:
    from ..browser.launch import BrowserLaunchError, launch_dedicated
    from ..forms import changes, fill, read_page
    from ..forms.practice import PRACTICE_VALUES, practice_values

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return Result("forms_practice", "fail", "The form engine test could not run: Playwright is missing.",
                      fix=f"Run '{cmd('setup')}' again.", details={"error": str(exc)})
    scratch = paths.data / "check_files"
    scratch.mkdir(parents=True, exist_ok=True)
    resume = scratch / "practice-resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% A file used only by the check command's practice form test.\n")
    details: dict = {}
    problems: list[str] = []
    counted = 0
    try:
        async with PracticeSite(paths.ui / "practice") as site, async_playwright() as p:
            context, used = await launch_dedicated(p, paths.browser_profile, settings.browser.channel, headless)
            details["browser"] = used
            try:
                page = await context.new_page()
                for style in PRACTICE_VALUES:
                    started = time.monotonic()
                    await page.goto(f"{site.base}/{style}.html")
                    await page.wait_for_load_state("networkidle")
                    reading = await read_page(page)
                    keys = {f.label: f.key for f in reading.fields}
                    values = practice_values(style, str(resume))
                    missing = [label for label in values if label not in keys]
                    results = await fill(page, {keys[l]: v for l, v in values.items() if l in keys},
                                         allowed_files=[scratch])
                    bad = [f"{r.label}: {r.status} ({r.message})" for r in results if r.status != "ok"]
                    details[style] = {"fields": len(reading.fields), "filled": len(results), "seconds":
                                      round(time.monotonic() - started, 1), "problems": bad, "not_found": missing}
                    counted += len(results)
                    problems += [f"{style}: {b}" for b in bad] + [f"{style}: no field '{m}'" for m in missing]
                    if style == "greenhouse":
                        before = await read_page(page)
                        await page.fill("#first_name", "Samuel")
                        found = [(c.label, c.change) for c in changes(before, await read_page(page))]
                        details["change_found"] = found
                        if found != [("First Name", "changed")]:
                            problems.append(f"the change made afterwards was reported as {found}")
            finally:
                await context.close()
    except BrowserLaunchError as exc:
        return Result("forms_practice", "fail", "The form engine test could not open Job App's browser window.",
                      fix="See the browser test above.", details={"attempts": exc.attempts})
    except Exception as exc:  # noqa: BLE001
        return Result("forms_practice", "fail", f"The form engine test stopped: {str(exc).splitlines()[0][:200]}",
                      fix="The log file below has the details.", details={**details, "error": str(exc)[:2000]})
    finally:
        try:
            resume.unlink()
        except OSError:
            pass
    if problems:
        return Result("forms_practice", "fail", f"The form engine had trouble with {len(problems)} field(s) on the practice forms.",
                      fix="The log file below lists them.", details={**details, "problems": problems})
    return Result("forms_practice", "ok",
                  f"Form engine: filled and checked {counted} fields of every kind on 5 practice forms, and found a change made afterwards.",
                  details=details)


# --- real forms, read only -------------------------------------------------------------------------

DEFAULT_BOARDS = [("greenhouse", "stripe"), ("lever", "palantir"), ("ashby", "openai")]


async def _application_urls(paths: Paths) -> list[tuple[str, str, str]]:
    """(system, company board, application form address) for one posting on each system."""
    from ..search.boards import BoardStore
    from ..search.http import FetchError, SafeWeb
    from ..search.models import Board
    from ..search.sources import read_board

    store = BoardStore(paths.data / "company_boards.json")
    chosen = {}
    for board in store.followed():
        if board.ats in ("greenhouse", "lever", "ashby") and board.ats not in chosen:
            chosen[board.ats] = board
    for ats, token in DEFAULT_BOARDS:
        chosen.setdefault(ats, Board(ats=ats, token=token))
    web = SafeWeb()
    out = []
    for ats in ("greenhouse", "lever", "ashby"):
        board = chosen[ats]
        try:
            postings = await read_board(web, board, [])
        except FetchError:
            continue
        if not postings:
            continue
        # An internship posting if there is one, since that is the kind you will apply to.
        posting = next((p for p in postings if "intern" in p.title.lower()), postings[0])
        if ats == "greenhouse":
            url = f"https://job-boards.greenhouse.io/{board.token}/jobs/{posting.external_id}"
        elif ats == "lever":
            url = posting.apply_url or posting.url.rstrip("/") + "/apply"
        else:
            url = posting.apply_url or posting.url.rstrip("/") + "/application"
        out.append((ats, board.token, url))
    return out


async def real_forms_check(paths: Paths, settings: Settings) -> Result:
    from ..forms import describe, read_page
    from ..search.pages import guard_route

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return Result("forms_real", "fail", "The real-form test could not run: Playwright is missing.",
                      fix=f"Run '{cmd('setup')}' again.", details={"error": str(exc)})
    try:
        targets = await _application_urls(paths)
    except Exception as exc:  # noqa: BLE001
        targets = []
        error = str(exc)
    else:
        error = ""
    if not targets:
        return Result("forms_real", "warn", "Could not find a real application form to read.",
                      fix="This needs the job feeds above to work.", details={"error": error})
    folder = paths.logs / "form_samples"
    folder.mkdir(parents=True, exist_ok=True)
    summary, details = [], {}
    async with async_playwright() as p:
        browser = None
        for channel in (settings.browser.channel, "msedge", None):
            try:
                browser = await p.chromium.launch(channel=channel, headless=True)
                break
            except Exception:  # noqa: BLE001
                continue
        if browser is None:
            return Result("forms_real", "warn", "No hidden browser could start to read real forms.")
        try:
            for ats, token, url in targets:
                context = await browser.new_context()
                await context.route("**/*", guard_route)
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(1.5)
                    reading = await read_page(page)
                    described = describe(reading)
                    html = await page.evaluate(
                        "() => { const c = document.documentElement.cloneNode(true);"
                        " c.querySelectorAll('script, style, noscript, svg').forEach(e => e.remove());"
                        " return '<!doctype html>' + c.outerHTML; }")
                    (folder / f"{ats}.json").write_text(json.dumps({"url": url, **described}, indent=1), encoding="utf-8")
                    (folder / f"{ats}.html").write_text(html, encoding="utf-8")
                    for frame in page.frames[1:]:
                        if "greenhouse" in frame.url or "lever" in frame.url or "ashby" in frame.url:
                            try:
                                inner = await frame.evaluate("() => document.documentElement.outerHTML")
                                (folder / f"{ats}-frame.html").write_text(inner, encoding="utf-8")
                            except Exception:  # noqa: BLE001
                                pass
                    fields = [f for f in reading.fields if f.visible or f.kind == "file"]
                    unlabelled = [f for f in fields if not f.label]
                    kinds: dict[str, int] = {}
                    for f in fields:
                        kinds[f.kind] = kinds.get(f.kind, 0) + 1
                    details[ats] = {"url": url, "board": token, "fields": len(fields), "unlabelled": len(unlabelled),
                                    "kinds": kinds, "submit_found": any(b.role == "submit" for b in reading.buttons)}
                    note = f" ({len(unlabelled)} without a label)" if unlabelled else ""
                    summary.append(f"{ats.title()} {len(fields)} fields{note}")
                except Exception as exc:  # noqa: BLE001
                    details[ats] = {"url": url, "error": str(exc).splitlines()[0][:300]}
                    summary.append(f"{ats.title()} could not be read")
                finally:
                    await context.close()
        finally:
            await browser.close()
    read = [d for d in details.values() if d.get("fields")]
    status = "ok" if read and all(d.get("fields") and not d.get("unlabelled") for d in details.values()) else "warn"
    return Result("forms_real", status,
                  "Form reader on real application forms (read only, nothing typed or sent): " + "; ".join(summary) + ".",
                  fix="" if status == "ok" else "Not a problem for you to fix: the saved samples show Claude what to improve.",
                  details={**details, "samples": str(folder)})
