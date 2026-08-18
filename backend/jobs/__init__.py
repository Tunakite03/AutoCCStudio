"""Job state, persistence and background execution."""

from ..core.cancellation import OperationCancelled
from ..core.config import RUNTIME_DIR, settings
from .runner import JobCancelled, JobContext, JobRunner
from .store import JobConflict, JobNotFound, JobStore
from .types import JobRecord

store = JobStore(RUNTIME_DIR)
runner = JobRunner(store, settings.max_concurrent_jobs)

__all__ = [
    "JobCancelled",
    "OperationCancelled",
    "JobConflict",
    "JobContext",
    "JobNotFound",
    "JobRunner",
    "JobRecord",
    "JobStore",
    "runner",
    "store",
]
