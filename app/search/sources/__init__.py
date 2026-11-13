"""The job systems the search can read directly, and how to tell a link belongs to one."""

from __future__ import annotations

from typing import Optional

from ..http import Web
from ..models import Board, Posting
from . import ashby, greenhouse, lever, workday

SYSTEMS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday}
NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday"}


def identify(url: str) -> Optional[dict]:
    """{"ats", "token", "job_id", ...} if the link is on a job system the app reads, else None."""
    for name, module in SYSTEMS.items():
        try:
            found = module.identify(url)
        except (ValueError, IndexError):
            found = None
        if found:
            return {"ats": name, **found}
    return None


def board_for(found: dict, company: str = "") -> Board:
    return Board(ats=found["ats"], token=found["token"], company=company, region=found.get("region", ""))


async def read_board(web: Web, board: Board, search_words: list[str]) -> list[Posting]:
    if board.ats == "workday":
        return await workday.search(web, board, search_words)
    return await SYSTEMS[board.ats].read_board(web, board)


async def read_job(web: Web, found: dict, company: str = "") -> Posting:
    ats, token, job_id = found["ats"], found["token"], found["job_id"]
    if ats == "lever":
        return await lever.read_job(web, token, job_id, company, found.get("region", ""))
    return await SYSTEMS[ats].read_job(web, token, job_id, company)
