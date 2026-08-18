"""Where background work actually runs.

FastAPI's BackgroundTasks was the wrong tool for jobs that run for tens of
minutes: it borrows a thread from the pool that also serves sync request
handlers, it accepts unlimited concurrent work, and an exception inside it is
invisible. This runner owns a small dedicated pool, so a third upload queues
instead of thrashing the CPU against the first two, and every failure lands on
the job as an error the UI can show.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from ..core.cancellation import OperationCancelled, clear_stop_check, set_stop_check
from ..core.config import get_logger
from ..core.messages import CodedError, Message, raw
from .model import (
    PHASE_QUEUED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    make_progress,
)
from .store import JobNotFound, JobStore
from .types import JobRecord

logger = get_logger("jobs.runner")


class JobCancelled(RuntimeError):
    """The project was deleted while its worker was running."""


class JobContext:
    """What a job function is given: scoped access to its own job.

    Progress ticks publish over SSE without a disk write — they are worth
    streaming, not worth an fsync each. They are also where a stop request is
    noticed: a running thread cannot be killed from outside, so every phase
    reports often enough to double as a cancellation checkpoint.
    """

    def __init__(self, store: JobStore, job_id: str, operation: str):
        self.store = store
        self.job_id = job_id
        self.operation = operation

    def read(self) -> JobRecord:
        """Take a snapshot of the job to work from, unlocked."""

        return self.store.read(self.job_id)

    @contextmanager
    def edit(self) -> Iterator[JobRecord]:
        """Open the job to write results back. Keep the body short."""

        with self.store.edit(self.job_id) as job:
            yield job

    def progress(
        self,
        phase: str,
        *,
        current: int = 0,
        total: int | None = None,
        message: Message | None = None,
    ) -> None:
        try:
            with self.store.edit(self.job_id, persist=False) as job:
                if job.get("cancel_requested"):
                    # Raised before the progress write, so the last thing the UI
                    # saw stays the last thing that actually happened.
                    raise OperationCancelled(self.job_id)
                job["progress"] = make_progress(
                    phase, current=current, total=total, message=message
                )
        except JobNotFound as exc:
            raise JobCancelled(self.job_id) from exc

    def checkpoint(self, apply: Callable[[JobRecord], None]) -> None:
        """Persist partial results so a later failure does not discard them.

        Deliberately does not stop on a cancel request: a worker checkpoints
        precisely because it has something worth keeping. The `progress` call
        that follows is what ends the run, one line later and one write safer.
        """

        try:
            with self.store.edit(self.job_id) as job:
                apply(job)
        except JobNotFound as exc:
            raise JobCancelled(self.job_id) from exc

    def raise_if_cancelled(self) -> None:
        """Stop between phases, where no progress tick would come for minutes."""

        try:
            if self.store.cancel_requested(self.job_id):
                raise OperationCancelled(self.job_id)
        except JobNotFound as exc:
            raise JobCancelled(self.job_id) from exc


class JobRunner:
    def __init__(self, store: JobStore, max_workers: int):
        self._store = store
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="autocc-job"
        )

    def submit(
        self,
        job_id: str,
        operation: str,
        work: Callable[[JobContext], None],
    ) -> None:
        """Queue work for a job that is already marked as processing."""

        try:
            with self._store.edit(job_id, persist=False) as job:
                job["progress"] = make_progress(
                    PHASE_QUEUED, message=Message("progress.queued")
                )
        except JobNotFound:
            logger.info("job %s: %s not queued, project is gone", job_id, operation)
            return

        self._executor.submit(self.run_blocking, job_id, operation, work)

    def run_blocking(
        self,
        job_id: str,
        operation: str,
        work: Callable[[JobContext], None],
    ) -> None:
        """Run a job's work on the calling thread, with the same error handling.

        `submit` hands this to the pool; callers that need the result before
        continuing (tests, a future CLI) can drive it directly.
        """
        context = JobContext(self._store, job_id, operation)
        logger.info("job %s: %s started", job_id, operation)
        # Lets code with no idea what a job is — an HTTP backoff, a provider
        # loop — stop when this one is stopped.
        set_stop_check(context.raise_if_cancelled)
        try:
            work(context)
        except (JobCancelled, JobNotFound):
            logger.info("job %s: %s abandoned, project was deleted", job_id, operation)
            return
        except OperationCancelled:
            logger.info("job %s: %s stopped on request", job_id, operation)
            self._stop(job_id)
            return
        except Exception as exc:
            # Nothing may escape: an unhandled error here would leave the job on
            # "processing" forever and every SSE client hanging on it.
            logger.exception("job %s: %s failed", job_id, operation)
            self._fail(job_id, exc, operation)
            return
        finally:
            # Pool threads outlive the run; a stale check would answer for the
            # wrong job on the next one.
            clear_stop_check()
        logger.info("job %s: %s finished", job_id, operation)

    def _stop(self, job_id: str) -> None:
        """Settle a run the user stopped, keeping whatever it already wrote."""

        try:
            with self._store.edit(job_id) as job:
                job["status"] = STATUS_CANCELLED
                job["error"] = None
                job["progress"] = None
                job["cancel_requested"] = False
                if job.get("speaker_analysis_status") in {"pending", "processing"}:
                    job["speaker_analysis_status"] = "cancelled"
                    job["speaker_analysis_error"] = Message(
                        "err.speakerAnalysis.stopped"
                    ).as_dict()
                if job.get("dubbing_status") in {"pending", "processing"}:
                    job["dubbing_status"] = "cancelled"
                    job["dubbing_error"] = Message("err.dub.stopped").as_dict()
        except JobNotFound:
            return

    def _fail(self, job_id: str, exc: Exception, operation: str = "") -> None:
        try:
            with self._store.edit(job_id) as job:
                job["status"] = STATUS_ERROR
                job["error"] = describe_error(exc)
                job["progress"] = None
                # A failed dub leaves the per-step status saying "processing"
                # otherwise, and the panel keeps showing a spinner under a job
                # that has already stopped.
                if operation == "dubbing" and job.get("dubbing_status") in {
                    "pending",
                    "processing",
                }:
                    job["dubbing_status"] = "failed"
                    job["dubbing_error"] = describe_error(exc)
        except JobNotFound:
            return

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def describe_error(exc: Exception) -> dict:
    """Provider failures carry a message worth showing; nothing else does.

    A coded failure already knows how it should read. An OSError or a ValueError
    does not, so its own text is passed through verbatim — better than hiding a
    real cause behind a generic sentence. Anything else is a bug, and says so.
    """

    if isinstance(exc, CodedError):
        return exc.message.as_dict()
    if isinstance(exc, (OSError, ValueError)):
        return raw(str(exc)).as_dict()
    return Message("err.unexpected", {"type": type(exc).__name__}).as_dict()


def finish(job: JobRecord) -> None:
    """Mark a job completed and clear the transient fields.

    A stop request that arrives after the last checkpoint is dropped here on
    purpose: the work is done, and leaving the flag up would stop the *next* run
    before it started.
    """

    job["status"] = STATUS_COMPLETED
    job["error"] = None
    job["progress"] = None
    job["cancel_requested"] = False
