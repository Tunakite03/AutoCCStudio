"""One HTTP path for every outbound provider call.

Deepgram used `requests` while the LLM adapter used `urllib`, so timeouts,
retries and error reporting had to be written twice and only ever got fixed on
one side. Everything goes through `post` here instead.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator

from .config import get_logger, settings

logger = get_logger("http")

# Transient by nature: the provider is rate-limiting us or briefly unavailable.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 1.5


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
    """

    requests = _requests()
    attempts = max(0, retries if retries is not None else settings.http_retries) + 1
    last_error: Exception | None = None

    for attempt in range(attempts):
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
            last_error = exc
            if attempt + 1 < attempts:
                logger.warning(
                    "%s unreachable (attempt %s/%s): %s", label, attempt + 1, attempts, exc
                )
                _sleep_for_attempt(attempt)
                continue
            logger.warning("%s unreachable: %s", label, exc)
            raise HTTPClientError(f"Không kết nối được {label}: {exc}") from exc

        if response.ok:
            return response

        detail = response.text.strip()[-1000:] or response.reason
        if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
            logger.warning(
                "%s returned HTTP %s (attempt %s/%s), retrying",
                label, response.status_code, attempt + 1, attempts,
            )
            _sleep_for_attempt(attempt)
            continue

        logger.warning("%s returned HTTP %s: %s", label, response.status_code, detail)
        raise HTTPClientError(
            f"{label} trả về HTTP {response.status_code}: {detail}",
            status_code=response.status_code,
            body=detail,
        )

    # Only reachable if the loop exhausted its retries on connection errors.
    raise HTTPClientError(f"Không kết nối được {label}: {last_error}")


def json_body(response, label: str) -> Any:
    """Decode a provider response, turning malformed bodies into one error type."""

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("%s returned a non-JSON body", label)
        raise HTTPClientError(f"{label} trả về dữ liệu không phải JSON") from exc
    return payload
