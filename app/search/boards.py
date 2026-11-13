"""data/company_boards.json: which job board each company uses, learned as the app goes.

A company's board is found in three ways: by trying likely board names for a
company you put on your watchlist (or that the planner suggests), from the
address of any posting the search reads, and from links you add yourself.
Boards marked ``follow`` are read on every search. Companies whose board could
not be found are remembered too, so they are not looked up again for a month.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ..core.dedupe import company_key
from ..core.jsonfile import JsonCorrupt, read_json, write_json_atomic
from ..core.models import now_iso
from . import sources
from .http import FetchError, Web
from .models import Board, BoardsFile, CompanyEntry
from .sources import ashby, greenhouse, lever

log = logging.getLogger("jobapp.search.boards")

RECHECK_DAYS = 30


def slug_candidates(name: str) -> list[str]:
    words = company_key(name).split()
    if not words:
        return []
    joined = "".join(words)
    out = [joined]
    if len(words) > 1:
        out.append("-".join(words))
    return [s for s in dict.fromkeys(out) if re.fullmatch(r"[a-z0-9-]{2,60}", s)]


def same_company(a: str, b: str) -> bool:
    ka, kb = company_key(a), company_key(b)
    if not ka or not kb:
        return False
    if ka == kb or ka.replace(" ", "") == kb.replace(" ", ""):
        return True
    first_a, first_b = ka.split()[0], kb.split()[0]
    return len(first_a) >= 4 and first_a == first_b


class BoardStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> BoardsFile:
        try:
            return BoardsFile.model_validate(read_json(self.path))
        except FileNotFoundError:
            return BoardsFile()
        except (JsonCorrupt, ValidationError) as exc:
            log.error("company_boards.json could not be read, starting a new one: %s", exc)
            return BoardsFile()

    def save(self) -> None:
        write_json_atomic(self.path, self.data.model_dump(mode="json"))

    # --- boards -------------------------------------------------------------------------

    def get(self, key: str) -> Optional[Board]:
        return self.data.boards.get(key.lower())

    def add(self, board: Board, *, follow: bool = False, source: str = "found") -> Board:
        key = board.key
        existing = self.data.boards.get(key)
        if existing is None:
            board.follow = follow
            board.source = board.source or source
            board.verified_at = board.verified_at or now_iso()
            self.data.boards[key] = board
            return board
        existing.follow = existing.follow or follow
        if board.company and not existing.company:
            existing.company = board.company
        if board.region and not existing.region:
            existing.region = board.region
        return existing

    def followed(self) -> list[Board]:
        return [b for b in self.data.boards.values() if b.follow]

    def note_read(self, key: str, count: Optional[int], error: str = "") -> None:
        board = self.data.boards.get(key)
        if board is not None:
            board.last_read_at = now_iso()
            board.last_count = count
            board.last_error = error

    def learn_from_url(self, url: str, company: str, *, follow: bool = False) -> Optional[Board]:
        found = sources.identify(url)
        if not found:
            return None
        return self.add(sources.board_for(found, company), follow=follow, source="found")

    # --- companies ------------------------------------------------------------------------

    def company(self, name: str) -> Optional[CompanyEntry]:
        return self.data.companies.get(company_key(name))

    def needs_lookup(self, name: str) -> bool:
        entry = self.company(name)
        if entry is None or not entry.checked_at:
            return True
        try:
            checked = datetime.fromisoformat(entry.checked_at)
        except ValueError:
            return True
        return datetime.now(checked.tzinfo) - checked > timedelta(days=RECHECK_DAYS)

    def boards_of(self, name: str) -> list[Board]:
        entry = self.company(name)
        keys = list(entry.boards) if entry else []
        # Boards learned from posting links carry the company's name too.
        for key, board in self.data.boards.items():
            if key not in keys and board.company and same_company(board.company, name):
                keys.append(key)
        return [self.data.boards[k] for k in keys if k in self.data.boards]

    def record_company(self, name: str, boards: list[Board], note: str = "") -> None:
        key = company_key(name)
        if not key:
            return
        self.data.companies[key] = CompanyEntry(
            name=name.strip(), boards=[b.key for b in boards], checked_at=now_iso(), note=note)

    def view(self, names: list[str]) -> list[dict]:
        out = []
        for name in names:
            entry = self.company(name)
            boards = self.boards_of(name)
            out.append({
                "name": name,
                "boards": [{"ats": b.ats, "system": sources.NAMES[b.ats], "token": b.token,
                            "last_count": b.last_count, "last_error": b.last_error} for b in boards],
                "checked": bool(entry and entry.checked_at),
                "note": entry.note if entry else "",
            })
        return out


async def _try_greenhouse(web: Web, name: str, slug: str) -> Optional[Board]:
    try:
        board_title = await greenhouse.board_name(web, slug)
    except FetchError:
        return None
    if board_title and not same_company(board_title, name) and slug not in company_key(board_title).replace(" ", ""):
        return None
    return Board(ats="greenhouse", token=slug, company=name)


async def _try_lever(web: Web, name: str, slug: str) -> Optional[Board]:
    region = await lever.find_region(web, slug)
    return None if region is None else Board(ats="lever", token=slug, company=name, region=region)


async def _try_ashby(web: Web, name: str, slug: str) -> Optional[Board]:
    try:
        jobs = await ashby.read_board(web, Board(ats="ashby", token=slug, company=name))
    except FetchError:
        return None
    return Board(ats="ashby", token=slug, company=name) if jobs else None


async def look_up(web: Web, store: BoardStore, name: str, *, follow: bool, source: str) -> list[Board]:
    """Find a company's board on Greenhouse, Lever or Ashby by trying likely names."""
    found: list[Board] = []
    for slug in slug_candidates(name):
        results = await asyncio.gather(
            _try_greenhouse(web, name, slug), _try_lever(web, name, slug), _try_ashby(web, name, slug))
        found = [b for b in results if b is not None]
        if found:
            break
    boards = [store.add(b, follow=follow, source=source) for b in found]
    known = [b for b in store.boards_of(name) if b.key not in {x.key for x in boards}]
    if follow:
        for board in known:
            board.follow = True
    note = "" if boards or known else "No board found on Greenhouse, Lever or Ashby. The web search still covers it."
    store.record_company(name, boards + known, note)
    return boards + known


async def look_up_many(web: Web, store: BoardStore, names: list[str], *, follow: bool, source: str,
                       force: bool = False) -> dict[str, list[Board]]:
    wanted = [n for n in dict.fromkeys(n.strip() for n in names) if n and (force or store.needs_lookup(n))]
    results = await asyncio.gather(*(look_up(web, store, n, follow=follow, source=source) for n in wanted))
    out = dict(zip(wanted, results))
    for name in names:
        if name not in out:
            boards = store.boards_of(name)
            if follow:
                for board in boards:
                    board.follow = True
            out[name] = boards
    return out
