"""Reading one job posting from its link, whatever site it is on.

In order: a job system the app reads directly (Greenhouse, Lever, Ashby,
Workday); the page itself, using the JobPosting description many career sites
embed for search engines; and, for pages drawn by JavaScript, the page as a
headless browser shows it. Job boards whose terms forbid automated reading
(LinkedIn, Indeed, Glassdoor and similar) are not read at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from . import sources
from .http import FetchError, Web, check_public
from .models import Posting
from .text import clip, html_to_text, json_ld_postings, looks_closed, page_title, tidy

log = logging.getLogger("jobapp.search.pages")

# Boards whose terms restrict automated access (PLAN.md 7.4). Postings found
# there are matched to the employer's own page instead.
RESTRICTED = (
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com", "simplyhired.com",
    "joinhandshake.com", "handshake.com", "wellfound.com", "angel.co", "dice.com", "careerbuilder.com",
)
# Boards and aggregators: readable, but not the employer's own page.
BOARDS = RESTRICTED + (
    "simplify.jobs", "builtin.com", "builtinnyc.com", "builtinboston.com", "ycombinator.com", "workatastartup.com",
    "otta.com", "welcometothejungle.com", "levels.fyi", "jobright.ai", "intern-list.com", "internships.com",
    "lensa.com", "talent.com", "jooble.org", "adzuna.com", "jobgether.com", "remoterocketship.com",
)
# Applicant systems employers use for their own postings.
APPLICANT_SYSTEMS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "myworkdaysite.com", "smartrecruiters.com",
    "icims.com", "jobvite.com", "workable.com", "bamboohr.com", "recruitee.com", "breezy.hr", "jazzhr.com",
    "applytojob.com", "taleo.net", "successfactors.com", "successfactors.eu", "oraclecloud.com", "eightfold.ai",
    "phenompeople.com", "avature.net", "ultipro.com", "ukg.com", "paylocity.com", "adp.com", "dayforcehcm.com",
    "rippling.com", "pinpointhq.com", "teamtailor.com", "personio.de", "personio.com", "gem.com", "dover.com",
    "jobs.apple.com", "careers.google.com", "metacareers.com", "amazon.jobs", "careers.microsoft.com",
)
MIN_USEFUL_TEXT = 600


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def on(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_restricted(url: str) -> bool:
    return on(host_of(url), RESTRICTED)


def site_kind(url: str) -> str:
    """"system" (an applicant system), "board" (a job board), or "site" (anything else, often the employer)."""
    host = host_of(url)
    if on(host, APPLICANT_SYSTEMS):
        return "system"
    if on(host, BOARDS):
        return "board"
    return "site"


_EMBEDS = [
    re.compile(r"https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_app\?[^\"'<>\s]*for=[A-Za-z0-9_.-]+[^\"'<>\s]*"),
    re.compile(r"https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/[A-Za-z0-9_.-]+/jobs/\d+"),
    re.compile(r"https?://jobs(?:\.eu)?\.lever\.co/[A-Za-z0-9_.-]+/[0-9a-fA-F-]{20,40}"),
    re.compile(r"https?://jobs\.ashbyhq\.com/[A-Za-z0-9_.%-]+/[0-9a-fA-F-]{30,40}"),
]


def embedded_system_link(markup: str) -> Optional[str]:
    """A posting link on an applicant system that a company page embeds, if there is exactly one kind."""
    for pattern in _EMBEDS:
        found = set(m.replace("&amp;", "&") for m in pattern.findall(markup or ""))
        if len(found) == 1:
            return found.pop()
    return None


async def guard_route(route) -> None:
    """For a hidden browser: let a request through only if it goes to a public web address."""
    request = route.request
    target = request.url
    if not target.startswith(("http://", "https://")):
        await route.continue_()
        return
    try:
        if request.is_navigation_request():
            await asyncio.to_thread(check_public, target)
        else:
            host = (urlsplit(target).hostname or "").lower()
            if host == "localhost" or re.match(r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|\[)", host):
                raise FetchError("blocked", target)
    except FetchError:
        await route.abort()
        return
    await route.continue_()


class HeadlessReader:
    """Shows JavaScript-drawn pages in a hidden browser and returns their text. Started only when needed."""

    def __init__(self, channel: str = "chrome", limit: int = 30):
        self.channel = channel
        self.limit = limit
        self.used = 0
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()
        self.unavailable = ""

    async def _start(self):
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        for channel in (self.channel, "msedge", None):
            try:
                self._browser = await self._playwright.chromium.launch(channel=channel, headless=True)
                return
            except Exception as exc:  # noqa: BLE001 - try the next browser
                self.unavailable = str(exc).splitlines()[0][:200]
        raise RuntimeError(self.unavailable or "no browser")

    async def render(self, url: str) -> tuple[str, str]:
        """(final address, HTML). Raises FetchError."""
        if self.unavailable and self._browser is None and self._playwright is not None:
            raise FetchError("network", url, "No browser is available to show this page.")
        if self.used >= self.limit:
            raise FetchError("network", url, "Enough pages were opened in the hidden browser for one search.")
        async with self._lock:
            if self._browser is None:
                try:
                    await self._start()
                except Exception as exc:  # noqa: BLE001
                    raise FetchError("network", url, f"No browser is available to show this page ({exc}).") from exc
            self.used += 1
        context = await self._browser.new_context()

        try:
            await context.route("**/*", guard_route)
            page = await context.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            if response is not None and response.status in (404, 410):
                raise FetchError("not_found", url, f"The site answered {response.status}.", response.status)
            try:
                await page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:  # noqa: BLE001 - busy pages never go idle; what is there is enough
                pass
            return page.url, await page.content()
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FetchError("network", url, f"The page could not be shown ({str(exc).splitlines()[0][:160]}).") from exc
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None


def _from_page(url: str, markup: str, hint: Optional[Posting]) -> Posting:
    ld = json_ld_postings(markup)
    first = ld[0] if ld else {}
    text = first.get("description") or ""
    page_text = html_to_text(markup)
    if len(text) < MIN_USEFUL_TEXT and len(page_text) > len(text):
        text = page_text if not text else f"{text}\n\n{page_text}"
    title = first.get("title") or (hint.title if hint else "") or page_title(markup)
    company = first.get("company") or (hint.company if hint else "")
    notes = []
    if first.get("valid_through"):
        notes.append(f"The page says applications close {first['valid_through']}")
    return Posting(
        company=company,
        title=title,
        location=first.get("location") or (hint.location if hint else ""),
        url=url,
        source=hint.source if hint else "page",
        found_via=hint.found_via if hint else "",
        posted_at=first.get("posted_at") or (hint.posted_at if hint else ""),
        pay=first.get("pay") or (hint.pay if hint else ""),
        type=first.get("employment_type") or (hint.type if hint else ""),
        description=tidy(text),
        # A description the page declares about itself is complete even when short.
        full=len(first.get("description") or "") >= 200 or len(text) >= MIN_USEFUL_TEXT,
        open=not looks_closed(page_text),
        terms=hint.terms if hint else [],
        notes=(hint.notes if hint else []) + notes,
        hint=hint.hint if hint else {},
    )


def _merge(read: Posting, hint: Optional[Posting]) -> Posting:
    """Keep what the finder knew (where it was found, list notes) on the posting read from the source."""
    if hint is None:
        return read
    read.found_via = hint.found_via or read.found_via
    read.terms = hint.terms or read.terms
    read.notes = list(dict.fromkeys(hint.notes + read.notes))
    read.hint = hint.hint or read.hint
    read.posted_at = read.posted_at or hint.posted_at
    if hint.source in ("list", "web"):
        read.source = hint.source
    return read


async def read_posting(web: Web, url: str, *, hint: Optional[Posting] = None,
                       headless: Optional[HeadlessReader] = None, company: str = "") -> Posting:
    """The full posting behind a link. Raises FetchError ("not_found" means the posting is gone)."""
    if is_restricted(url):
        raise FetchError("blocked", url, "This job board does not allow automated reading. "
                                         "Look for the same job on the employer's own site.")
    found = sources.identify(url)
    if found and found.get("job_id"):
        posting = await sources.read_job(web, found, company or (hint.company if hint else ""))
        return _merge(posting, hint)
    try:
        response = await web.get(url)
    except FetchError as exc:
        # Some career sites turn away anything that is not a browser.
        if exc.kind not in ("http", "timeout") or headless is None or site_kind(url) == "board":
            raise
        final, rendered = await headless.render(url)
        posting = _from_page(final, rendered, hint)
        posting.description = clip(posting.description, 20_000)
        return posting
    markup = response.text()
    embedded = embedded_system_link(markup)
    if embedded and sources.identify(embedded) and sources.identify(embedded).get("job_id"):
        try:
            posting = await sources.read_job(web, sources.identify(embedded), company or (hint.company if hint else ""))
            posting.url = url  # keep the employer's own page as the link
            return _merge(posting, hint)
        except FetchError:
            pass
    posting = _from_page(response.url, markup, hint)
    if not posting.full and headless is not None and site_kind(url) != "board":
        try:
            final, rendered = await headless.render(url)
            shown = _from_page(final, rendered, hint)
            if len(shown.description) > len(posting.description):
                posting = shown
        except FetchError as exc:
            if exc.kind == "not_found":
                raise
            log.info("Hidden browser could not show %s: %s", url, exc)
    posting.description = clip(posting.description, 20_000)
    return posting
