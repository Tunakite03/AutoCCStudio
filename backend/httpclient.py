"""One HTTP path for every outbound provider call.

Deepgram used `requests` while the LLM adapter used `urllib`, so timeouts,
retries and error reporting had to be written twice and only ever got fixed on
one side. Everything goes through `post` here instead.
"""

from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from .config import get_logger, settings

logger = get_logger("http")

# Transient by nature: the provider is rate-limiting us or briefly unavailable.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RATE_LIMITED_STATUS = 429
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


class HTTPClientError(RuntimeError):
    """A provider call failed after exhausting retries."""

    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dependency
        raise HTTPClientError(
            "Chưa cài requests. Chạy: pip install -r requirements.txt"
        ) from exc
    return requests


def _sleep_for_attempt(attempt: int) -> None:
    time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))


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
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    return None


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
):
    """POST with bounded retries on transient failures.

    `body_factory` exists for streamed file uploads: a retry cannot reuse a
    consumed file handle, so the caller supplies a fresh one per attempt.

    A 429 is counted separately and waits as long as the provider asks. Sharing
    one budget with connection errors meant a rate limit was given a couple of
    seconds to clear and then reported as a hard failure, which for a per-minute
    quota it never was.
    """

    requests = _requests()
    attempts = max(0, retries if retries is not None else settings.http_retries) + 1
    rate_limit_attempts = max(0, settings.http_rate_limit_retries) + 1
    used = 1
    rate_limited = 0

    while True:
        payload = body_factory() if body_factory is not None else data
        try:
            response = requests.post(
                url,
                headers=headers,
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
                f"Không kết nối được {label}: {exc} (sau {used} lần thử)"
            ) from exc

        if response.ok:
            return response

        detail = response.text.strip()[-1000:] or response.reason
        if response.status_code == RATE_LIMITED_STATUS:
            if rate_limited + 1 < rate_limit_attempts:
                delay = _rate_limit_delay(response, rate_limited)
                rate_limited += 1
                logger.warning(
                    "%s rate limited (attempt %s/%s), waiting %.1fs",
                    label, rate_limited, rate_limit_attempts, delay,
                )
                time.sleep(delay)
                continue
            logger.warning("%s stayed rate limited: %s", label, detail)
            raise HTTPClientError(
                f"{label} vẫn giới hạn tốc độ (HTTP 429) sau {rate_limit_attempts} "
                f"lần thử: {detail}",
                status_code=response.status_code,
                body=detail,
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
            f"{label} trả về HTTP {response.status_code}: {detail}",
            status_code=response.status_code,
            body=detail,
        )


def json_body(response, label: str) -> Any:
    """Decode a provider response, turning malformed bodies into one error type."""

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("%s returned a non-JSON body", label)
        raise HTTPClientError(f"{label} trả về dữ liệu không phải JSON") from exc
    return payload
