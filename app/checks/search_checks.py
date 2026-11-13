"""The check command's tests for the job search: the real job feeds, the hidden browser, and an agent's web search."""

from __future__ import annotations

import time
from datetime import date

from ..commands import cmd
from ..core.models import Settings
from ..paths import Paths
from . import Result

# Public boards that have existed for years. A board that has since moved
# still proves the feed itself can be reached, so only network failures count.
FEEDS = [
    ("Greenhouse", "https://boards-api.greenhouse.io/v1/boards/stripe"),
    ("Lever", "https://api.lever.co/v0/postings/palantir?mode=json&limit=1"),
    ("Ashby", "https://api.ashbyhq.com/posting-api/job-board/openai"),
]


def next_summer(today: date | None = None) -> int:
    today = today or date.today()
    return today.year + 1 if today.month >= 8 else today.year


async def feeds_check(paths: Paths, settings: Settings) -> Result:
    from ..search.http import FetchError, SafeWeb
    from ..search.sources import simplify

    web = SafeWeb()
    reached, failed, details = [], [], {}
    for name, url in FEEDS:
        started = time.monotonic()
        try:
            response = await web.get(url, timeout=20)
            details[name] = {"status": response.status, "seconds": round(time.monotonic() - started, 1)}
            reached.append(name)
        except FetchError as exc:
            details[name] = {"error": str(exc), "kind": exc.kind, "status": exc.status}
            if exc.kind in ("not_found", "http") and exc.status:
                reached.append(name)  # the site answered; that board is just gone
            else:
                failed.append(name)
    year = next_summer()
    repo, label = simplify.repos_for(["internships"], [f"Summer {year}"])[0]
    listings = None
    try:
        found = await simplify.read_list(web, repo, label, [f"Summer {year}"])
        listings = len(found)
        reached.append("community list")
        details["list"] = {"repo": repo, "open_listings": listings}
    except FetchError as exc:
        details["list"] = {"repo": repo, "error": str(exc), "kind": exc.kind}
        failed.append(f"the Summer {year} community list")
    if failed:
        return Result("search_feeds", "fail", f"Could not reach: {', '.join(failed)}.",
                      fix="Check the internet connection, or whether a firewall or antivirus blocks Python. "
                          "The log file below has the errors.", details=details)
    list_text = f" The Summer {year} list has {listings:,} open listings." if listings is not None else ""
    return Result("search_feeds", "ok", f"Job feeds reachable: Greenhouse, Lever, Ashby and the community list.{list_text}",
                  details=details)


async def headless_check(settings: Settings) -> Result:
    from ..search.pages import HeadlessReader

    reader = HeadlessReader(settings.browser.channel)
    try:
        await reader._start()
        version = reader._browser.version if reader._browser else ""
        return Result("search_headless", "ok", f"Hidden browser for JavaScript job pages: starts ({version}).",
                      details={"version": version})
    except Exception as exc:  # noqa: BLE001
        return Result("search_headless", "warn",
                      "The hidden browser for JavaScript job pages could not start. Such pages are then judged "
                      "from what the search agent saw.", fix=f"Run '{cmd('setup')}' again.", details={"error": str(exc)[:500]})
    finally:
        await reader.close()


async def web_search_check(paths: Paths, settings: Settings) -> Result:
    """An agent runs one web search through the same runner the search agent uses."""
    from ..agents.runner import AgentError, ask_structured

    schema = {"type": "object", "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
              "required": ["title", "url"], "additionalProperties": False}
    try:
        reply = await ask_structured(
            paths, settings, name="check_web_search",
            instructions="This is a connection test. Use WebSearch once, then return the title and address of the "
                         'first result as {"title": "...", "url": "..."}.',
            prompt="Search the web for: Greenhouse job board API",
            schema=schema, model=settings.models.search, timeout=200, max_turns=4, max_budget_usd=0.25, fallback=False,
            extra={"tools": ["WebSearch"], "allowed_tools": ["WebSearch"]},
        )
    except AgentError as exc:
        return Result("search_web", "fail", f"An agent could not search the web ({exc.kind}).",
                      fix=exc.message + " Search still works without the web search agent: it can be "
                                        "switched off in the Search screen.",
                      details={"kind": exc.kind, "detail": exc.detail})
    details = {"data": reply.data, "tools_called": reply.tools_called, "turns": reply.turns,
               "cost_usd": reply.cost_usd}
    used = "WebSearch" in reply.tools_called
    url = str((reply.data or {}).get("url") or "")
    if used and url.startswith("http"):
        return Result("search_web", "ok", "An agent searched the web.", details=details)
    return Result("search_web", "fail",
                  "The agent ran but did not use web search." if not used else "The web search returned no address.",
                  fix="Search still works without the web search agent: switch it off in the Search screen. "
                      "The log file below has what the agent did.", details=details)
