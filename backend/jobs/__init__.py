"""Job state, persistence and background execution."""

from ..config import RUNTIME_DIR, settings
from .runner import JobCancelled, JobContext, JobRunner
from .store import JobConflict, JobNotFound, JobStore

store = JobStore(RUNTIME_DIR)
runner = JobRunner(store, settings.max_concurrent_jobs)

__all__ = [
    "JobCancelled",
    "JobConflict",
    "JobContext",
    "JobNotFound",
    "JobRunner",
    "JobStore",
    "runner",
    "store",
]
