"""The signal that stops a run in progress.

It lives on its own because the two sides that need it must not import each
other: `ai` raises it from deep inside a provider loop, while `jobs.runner` is
what decides what a stopped run leaves behind.

Deliberately not an `AIProviderError`: a stop is not a provider failure, so
every `except Exception` that turns provider trouble into a job error has to let
this one through untouched.
"""

from __future__ import annotations

import threading


class OperationCancelled(RuntimeError):
    """The user asked for the work in progress to stop."""


# How a worker thread learns it should stop, without every function between the
# job layer and the wait growing a parameter for it. Thread-local because each
# worker answers to its own job.
_current = threading.local()


def set_stop_check(check) -> None:
    """Register the calling thread's stop check for the length of one run."""

    _current.check = check


def clear_stop_check() -> None:
    _current.check = None


def raise_if_stopped() -> None:
    """Raise if this thread's job has been stopped; do nothing if it has no job.

    Called from inside long waits — a rate-limit backoff can be a minute, and a
    stop request that is only read between HTTP calls would look ignored.
    """

    check = getattr(_current, "check", None)
    if check is not None:
        check()
