"""One HTTP path for every outbound provider call.

Deepgram used `requests` while the LLM adapter used `urllib`, so timeouts,
retries and error reporting had to be written twice and only ever got fixed on
one side. Everything goes through `post` here instead.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from .apikeys import CredentialPool
from .cancellation import raise_if_stopped
from .config import get_logger, settings
from .messages import CodedError

logger = get_logger("http")

# Transient by nature: the provider is rate-limiting us or briefly unavailable.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RATE_LIMITED_STATUS = 429
# One bad key in a pool of eight should not kill every eighth request. These are
# not retried on the same key — they are the provider saying "not this one".
KEY_REJECTED_STATUS = frozenset({401, 403})
# Long enough to mean "until you fix .env and restart", short enough that a
# provider having a bad minute does not lose the key for the session.
KEY_REJECTED_COOLDOWN_SECONDS = 300.0
BACKOFF_BASE_SECONDS = 1.5
# A rate limit clears on the provider's schedule, not ours, so it gets its own
# budget: waiting seconds like a flaky connection just spends every retry before
# the window has moved at all.
RATE_LIMIT_BASE_SECONDS = 2.0
RATE_LIMIT_MAX_SECONDS = 60.0
# Providers do answer a 429 with "retry-after: 0"; hammering is what got us
# rate limited in the first place.
RATE_LIMIT_MIN_SECONDS = 1.0
# Whatever the provider tells us about the wait, under any of its spellings.
RETRY_AFTER_HEADERS = (
    "retry-after",
    "x-ratelimit-reset-after",
    "ratelimitbysize-reset",
)
# A wait is served in slices this long so a stopped job does not have to sit
# through the rest of a minute-long backoff before anything notices.
STOP_CHECK_INTERVAL_SECONDS = 0.5


class HTTPClientError(CodedError):
    """A provider call failed after exhausting retries."""

    def __init__(
        self,
        code,
        *,
        status_code: int | None = None,
        body: str = "",
        **params,
    ):
        super().__init__(code, **params)
        self.status_code = status_code
        self.body = body


def _requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dependency
        raise HTTPClientError("err.http.requestsMissing") from exc
    return requests


def _wait(seconds: float) -> None:
    """Sleep, but in slices, checking for a stop request between them."""

    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        raise_if_stopped()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, STOP_CHECK_INTERVAL_SECONDS))


def _sleep_for_attempt(attempt: int) -> None:
    _wait(BACKOFF_BASE_SECONDS * (2**attempt))


def _retry_after_seconds(response) -> float | None:
    """How long the provider asked us to wait, in seconds, if it said so at all."""

    headers = getattr(response, "headers", None) or {}
    try:
        lowered = {str(key).lower(): value for key, value in headers.items()}
    except AttributeError:
        return None
    for name in RETRY_AFTER_HEADERS:
        raw = lowered.get(name)
        if raw is None:
            continue
        text = str(raw).strip()
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        try:
            # Retry-After also comes as an HTTP date.
            deadline = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            continue
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())
    return None


def _pool_exhausted(label: str, credentials, tried: int, detail: str) -> HTTPClientError:
    """The one condition worth stopping a job for: no key left to try, now."""

    logger.warning("%s: all %s API keys stayed rate limited", label, len(credentials))
    return HTTPClientError(
        "err.http.poolExhausted",
        status_code=RATE_LIMITED_STATUS,
        body=detail,
        label=label,
        keys=len(credentials),
        tried=tried,
        detail=detail,
    )


def _rate_limit_delay(response, already_waited: int) -> float:
    requested = _retry_after_seconds(response)
    if requested is None:
        requested = RATE_LIMIT_BASE_SECONDS * (2**already_waited)
    return min(max(requested, RATE_LIMIT_MIN_SECONDS), RATE_LIMIT_MAX_SECONDS)


def post(
    url: str,
    *,
    headers: dict[str, str],
    timeout: tuple[float, float],
    label: str,
    data: Any = None,
    json_body: Any = None,
    body_factory: Callable[[], Iterator[bytes] | Any] | None = None,
    retries: int | None = None,
    credentials: CredentialPool | None = None,
):
    """POST with bounded retries on transient failures.

    `body_factory` exists for streamed file uploads: a retry cannot reuse a
    consumed file handle, so the caller supplies a fresh one per attempt.

    A 429 is counted separately and waits as long as the provider asks. Sharing
    one budget with connection errors meant a rate limit was given a couple of
    seconds to clear and then reported as a hard failure, which for a per-minute
    quota it never was.

    `credentials` turns that wait into a rotation: the limited key is put on a
    cooldown of its own and the next request goes out under a different one
    immediately. Waiting only happens when *every* key is cooling at the same
    moment, and even then no key is written off — the pool hands back whichever
    recovers first, which is usually the one limited earliest.
    """

    requests = _requests()
    attempts = max(0, retries if retries is not None else settings.http_retries) + 1
    rate_limit_attempts = max(0, settings.http_rate_limit_retries) + 1
    pooled = credentials is not None and len(credentials) > 0
    # Each key gets the full budget: with a pool a 429 costs a rotation rather
    # than a wait, and rotations must not spend the waits.
    rate_limit_budget = rate_limit_attempts * (len(credentials) if pooled else 1)
    used = 1
    rate_limited = 0
    # Waits keep their own budget, separate from rotations. Sharing one would
    # make a single-key install give up in half the attempts it does today, and
    # an eight-key one wait eight times as long before reporting anything.
    waited = 0
    rejected = 0
    detail = ""

    while True:
        raise_if_stopped()
        key = None
        request_headers = headers
        if pooled:
            key, cooldown = credentials.acquire()
            if key is None:
                # Every key is limited at the same moment — the only situation
                # worth waiting for, since each one recovers on its own clock.
                if waited >= rate_limit_attempts:
                    raise _pool_exhausted(label, credentials, rate_limited, detail)
                waited += 1
                delay = min(cooldown, RATE_LIMIT_MAX_SECONDS)
                logger.warning(
                    "%s: all %s API keys are rate limited, waiting %.1fs (%s/%s) "
                    "for the first to recover",
                    label, len(credentials), delay, waited, rate_limit_attempts,
                )
                _wait(delay)
                continue
            request_headers = {**headers, **credentials.authorization(key)}

        payload = body_factory() if body_factory is not None else data
        try:
            response = requests.post(
                url,
                headers=request_headers,
                data=payload,
                json=json_body,
                timeout=timeout,
            )
        except (OSError, requests.RequestException) as exc:
            if used < attempts:
                logger.warning(
                    "%s unreachable (attempt %s/%s): %s", label, used, attempts, exc
                )
                _sleep_for_attempt(used - 1)
                used += 1
                continue
            logger.warning("%s unreachable: %s", label, exc)
            raise HTTPClientError(
                "err.http.unreachable", label=label, attempts=used, cause=str(exc)
            ) from exc

        if response.ok:
            if key is not None:
                credentials.release(key)
            return response

        detail = response.text.strip()[-1000:] or response.reason
        if response.status_code == RATE_LIMITED_STATUS:
            rate_limited += 1
            if key is not None:
                # The key's own strike count drives its backoff, so one struggling
                # key does not lengthen the cooldown of the others.
                delay = _rate_limit_delay(response, credentials.strikes(key))
                credentials.penalise(key, delay)
                if rate_limited < rate_limit_budget:
                    logger.warning(
                        "%s rate limited on key %s (%s/%s), cooling it %.1fs and rotating",
                        label, credentials.label(key), rate_limited, rate_limit_budget, delay,
                    )
                    continue
                raise _pool_exhausted(label, credentials, rate_limited, detail)
            if rate_limited < rate_limit_budget:
                delay = _rate_limit_delay(response, rate_limited - 1)
                logger.warning(
                    "%s rate limited (attempt %s/%s), waiting %.1fs",
                    label, rate_limited, rate_limit_budget, delay,
                )
                _wait(delay)
                continue
            logger.warning("%s stayed rate limited: %s", label, detail)
            raise HTTPClientError(
                "err.http.rateLimited",
                status_code=response.status_code,
                body=detail,
                label=label,
                attempts=rate_limit_budget,
                detail=detail,
            )

        if key is not None and response.status_code in KEY_REJECTED_STATUS:
            rejected += 1
            credentials.reject(key, KEY_REJECTED_COOLDOWN_SECONDS)
            if rejected < len(credentials):
                logger.warning(
                    "%s rejected key %s (HTTP %s), dropping it from rotation: %s",
                    label, credentials.label(key), response.status_code, detail,
                )
                continue
            logger.warning("%s rejected every API key it was given", label)
            raise HTTPClientError(
                "err.http.keysRejected",
                status_code=response.status_code,
                body=detail,
                label=label,
                keys=len(credentials),
                status=response.status_code,
                detail=detail,
            )

        if response.status_code in RETRYABLE_STATUS and used < attempts:
            logger.warning(
                "%s returned HTTP %s (attempt %s/%s), retrying",
                label, response.status_code, used, attempts,
            )
            _sleep_for_attempt(used - 1)
            used += 1
            continue

        logger.warning("%s returned HTTP %s: %s", label, response.status_code, detail)
        raise HTTPClientError(
            "err.http.status",
            status_code=response.status_code,
            body=detail,
            label=label,
            status=response.status_code,
            detail=detail,
        )


def json_body(response, label: str) -> Any:
    """Decode a provider response, turning malformed bodies into one error type."""

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("%s returned a non-JSON body", label)
        raise HTTPClientError("err.http.notJson", label=label) from exc
    return payload
