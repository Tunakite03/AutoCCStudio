"""Where background work actually runs.

FastAPI's BackgroundTasks was the wrong tool for jobs that run for tens of
minutes: it borrows a thread from the pool that also serves sync request
handlers, it accepts unlimited concurrent work, and an exception inside it is
invisible. This runner owns a small dedicated pool, so a third upload queues
instead of thrashing the CPU against the first two, and every failure lands on
the job as an error the UI can show.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Callable, Iterator

from ..config import get_logger
from .model import (
    PHASE_QUEUED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    make_progress,
)
from .store import JobNotFound, JobStore

logger = get_logger("jobs.runner")


class JobCancelled(RuntimeError):
    """The project was deleted while its worker was running."""


class JobContext:
    """What a job function is given: scoped access to its own job.

    Progress ticks publish over SSE without a disk write — they are worth
    streaming, not worth an fsync each.
    """

    def __init__(self, store: JobStore, job_id: str, operation: str):
        self.store = store
        self.job_id = job_id
        self.operation = operation

    def read(self) -> dict:
        """Take a snapshot of the job to work from, unlocked."""

        return self.store.read(self.job_id)

    @contextmanager
    def edit(self) -> Iterator[dict]:
        """Open the job to write results back. Keep the body short."""

        with self.store.edit(self.job_id) as job:
            yield job

    def progress(
        self,
        phase: str,
        *,
        current: int = 0,
        total: int | None = None,
        message: str = "",
    ) -> None:
        try:
            with self.store.edit(self.job_id, persist=False) as job:
                job["progress"] = make_progress(
                    phase, current=current, total=total, message=message
                )
        except JobNotFound as exc:
            raise JobCancelled(self.job_id) from exc

    def checkpoint(self, apply: Callable[[dict], None]) -> None:
        """Persist partial results so a later failure does not discard them."""

        try:
            with self.store.edit(self.job_id) as job:
                apply(job)
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
                    PHASE_QUEUED, message="Đang chờ đến lượt xử lý"
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
        try:
            work(context)
        except (JobCancelled, JobNotFound):
            logger.info("job %s: %s abandoned, project was deleted", job_id, operation)
            return
        except Exception as exc:
            # Nothing may escape: an unhandled error here would leave the job on
            # "processing" forever and every SSE client hanging on it.
            logger.exception("job %s: %s failed", job_id, operation)
            self._fail(job_id, exc)
            return
        logger.info("job %s: %s finished", job_id, operation)

    def _fail(self, job_id: str, exc: Exception) -> None:
        try:
            with self._store.edit(job_id) as job:
                job["status"] = STATUS_ERROR
                job["error"] = describe_error(exc)
                job["progress"] = None
        except JobNotFound:
            return

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def describe_error(exc: Exception) -> str:
    """Provider failures carry a message worth showing; nothing else does."""

    from ..ai import AIProviderError

    if isinstance(exc, (AIProviderError, OSError, ValueError)):
        return str(exc)
    return (
        f"Lỗi không mong đợi khi xử lý ({type(exc).__name__}). "
        "Xem log server để biết chi tiết."
    )


def finish(job: dict) -> None:
    """Mark a job completed and clear the transient fields."""

    job["status"] = STATUS_COMPLETED
    job["error"] = None
    job["progress"] = None
