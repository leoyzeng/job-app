"""One run of "Search for new jobs" (PLAN.md 7.2).

1. Plan: turn the confirmed requirements into queries and title filters (the
   planner agent, cached until the requirements change).
2. Boards: find the job boards of watchlist and suggested companies.
3. Collect, all at once: company boards, community lists, web search agents.
4. Screen: drop broken links, repeats, titles that cannot fit, jobs already
   in your tracker in any status, and postings judged before.
5. Judge, most promising first: read each posting in full, check it is open
   and plausible, and have the fit evaluator judge it against every hard
   requirement. Stops at the listing cap, the evaluation cap or the budget.
6. List the ones that pass, and write data/search_runs/<time>.json.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ..commands import LOGS_HINT
from ..agents.runner import AgentError
from ..core.dedupe import BadUrl, company_key, job_id_for, make_dedupe_keys, normalize_url
from ..core.events import EventBus
from ..core.jsonfile import JsonCorrupt, read_json, write_json_atomic
from ..core.models import Details, HardResult, Job, Match, Settings, Source, Status, now_iso
from ..core.ranking import spread_queue
from ..core.store import DuplicateJob, TrackerStore
from ..paths import Paths
from ..profile.models import SearchReading
from . import filters, pages
from .agents import (AskFn, Verdict, WebAgentFn, evaluate, make_plan, match_fields, reading_fingerprint, simple_plan,
                     web_search)
from .boards import BoardStore, look_up_many, same_company
from .http import FetchError, Web
from .models import PlanFile, Posting, SearchPlan, SeenEntry, SeenFile
from .sources import NAMES, read_board, simplify
from .text import clip, scam_signs

log = logging.getLogger("jobapp.search")

PLAN_MAX_AGE_DAYS = 14
BATCH = 4
AGENT_PAGE_READS = 60
FILTER_EXAMPLES = 6
OUTCOME_WORDS = {
    "listed": "Listed", "rejected": "Did not meet a hard requirement", "closed": "Posting closed",
    "unreadable": "Could not be read", "not_judged": "Could not be judged", "suspicious": "Looked like a scam",
}


class Stopped(Exception):
    pass


def search_dir(paths: Paths) -> Path:
    return paths.data / "search_runs"


# --- postings judged before -------------------------------------------------------------------


class SeenStore:
    """data/search_seen.json: postings already judged, so they are not paid for twice."""

    KEEP_DAYS = {"rejected": 180, "suspicious": 180, "closed": 60, "unreadable": 7}

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = SeenFile.model_validate(read_json(path))
        except FileNotFoundError:
            self.data = SeenFile()
        except (JsonCorrupt, ValidationError) as exc:
            log.error("search_seen.json could not be read, starting a new one: %s", exc)
            self.data = SeenFile()

    def lookup(self, keys: list[str], fingerprint: str) -> Optional[SeenEntry]:
        now = datetime.now().astimezone()
        for key in keys:
            entry = self.data.entries.get(key)
            if entry is None:
                continue
            if entry.outcome in ("rejected", "suspicious") and entry.plan != fingerprint:
                continue  # the requirements changed: judge it again
            try:
                age = now - datetime.fromisoformat(entry.at)
            except ValueError:
                continue
            if age <= timedelta(days=self.KEEP_DAYS.get(entry.outcome, 30)):
                return entry
        return None

    def record(self, keys: list[str], outcome: str, reason: str, fingerprint: str, label: str) -> None:
        entry = SeenEntry(outcome=outcome, reason=clip(reason, 300), at=now_iso(), plan=fingerprint, label=label[:200])
        for key in keys:
            self.data.entries[key] = entry

    def save(self) -> None:
        # Old entries are dropped so the file does not grow for ever.
        now = datetime.now().astimezone()
        keep = {}
        for key, entry in self.data.entries.items():
            try:
                if now - datetime.fromisoformat(entry.at) <= timedelta(days=self.KEEP_DAYS.get(entry.outcome, 30)):
                    keep[key] = entry
            except ValueError:
                continue
        self.data.entries = keep
        write_json_atomic(self.path, self.data.model_dump(mode="json"), backup=False)


# --- the run ----------------------------------------------------------------------------------


@dataclass
class SearchContext:
    paths: Paths
    tracker: TrackerStore
    bus: EventBus
    web: Web
    ask: AskFn
    web_agent: Optional[WebAgentFn]
    headless: Optional[pages.HeadlessReader]
    settings: Settings
    reading: SearchReading
    profile: dict
    resume: Optional[dict] = None       # skills, positions, projects: only when resume match is on


def legit_note(posting: Posting) -> tuple[str, str]:
    """(kind, note): "system", "employer", "board" or "unverified", with a note for the listing when needed."""
    kind = pages.site_kind(posting.url)
    if kind == "system":
        return "system", ""
    if kind == "board":
        return "board", "Only found on a job board; the employer's own page was not found."
    host = pages.host_of(posting.url)
    words = [w for w in company_key(posting.company).split() if len(w) >= 3]
    squashed = host.replace("-", "").replace(".", "")
    if words and (any(w in host for w in words) or "".join(words) in squashed):
        return "employer", ""
    return "unverified", f"Unverified employer: the posting is on {host}, which does not obviously belong to {posting.company}."


class SearchRun:
    def __init__(self, ctx: SearchContext, run_id: str):
        self.ctx = ctx
        self.id = run_id
        self.status = "running"
        self.stage = "plan"
        self.message = "Planning the search"
        self.started_at = now_iso()
        self.finished_at = ""
        self.counts: Counter = Counter()
        self.filtered: Counter = Counter()
        self.filtered_examples: dict[str, list[str]] = {}
        self.sources: list[dict] = []
        self.outcomes: list[dict] = []
        self.agents: list[dict] = []
        self.notes: list[str] = []
        self.cost = 0.0
        self.plan: Optional[SearchPlan] = None
        self.plan_reused = False
        self.stop_requested = False
        self._last_publish = 0.0
        self._read_cache: dict[str, Posting] = {}
        self._agent_reads = 0
        self.fingerprint = reading_fingerprint(ctx.reading)
        self.boards = BoardStore(ctx.paths.data / "company_boards.json")
        self.seen = SeenStore(ctx.paths.data / "search_seen.json")

    # --- what the dashboard sees -------------------------------------------------------

    def view(self) -> dict:
        return {
            "id": self.id, "status": self.status, "stage": self.stage, "message": self.message,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "counts": dict(self.counts), "cost_usd": round(self.cost, 4),
        }

    def report(self) -> dict:
        plan = self.plan.model_dump(mode="json") if self.plan else None
        return {
            **self.view(),
            "plan": plan, "plan_reused": self.plan_reused,
            "sources": self.sources,
            "filtered": [{"reason": r, "count": n, "examples": self.filtered_examples.get(r, [])}
                         for r, n in self.filtered.most_common()],
            "outcomes": self.outcomes,
            "agents": self.agents,
            "notes": self.notes,
        }

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_publish > 0.4:
            self._last_publish = now
            self.ctx.bus.publish("search_changed")

    def _progress(self, stage: str, message: str) -> None:
        changed = stage != self.stage
        self.stage, self.message = stage, message
        self._publish(force=changed)

    def _check_stop(self) -> None:
        if self.stop_requested:
            raise Stopped()

    def _agent(self, name: str, reply, ok: bool = True, error: str = "", seconds: float = 0.0) -> None:
        cost = float(getattr(reply, "cost_usd", None) or 0.0) if reply is not None else 0.0
        self.cost += cost
        self.agents.append({"agent": name, "ok": ok, "cost_usd": round(cost, 4), "seconds": round(seconds, 1),
                            "turns": getattr(reply, "turns", None), "error": error})

    def _filtered(self, reason: str, posting: Posting) -> None:
        self.filtered[reason] += 1
        examples = self.filtered_examples.setdefault(reason, [])
        if len(examples) < FILTER_EXAMPLES:
            examples.append(posting.label())

    def _outcome(self, posting: Posting, outcome: str, reason: str = "", job_id: str = "") -> None:
        self.counts[outcome] += 1
        self.outcomes.append({
            "outcome": outcome, "label": posting.label(), "company": posting.company, "title": posting.title,
            "url": posting.url, "source": posting.found_via or posting.source, "reason": reason, "job_id": job_id,
        })

    # --- 1. plan -------------------------------------------------------------------------

    async def _make_plan(self) -> SearchPlan:
        path = self.ctx.paths.data / "search_plan.json"
        try:
            saved = PlanFile.model_validate(read_json(path))
        except (FileNotFoundError, JsonCorrupt, ValidationError):
            saved = None
        if saved and saved.fingerprint == self.fingerprint:
            try:
                age = datetime.now().astimezone() - datetime.fromisoformat(saved.made_at)
            except ValueError:
                age = timedelta(days=PLAN_MAX_AGE_DAYS + 1)
            if age <= timedelta(days=PLAN_MAX_AGE_DAYS):
                self.plan_reused = True
                return saved.plan
        started = time.monotonic()
        try:
            plan, reply = await make_plan(self.ctx.ask, self.ctx.paths, self.ctx.reading, self.ctx.profile,
                                          self.ctx.settings.search.watchlist)
        except AgentError as exc:
            self._agent("search_planner", None, ok=False, error=exc.kind, seconds=time.monotonic() - started)
            if exc.kind in ("not_signed_in", "limit"):
                raise
            self.notes.append("The planner could not be reached, so a simple plan made from your requirements was used.")
            return simple_plan(self.ctx.reading)
        self._agent("search_planner", reply, seconds=time.monotonic() - started)
        write_json_atomic(path, PlanFile(fingerprint=self.fingerprint, made_at=now_iso(), plan=plan)
                          .model_dump(mode="json"), backup=False)
        return plan

    # --- 3. collect -----------------------------------------------------------------------

    async def _read_boards(self, plan: SearchPlan) -> list[Posting]:
        boards = self.boards.followed()[:150]
        if not boards:
            self.sources.append({"name": "Company job boards", "ok": True, "count": 0, "detail": "No boards to read yet."})
            return []
        words = plan.kind_words[:2] or plan.role_words[:2]
        done = 0

        async def one(board):
            nonlocal done
            try:
                found = await read_board(self.ctx.web, board, words)
                self.boards.note_read(board.key, len(found))
                return board, found, ""
            except FetchError as exc:
                self.boards.note_read(board.key, None, str(exc))
                return board, [], str(exc)
            finally:
                done += 1
                self.message = f"Reading company job boards ({done} of {len(boards)})"
                self._publish()

        results = await asyncio.gather(*(one(b) for b in boards))
        postings, failed = [], []
        for board, found, error in results:
            for posting in found:
                if board.company and posting.company in (board.token, ""):
                    posting.company = board.company
            postings.extend(found)
            if error:
                failed.append(f"{board.company or board.token} ({NAMES[board.ats]}): {error}")
        self.sources.append({
            "name": "Company job boards", "ok": len(failed) < len(boards), "count": len(postings),
            "detail": f"{len(boards) - len(failed)} of {len(boards)} boards read.",
            "errors": failed[:20],
        })
        return postings

    async def _read_lists(self, plan: SearchPlan) -> list[Posting]:
        out = []
        for repo, label in simplify.repos_for(plan.lists, plan.terms):
            try:
                found = await simplify.read_list(self.ctx.web, repo, label, plan.terms)
                self.sources.append({"name": label, "ok": True, "count": len(found)})
                out.extend(found)
            except FetchError as exc:
                self.sources.append({"name": label, "ok": False, "count": 0, "detail": str(exc)})
        return out

    async def _read_for_agent(self, url: str) -> str:
        url = (url or "").strip()
        if self._agent_reads >= AGENT_PAGE_READS:
            return "Enough pages have been read for this search. Return what you have."
        self._agent_reads += 1
        if pages.is_restricted(url):
            return ("This job board does not allow automated reading. Search for the same job title and company "
                    "to find the employer's own posting.")
        try:
            posting = await pages.read_posting(self.ctx.web, url, headless=self.ctx.headless)
        except FetchError as exc:
            if exc.kind == "not_found":
                return "This posting no longer exists (the site answered that it was not found)."
            return f"Could not read this page: {exc}"
        try:
            self._read_cache[normalize_url(url)] = posting
            self._read_cache[normalize_url(posting.url)] = posting
        except BadUrl:
            pass
        kind = pages.site_kind(posting.url)
        where = {"system": "an applicant system (the employer's own application site)",
                 "board": "a job board, not the employer's own page",
                 "site": "a company or other website"}[kind]
        lines = [
            f"Title: {posting.title}", f"Company: {posting.company}", f"Location: {posting.location}",
            f"Posted: {posting.posted_at}", f"Pay: {posting.pay}", f"Address: {posting.url}", f"This page is on {where}.",
            "Status: CLOSED, no longer accepting applications" if posting.open is False else "Status: open as far as the page shows",
            "", clip(posting.description, 4500) or "(The page had no readable text.)",
        ]
        return "\n".join(lines)

    async def _web(self, plan: SearchPlan) -> list[Posting]:
        queries = plan.queries or []
        if not queries:
            self.sources.append({"name": "Web search", "ok": True, "count": 0, "detail": "The plan had no web queries."})
            return []
        groups = [queries[: (len(queries) + 1) // 2], queries[(len(queries) + 1) // 2:]] if len(queries) >= 4 else [queries]
        per_agent = max(6, min(15, self.ctx.settings.search.max_new_per_search // len(groups) + 4))

        async def one(index: int, group: list[str]):
            started = time.monotonic()
            try:
                found, reply = await web_search(self.ctx.web_agent, self.ctx.paths, self.ctx.reading, group,
                                                per_agent, self._read_for_agent)
            except AgentError as exc:
                self._agent(f"search_agent_{index}", None, ok=False, error=exc.kind, seconds=time.monotonic() - started)
                if exc.kind in ("not_signed_in", "limit"):
                    raise
                return [], exc.message
            self._agent(f"search_agent_{index}", reply, seconds=time.monotonic() - started)
            return found, (reply.data or {}).get("tried", "") if isinstance(reply.data, dict) else ""

        results = await asyncio.gather(*(one(i + 1, g) for i, g in enumerate(groups)))
        postings, tried, errors = [], [], []
        for found, note in results:
            if not found and note and not tried:
                errors.append(note)
            if note:
                tried.append(note)
            for item in found:
                try:
                    cached = self._read_cache.get(normalize_url(item["url"]))
                except BadUrl:
                    continue
                found_on = item.get("found_on") or ""
                hint = Posting(
                    company=item["company"], title=item["title"], location=item.get("location", ""), url=item["url"],
                    source="web", found_via=f"Web search{', found on ' + found_on if found_on else ''}",
                    posted_at=item.get("posted_at", "")[:10], pay=item.get("pay", ""), type=item.get("type", ""),
                    description=item.get("summary", ""), hint=item,
                )
                if cached is not None:
                    cached = cached.model_copy(deep=True)
                    cached.source, cached.found_via, cached.hint = "web", hint.found_via, item
                    cached.company = cached.company or hint.company
                    cached.title = cached.title or hint.title
                    postings.append(cached)
                else:
                    postings.append(hint)
                self.boards.learn_from_url(item["url"], item["company"])
        self.sources.append({"name": "Web search", "ok": bool(postings) or not errors, "count": len(postings),
                             "detail": " ".join(tried)[:600]})
        return postings

    # --- 4. screen -------------------------------------------------------------------------

    def _screen(self, plan: SearchPlan, candidates: list[Posting]) -> list[tuple[Posting, list[str]]]:
        in_run: set[str] = set()
        pool = []
        for posting in candidates:
            self.counts["found"] += 1
            try:
                keys = make_dedupe_keys(posting.url, posting.company, posting.title, posting.location)
            except BadUrl:
                self._filtered("link is not a web address", posting)
                continue
            if in_run.intersection(keys):
                self.counts["repeat"] += 1
                continue
            in_run.update(keys)
            reason = filters.problem(plan, posting)
            if reason:
                self._filtered(reason, posting)
                continue
            if self.ctx.tracker.find_duplicate(keys) is not None:
                self.counts["already_listed"] += 1
                continue
            if self.seen.lookup(keys, self.fingerprint) is not None:
                self.counts["judged_before"] += 1
                continue
            pool.append((posting, keys))
        self.counts["filtered"] = sum(self.filtered.values())
        self.counts["to_judge"] = len(pool)
        return pool

    # --- 5. judge -------------------------------------------------------------------------

    async def _read(self, posting: Posting) -> tuple[Optional[Posting], str, str]:
        """(posting read in full, outcome if it cannot go on, reason)."""
        if posting.full and posting.source in ("greenhouse", "lever", "ashby"):
            return posting, "", ""
        try:
            cached = self._read_cache.get(normalize_url(posting.url))
        except BadUrl:
            cached = None
        if cached is not None and cached.full:
            read = cached.model_copy(deep=True)
            read.found_via, read.source, read.hint = posting.found_via, posting.source, posting.hint
            read.notes = list(dict.fromkeys(posting.notes + read.notes))
            read.terms = posting.terms or read.terms
        else:
            try:
                read = await pages.read_posting(self.ctx.web, posting.url, hint=posting, headless=self.ctx.headless,
                                                company=posting.company)
            except FetchError as exc:
                if exc.kind == "not_found":
                    return None, "closed", "The posting's page no longer exists."
                return None, "unreadable", str(exc)
        read.company = read.company or posting.company
        read.title = read.title or posting.title
        read.location = read.location or posting.location
        if read.open is False:
            return None, "closed", "The page says it no longer accepts applications."
        if not read.description.strip():
            return None, "unreadable", "The page had no readable text."
        self.boards.learn_from_url(read.url, read.company)
        return read, "", ""

    def _listed_companies(self) -> set[str]:
        return {company_key(job.company) for job in self.ctx.tracker.all_jobs() if job.status == Status.NEW}

    def _list(self, posting: Posting, verdict: Verdict, legit: tuple[str, str], signs: list[str]) -> Optional[str]:
        team = next((n[6:] for n in posting.notes if n.startswith("Team: ")), "")
        notes = [n for n in posting.notes if not n.startswith("Team: ")]
        if legit[1]:
            notes.append(legit[1])
        if signs:
            notes.append("Check this posting: it " + " and ".join(signs) + ".")
        unknown = [h["requirement"] for h in verdict.unknown]
        job = Job(
            id=job_id_for(posting.url),
            company=posting.company,
            title=posting.title,
            location=posting.location,
            url=posting.url,
            apply_url=posting.apply_url or posting.url,
            source=Source(kind=posting.source, found_via=posting.found_via or "search", search_run=self.id,
                          legit=legit[0], board=posting.board),
            posted_at=posting.posted_at or None,
            description_summary=verdict.summary or clip(posting.description, 500),
            details=Details(pay=posting.pay, type=posting.type, remote=posting.remote, team=team),
            match=Match(
                hard=[HardResult(**h) for h in verdict.hard],
                **match_fields(verdict),
                concerns=list(dict.fromkeys(verdict.concerns + notes)),
                unverified=unknown,
                checked_at=now_iso(),
            ),
            dedupe_keys=make_dedupe_keys(posting.url, posting.company, posting.title, posting.location),
        )
        try:
            job = self.ctx.tracker.add(job)
        except DuplicateJob:
            self.counts["already_listed"] += 1
            return None
        save_posting_text(self.ctx.paths, job.id, posting)
        if posting.board:
            board = self.boards.get(posting.board)
            if board is not None:
                board.follow = True
        return job.id

    async def _judge(self, plan: SearchPlan, pool: list[tuple[Posting, list[str]]]) -> None:
        settings = self.ctx.settings
        watch = settings.search.watchlist
        ranked = sorted(pool, key=lambda item: -filters.priority(
            plan, item[0], watched=any(same_company(item[0].company, w) for w in watch)))
        # Companies are taken in turn, and companies already in your New list come after the others
        # (unless you watch them), so each search shows as many different companies as it can.
        queue = spread_queue(ranked, listed=self._listed_companies(), watched=watch)
        cap_list = settings.search.max_new_per_search
        cap_judge = max(12, cap_list * 2)
        budget = settings.budget_usd.per_search
        judged = listed = failures = 0
        index = 0
        while index < len(queue) and listed < cap_list and judged < cap_judge:
            self._check_stop()
            if budget and self.cost >= budget:
                self.notes.append(f"Stopped judging at the search budget (${budget:.2f}). "
                                  f"{len(queue) - index} candidates are left for the next search.")
                break
            batch: list[tuple[str, Posting, list[str]]] = []
            size = min(BATCH, cap_list - listed)  # never judge more than can still be listed
            while len(batch) < size and index < len(queue):
                chunk = queue[index: index + size - len(batch)]
                index += len(chunk)
                self.message = f"Reading postings ({min(index, len(queue))} of {len(queue)} candidates)"
                self._publish()
                reads = await asyncio.gather(*(self._read(p) for p, _ in chunk))
                for (original, keys), (read, outcome, reason) in zip(chunk, reads):
                    if read is None:
                        self.seen.record(keys, outcome, reason, self.fingerprint, original.label())
                        self._outcome(original, outcome, reason)
                        continue
                    batch.append((f"p{index}_{len(batch)}", read, keys))
                self._check_stop()
            if not batch:
                continue
            self._progress("judge", f"Judging postings against your requirements ({judged + len(batch)} so far)")
            started = time.monotonic()
            try:
                verdicts, reply = await evaluate(self.ctx.ask, self.ctx.paths, self.ctx.reading, self.ctx.profile,
                                                 [(key, posting) for key, posting, _ in batch],
                                                 resume=self.ctx.resume)
            except AgentError as exc:
                self._agent("fit_evaluator", None, ok=False, error=exc.kind, seconds=time.monotonic() - started)
                if exc.kind in ("not_signed_in", "limit"):
                    raise
                failures += 1
                for _key, posting, _keys in batch:
                    self._outcome(posting, "not_judged", exc.message)
                if failures >= 2:
                    self.notes.append("The fit evaluator failed twice, so judging stopped. "
                                      f"The details are in agents.log in {LOGS_HINT}.")
                    break
                continue
            self._agent("fit_evaluator", reply, seconds=time.monotonic() - started)
            listed_now = 0
            for key, posting, keys in batch:
                verdict = verdicts.get(key)
                if verdict is None:
                    self._outcome(posting, "not_judged", "The evaluator gave no verdict for it.")
                    continue
                judged += 1
                signs = list(dict.fromkeys(scam_signs(posting.description) + verdict.scam_signs))
                legit = legit_note(posting)
                failed = verdict.failed
                if verdict.closed:
                    self.seen.record(keys, "closed", "The posting says it is closed.", self.fingerprint, posting.label())
                    self._outcome(posting, "closed", "The posting says it is closed.")
                elif failed:
                    reason = "; ".join(f"{h['requirement']}: {h['evidence']}" for h in failed)
                    self.seen.record(keys, "rejected", reason, self.fingerprint, posting.label())
                    self._outcome(posting, "rejected", reason)
                elif len(signs) >= 2 or (signs and legit[0] in ("unverified", "board")):
                    reason = "Shows signs of a scam: " + "; ".join(signs)
                    self.seen.record(keys, "suspicious", reason, self.fingerprint, posting.label())
                    self._outcome(posting, "suspicious", reason)
                elif settings.search.strict_hard_requirements and verdict.unknown:
                    reason = "Could not verify: " + "; ".join(h["requirement"] for h in verdict.unknown)
                    self.seen.record(keys, "rejected", reason, self.fingerprint, posting.label())
                    self._outcome(posting, "rejected", reason)
                else:
                    job_id = self._list(posting, verdict, legit, signs)
                    if job_id:
                        listed += 1
                        listed_now += 1
                        self._outcome(posting, "listed", "", job_id)
            if listed_now:
                self.ctx.bus.publish("jobs_changed")
        left = len(queue) - index
        if left > 0 and not any("left for the next search" in n for n in self.notes):
            self.notes.append(f"{left} more candidates were not judged this time; the next search continues with them.")

    # --- the whole run ---------------------------------------------------------------------

    async def _run(self) -> None:
        ctx = self.ctx
        use = {"boards": True, "lists": True, "web": True, **ctx.settings.search.sources}
        self._progress("plan", "Planning the search")
        self.plan = plan = await self._make_plan()
        self._check_stop()
        if use["boards"]:
            self._progress("boards", "Looking up company job boards")
            await look_up_many(ctx.web, self.boards, ctx.settings.search.watchlist, follow=True, source="watchlist")
            await look_up_many(ctx.web, self.boards, [c["name"] for c in plan.companies], follow=True, source="suggested")
            self._check_stop()
        self._progress("collect", "Collecting postings")
        work = []
        if use["boards"]:
            work.append(self._read_boards(plan))
        if use["lists"] and plan.lists:
            work.append(self._read_lists(plan))
        if use["web"]:
            if ctx.web_agent is None:
                self.notes.append("Web search is not available in this mode.")
            else:
                work.append(self._web(plan))
        collected = await asyncio.gather(*work)
        self._check_stop()
        candidates = [p for group in collected for p in group]
        self._progress("screen", f"Checking {len(candidates)} postings")
        pool = self._screen(plan, candidates)
        self._progress("judge", f"Judging the {min(len(pool), ctx.settings.search.max_new_per_search * 2)} most promising postings")
        await self._judge(plan, pool)

    def _finish_message(self) -> str:
        listed = self.counts.get("listed", 0)
        if self.status == "failed":
            return self.message
        head = {"stopped": "Search stopped. "}.get(self.status, "Search finished. ")
        jobs = "1 new job" if listed == 1 else f"{listed} new jobs"
        parts = [f"Listed {jobs}."]
        rejected = self.counts.get("rejected", 0)
        if rejected:
            parts.append(f"{rejected} did not meet your hard requirements.")
        return head + " ".join(parts)

    async def run(self) -> None:
        try:
            await self._run()
            self.status = "done"
        except Stopped:
            self.status = "stopped"
        except asyncio.CancelledError:
            self.status = "stopped"
        except AgentError as exc:
            self.status, self.message = "failed", exc.message
        except Exception as exc:  # noqa: BLE001 - reported, never kills the app
            log.exception("Search failed")
            self.status = "failed"
            self.message = f"The search stopped with an error ({type(exc).__name__}). The details are in app.log in {LOGS_HINT}."
        finally:
            self.finished_at = now_iso()
            self.stage = "done"
            self.message = self._finish_message()
            for save in (self.boards.save, self.seen.save, self._write_log):
                try:
                    save()
                except Exception:  # noqa: BLE001
                    log.exception("Could not save search results")
            if self.ctx.headless is not None:
                await self.ctx.headless.close()
            log.info("Search %s %s: %s (cost %.3f)", self.id, self.status, dict(self.counts), self.cost)
            self._publish(force=True)

    def _write_log(self) -> None:
        folder = search_dir(self.ctx.paths)
        folder.mkdir(parents=True, exist_ok=True)
        write_json_atomic(folder / f"{self.id}.json", self.report(), backup=False)


def save_posting_text(paths: Paths, job_id: str, posting: Posting) -> None:
    """The posting's full text, kept for the application agent and for you (data/postings/<id>.md)."""
    folder = paths.data / "postings"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        lines = [f"# {posting.title}", "", f"- Company: {posting.company}", f"- Location: {posting.location}",
                 f"- Link: {posting.url}"]
        if posting.pay:
            lines.append(f"- Pay: {posting.pay}")
        if posting.posted_at:
            lines.append(f"- Posted: {posting.posted_at}")
        lines.append(f"- Read: {now_iso()}")
        lines += ["", posting.description or "(no text was read)", ""]
        (folder / f"{job_id}.md").write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save the posting text for %s: %s", job_id, exc)
