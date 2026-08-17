"""Ownership of job state: locking, persistence, and change notification.

The rule this module exists to enforce is that nobody mutates a job outside a
`store.edit(job_id)` block. Workers used to hold the same dict the request
handlers were editing, so a save could land on top of a user's edit, a delete
could be undone by a worker's final write, and two writers could persist
revisions out of order.

Critical sections are deliberately short. A worker takes what it needs out of
the job, does its minutes-long work unlocked, then reopens the job to write
results back — so `GET /api/jobs/{id}` never waits on a transcription.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import get_logger
from ..messages import CodedError, Message
from .model import is_valid_job_id, public_job

logger = get_logger("jobs.store")


class JobNotFound(LookupError):
    """No job with this id, or the id could never name one."""


class JobConflict(CodedError):
    """The job is busy and the requested transition is not allowed."""

    def __init__(self, code: str | Message = "err.job.busy", **params):
        super().__init__(code, **params)


class JobStore:
    def __init__(self, runtime_dir: Path):
        self._runtime_dir = runtime_dir
        self._jobs: dict[str, dict] = {}
        self._registry_lock = threading.Lock()
        self._job_locks: dict[str, threading.RLock] = {}
        self._subscribers: dict[str, set[tuple]] = {}
        self._subscribers_lock = threading.Lock()
        self._summary_cache: dict[str, tuple[tuple[float, float], dict]] = {}

    # ── Paths ────────────────────────────────────────────────────────

    @property
    def runtime_dir(self) -> Path:
        return self._runtime_dir

    def job_dir(self, job_id: str) -> Path:
        if not is_valid_job_id(job_id):
            # Centralised so no route can forget it: an id is the only part of a
            # job path a caller controls, and it lands in filesystem operations.
            raise JobNotFound(job_id)
        return self._runtime_dir / job_id

    def job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    # ── Locking ──────────────────────────────────────────────────────

    def _lock_for(self, job_id: str) -> threading.RLock:
        with self._registry_lock:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = threading.RLock()
                self._job_locks[job_id] = lock
            return lock

    # ── Reading ──────────────────────────────────────────────────────

    def _load(self, job_id: str) -> dict:
        """Return the live job dict, recovering it from disk when needed.

        Callers must hold the job lock.
        """

        with self._registry_lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job

        path = self.job_file(job_id)
        if not path.exists():
            raise JobNotFound(job_id)
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("job %s: metadata unreadable: %s", job_id, exc)
            raise JobNotFound(job_id) from exc

        if job.get("status") == "processing":
            # In-memory jobs return above. A processing job found only on disk
            # belongs to a previous server process and has no worker left.
            job["status"] = "error"
            job["error"] = Message("err.job.interrupted").as_dict()
            job["progress"] = None
            if job.get("speaker_analysis_status") in {"pending", "processing"}:
                job["speaker_analysis_status"] = "not_run"
                job["speaker_analysis_error"] = job["error"]
            logger.info("job %s: recovered as interrupted after a restart", job_id)
            with self._registry_lock:
                self._jobs[job_id] = job
            self._persist(job)
            return job

        with self._registry_lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> dict:
        """A snapshot safe to read outside the lock."""

        with self._lock_for(job_id):
            return public_job(self._load(job_id))

    def read(self, job_id: str) -> dict:
        """A deep-ish copy of the raw job for callers that need internal fields."""

        with self._lock_for(job_id):
            job = self._load(job_id)
            return {**job, "cues": [dict(cue) for cue in job.get("cues", [])]}

    def cancel_requested(self, job_id: str) -> bool:
        """Read the stop flag alone.

        Not an `edit`: opening one would bump the revision and publish a
        snapshot, and a worker polling between phases has nothing to report.
        """

        with self._lock_for(job_id):
            return bool(self._load(job_id).get("cancel_requested"))

    def exists(self, job_id: str) -> bool:
        try:
            self.get(job_id)
        except JobNotFound:
            return False
        return True

    # ── Writing ──────────────────────────────────────────────────────

    @contextmanager
    def edit(self, job_id: str, *, persist: bool = True) -> Iterator[dict]:
        """Open a job for mutation; persists and publishes on a clean exit.

        Keep the body short — this lock serialises every reader of the job.
        """

        with self._lock_for(job_id):
            job = self._load(job_id)
            yield job
            self._persist(job, persist=persist)

    def create(self, job: dict) -> dict:
        """Register a freshly built job and write it out."""

        job_id = job["id"]
        with self._lock_for(job_id):
            with self._registry_lock:
                self._jobs[job_id] = job
            self._persist(job)
        return job

    def _persist(self, job: dict, *, persist: bool = True) -> None:
        """Bump the revision, optionally write to disk, then notify subscribers.

        Callers must hold the job lock. `persist=False` publishes a live update
        (progress ticks) without paying a disk write for every one of them.
        """

        job_id = job["id"]
        if job.get("deleted"):
            logger.info("job %s: dropped a state update for a deleted project", job_id)
            return

        job["revision"] = int(job.get("revision", 0)) + 1
        snapshot = public_job(job)
        with self._registry_lock:
            # mtime alone is not enough to invalidate: two writes milliseconds
            # apart can share a timestamp, and the dashboard would then show a
            # stale cue count until something else touched the directory.
            self._summary_cache.pop(job_id, None)

        if persist:
            payload = json.dumps(job, ensure_ascii=False, indent=2)
            try:
                self._write_atomically(job_id, payload)
            except OSError as exc:
                # The in-memory job stays authoritative; losing the disk copy is
                # recoverable, losing the running job is not.
                logger.warning("job %s: could not persist metadata: %s", job_id, exc)

        self._publish(snapshot)

    def _write_atomically(self, job_id: str, payload: str) -> None:
        """Write via a sibling temp file so a crash cannot truncate job.json."""

        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "job.json.tmp"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.job_file(job_id))

    def delete(self, job_id: str) -> None:
        directory = self.job_dir(job_id)
        if not directory.exists() or not directory.is_dir():
            raise JobNotFound(job_id)

        with self._lock_for(job_id):
            with self._subscribers_lock:
                self._subscribers.pop(job_id, None)
            with self._registry_lock:
                removed = self._jobs.pop(job_id, None)
                self._summary_cache.pop(job_id, None)
                if removed is not None:
                    # A running worker still holds this dict; the tombstone makes
                    # its final write a no-op instead of recreating the directory.
                    removed["deleted"] = True
            shutil.rmtree(directory)

        with self._registry_lock:
            self._job_locks.pop(job_id, None)
        logger.info("job %s: deleted", job_id)

    def discard_from_memory(self, job_id: str) -> None:
        """Forget cached state without touching disk (used when a create fails)."""

        with self._registry_lock:
            self._jobs.pop(job_id, None)
            self._job_locks.pop(job_id, None)
            self._summary_cache.pop(job_id, None)

    # ── Listing ──────────────────────────────────────────────────────

    def summaries(self, summarise) -> list[dict]:
        """Every project on disk, newest first.

        Summarising stats every file in every project directory, so results are
        cached. Writes through this store evict their own entry; the mtime pair
        is the fallback for anything that changed the directory behind our back.
        """

        if not self._runtime_dir.exists():
            return []

        summaries = []
        for job_dir in self._runtime_dir.iterdir():
            if not job_dir.is_dir() or not is_valid_job_id(job_dir.name):
                continue
            metadata = job_dir / "job.json"
            try:
                stamp = (metadata.stat().st_mtime, job_dir.stat().st_mtime)
            except OSError:
                continue  # vanished between iterdir and stat

            cached = self._summary_cache.get(job_dir.name)
            if cached is not None and cached[0] == stamp:
                summaries.append(cached[1])
                continue

            try:
                job = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # a half-written project should not break the list
            if not job.get("id"):
                continue
            summary = summarise(job, job_dir)
            self._summary_cache[job_dir.name] = (stamp, summary)
            summaries.append(summary)

        summaries.sort(key=lambda item: item["updated_at"], reverse=True)
        return summaries

    # ── Change notification ──────────────────────────────────────────

    def subscribe(self, job_id: str) -> tuple:
        subscriber = (asyncio.get_running_loop(), asyncio.Queue(maxsize=1))
        with self._subscribers_lock:
            self._subscribers.setdefault(job_id, set()).add(subscriber)
        return subscriber

    def unsubscribe(self, job_id: str, subscriber: tuple) -> None:
        with self._subscribers_lock:
            subscribers = self._subscribers.get(job_id)
            if subscribers is None:
                return
            subscribers.discard(subscriber)
            if not subscribers:
                self._subscribers.pop(job_id, None)

    def _publish(self, snapshot: dict) -> None:
        job_id = snapshot["id"]
        revision = int(snapshot.get("revision", 0))
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        with self._subscribers_lock:
            subscribers = tuple(self._subscribers.get(job_id, ()))

        stale = []
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_enqueue_latest, queue, (revision, payload))
            except RuntimeError:
                stale.append((loop, queue))

        if stale:
            with self._subscribers_lock:
                active = self._subscribers.get(job_id)
                if active is not None:
                    active.difference_update(stale)
                    if not active:
                        self._subscribers.pop(job_id, None)


def _enqueue_latest(queue: asyncio.Queue, item: tuple[int, str]) -> None:
    """Keep only the newest snapshot; a slow client wants current state, not history."""

    while queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    queue.put_nowait(item)
