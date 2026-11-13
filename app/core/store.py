"""The tracker: every job the app has ever listed, and its status.

This is the only code that writes ``data/tracker.json``. Records are never
deleted. A skipped or applied job stays on file so that it is recognised and
left out if a later search finds it again.
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import lifecycle
from .dedupe import job_id_for, make_dedupe_keys
from .jsonfile import JsonCorrupt, backup_path, read_json, write_json_atomic
from .models import Job, StatusDetail

log = logging.getLogger("jobapp.store")

TRACKER_VERSION = 1


class DuplicateJob(Exception):
    def __init__(self, existing: Job):
        super().__init__(f"Already in the tracker: {existing.company}, {existing.title}")
        self.existing = existing


class TrackerUnreadable(Exception):
    """Neither the tracker file nor its backup could be read."""


class TrackerStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._stamp: Optional[tuple[int, int]] = None  # (mtime_ns, size) of the file we hold
        self.recovered_from_backup = False

    # --- reading ----------------------------------------------------------

    def _file_stamp(self) -> Optional[tuple[int, int]]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _parse(self, raw: object, source: Path) -> dict[str, Job]:
        if not isinstance(raw, dict) or not isinstance(raw.get("jobs"), dict):
            raise JsonCorrupt(source, "expected an object with a 'jobs' object inside")
        jobs: dict[str, Job] = {}
        for job_id, record in raw["jobs"].items():
            job = Job.model_validate(record)
            jobs[job.id or job_id] = job
        return jobs

    def _load(self) -> None:
        stamp = self._file_stamp()
        if stamp is None:
            self._jobs, self._stamp = {}, None
            return
        try:
            self._jobs = self._parse(read_json(self.path), self.path)
            self._stamp = stamp
            return
        except (JsonCorrupt, ValueError) as exc:
            log.error("Tracker file could not be read: %s", exc)
            problem = exc
        # Fall back to the copy kept from the previous write, and keep the
        # damaged file for inspection instead of overwriting it.
        bak = backup_path(self.path)
        try:
            jobs = self._parse(read_json(bak), bak)
        except Exception as exc:  # noqa: BLE001 - any failure here means no usable copy
            raise TrackerUnreadable(
                f"{self.path.name} is damaged and there is no usable backup. "
                f"Nothing was changed. Details: {problem}"
            ) from exc
        kept = self.path.with_name(f"tracker.damaged-{datetime.now():%Y%m%d-%H%M%S}.json")
        shutil.copy2(self.path, kept)
        log.warning("Recovered the tracker from %s; damaged file kept as %s", bak.name, kept.name)
        self._jobs = jobs
        self.recovered_from_backup = True
        self._save(backup=False)

    def _fresh(self) -> None:
        # Reload if the file changed behind our back (for example a hand edit).
        if self._stamp is None or self._file_stamp() != self._stamp:
            self._load()

    def _save(self, backup: bool = True) -> None:
        payload = {
            "version": TRACKER_VERSION,
            "jobs": {job_id: job.to_json() for job_id, job in self._jobs.items()},
        }
        write_json_atomic(self.path, payload, backup=backup)
        self._stamp = self._file_stamp()

    # --- public -------------------------------------------------------------

    def all_jobs(self) -> list[Job]:
        with self._lock:
            self._fresh()
            return [job.model_copy(deep=True) for job in self._jobs.values()]

    def get(self, job_id: str) -> Job:
        with self._lock:
            self._fresh()
            return self._jobs[job_id].model_copy(deep=True)

    def find_duplicate(self, keys: list[str]) -> Optional[Job]:
        with self._lock:
            self._fresh()
            wanted = set(keys)
            for job in self._jobs.values():
                if wanted.intersection(job.dedupe_keys):
                    return job.model_copy(deep=True)
            return None

    def add(self, job: Job) -> Job:
        """Add a new listing. Raises DuplicateJob if it is already on file."""
        with self._lock:
            self._fresh()
            if not job.dedupe_keys:
                job.dedupe_keys = make_dedupe_keys(job.url, job.company, job.title, job.location)
            existing = self.find_duplicate(job.dedupe_keys)
            if existing is not None:
                raise DuplicateJob(existing)
            if not job.id:
                job.id = job_id_for(job.url)
            base, n = job.id, 2
            while job.id in self._jobs:
                job.id = f"{base}_{n}"
                n += 1
            self._jobs[job.id] = job.model_copy(deep=True)
            self._save()
            return job

    def replace_all(self, jobs: list[Job]) -> None:
        """Overwrite the whole tracker. Used only to load sample data."""
        with self._lock:
            self._jobs = {job.id: job.model_copy(deep=True) for job in jobs}
            self._save(backup=False)

    def move(
        self,
        job_id: str,
        move: str,
        *,
        by: str = "user",
        detail: Optional[StatusDetail] = None,
        note: str = "",
    ) -> Job:
        """Apply a lifecycle move. Raises KeyError or lifecycle.NotAllowed."""
        with self._lock:
            self._fresh()
            job = self._jobs[job_id]
            lifecycle.apply(job, move, by=by, detail=detail, note=note)
            self._save()
            return job.model_copy(deep=True)

    def update(self, job: Job) -> Job:
        """Save changed details of an existing job (not its status)."""
        with self._lock:
            self._fresh()
            if job.id not in self._jobs:
                raise KeyError(job.id)
            self._jobs[job.id] = job.model_copy(deep=True)
            self._save()
            return job
