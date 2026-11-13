"""The life of a job listing: which status can follow which. PLAN.md section 5.

There are two kinds of moves:

* actions, taken by you with a button: skip, restore, prepare, continue,
  mark_applied;
* events, reported by the app while it works on an application:
  start_filling, needs_input, page_done, ready_for_review, failed,
  posting_closed.
"""

from __future__ import annotations

from typing import Optional

from .models import HistoryEntry, Job, Status, StatusDetail, now_iso

S = Status

WAITING = {S.WAITING_FOR_INPUT, S.WAITING_FOR_PAGE_REVIEW, S.WAITING_FOR_REVIEW}
ACTIVE = {S.QUEUED, S.FILLING}

USER_ACTIONS = ("skip", "restore", "prepare", "continue", "mark_applied")
SYSTEM_EVENTS = (
    "start_filling", "needs_input", "page_done", "ready_for_review", "failed", "posting_closed",
)

# Which dashboard tab a status belongs to.
GROUPS = {
    S.NEW: "new",
    S.QUEUED: "in_progress",
    S.FILLING: "in_progress",
    S.WAITING_FOR_INPUT: "waiting",
    S.WAITING_FOR_PAGE_REVIEW: "waiting",
    S.WAITING_FOR_REVIEW: "waiting",
    S.ERROR: "waiting",
    S.APPLIED: "applied",
    S.SKIPPED: "hidden",
    S.CLOSED: "hidden",
}


class NotAllowed(Exception):
    """The move does not make sense from the job's current status."""


def _target(current: Status, move: str) -> Status:
    if move == "skip":
        if current == S.APPLIED:
            raise NotAllowed("This job is marked as applied, so it cannot be skipped.")
        if current == S.SKIPPED:
            raise NotAllowed("This job is already skipped.")
        return S.SKIPPED
    if move == "restore":
        if current not in (S.SKIPPED, S.APPLIED, S.CLOSED):
            raise NotAllowed("Only skipped, applied or closed jobs can be moved back to New.")
        return S.NEW
    if move == "prepare":
        if current not in (S.NEW, S.ERROR):
            raise NotAllowed("Prepare starts a new application. This one has already been started.")
        return S.QUEUED
    if move == "continue":
        if current not in WAITING:
            raise NotAllowed("Continue resumes a paused application. This one is not paused.")
        return S.FILLING
    if move == "mark_applied":
        if current == S.APPLIED:
            raise NotAllowed("This job is already marked as applied.")
        return S.APPLIED

    if move == "start_filling":
        if current != S.QUEUED:
            raise NotAllowed(f"Cannot start filling from status {current.value}.")
        return S.FILLING
    if move in ("needs_input", "page_done", "ready_for_review"):
        if current != S.FILLING:
            raise NotAllowed(f"Cannot report {move} from status {current.value}.")
        return {
            "needs_input": S.WAITING_FOR_INPUT,
            "page_done": S.WAITING_FOR_PAGE_REVIEW,
            "ready_for_review": S.WAITING_FOR_REVIEW,
        }[move]
    if move == "failed":
        if current not in ACTIVE:
            raise NotAllowed(f"Cannot report a failure from status {current.value}.")
        return S.ERROR
    if move == "posting_closed":
        if current in (S.APPLIED, S.SKIPPED, S.CLOSED):
            raise NotAllowed(f"Cannot close a posting from status {current.value}.")
        return S.CLOSED
    raise NotAllowed(f"Unknown move: {move}")


def can(job: Job, move: str) -> bool:
    try:
        _target(job.status, move)
        return True
    except NotAllowed:
        return False


def apply(
    job: Job,
    move: str,
    *,
    by: str = "user",
    detail: Optional[StatusDetail] = None,
    note: str = "",
) -> Job:
    """Move the job to its next status and write the move into its history."""
    target = _target(job.status, move)
    job.history.append(
        HistoryEntry(at=now_iso(), from_status=job.status.value, to=target.value, by=by, note=note)
    )
    job.status = target
    # A status message only describes the status it was written for.
    job.status_detail = detail or StatusDetail()
    return job


def group_of(job: Job) -> str:
    return GROUPS[job.status]
