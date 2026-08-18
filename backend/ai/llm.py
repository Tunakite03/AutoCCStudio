"""Model adapters: the OpenAI-compatible chat API, and the local transformers pipeline."""

from __future__ import annotations

import json
import threading
import time
from functools import lru_cache

from .. import httpclient
from ..apikeys import CredentialPool
from ..config import settings
from ..messages import Message
from .shared import AIProviderError, AIResponseFormatError, logger


def _completion_url() -> str:
    base = settings.llm_base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


_llm_pace_lock = threading.Lock()
_llm_next_call_at = 0.0

_llm_pool_lock = threading.Lock()
_llm_pool: tuple[tuple[str, ...], CredentialPool] | None = None


def _llm_credentials() -> CredentialPool | None:
    """The shared key pool for the configured LLM keys, or None if there are none.

    Shared, and rebuilt only when the configured keys themselves change: the
    cooldowns are the whole value here. A pool created per call would forget
    which key was just rate limited and hand it straight back — the first batch
    of the next cue would walk into the same 429.
    """

    keys = settings.llm_api_keys
    if not keys:
        return None
    global _llm_pool
    with _llm_pool_lock:
        if _llm_pool is None or _llm_pool[0] != keys:
            logger.info("LLM key pool: %s key(s) configured", len(keys))
            _llm_pool = (keys, CredentialPool(keys))
        return _llm_pool[1]


def _wait_for_llm_slot() -> None:
    """Keep LLM_MIN_INTERVAL_SECONDS between outbound LLM calls.

    Hosted providers meter requests per second and a translation is a long burst
    of small calls, so the cheapest rate limit is the one never triggered. The
    sleep happens under the lock on purpose: two jobs translating at once have
    to queue behind each other, not each keep their own pace.
    """

    interval = max(0.0, settings.llm_min_interval_seconds)
    if not interval:
        return
    global _llm_next_call_at
    with _llm_pace_lock:
        wait = _llm_next_call_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _llm_next_call_at = time.monotonic() + interval


def _llm_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    operation: Message,
    model: str | None = None,
) -> str:
    if not settings.llm_base_url.strip():
        raise AIProviderError("err.ai.llmBaseUrlMissing", operation=operation)
    selected_model = (model or settings.llm_model).strip()
    if not selected_model:
        raise AIProviderError("err.ai.llmModelMissing", operation=operation)

    _wait_for_llm_slot()
    try:
        response = httpclient.post(
            _completion_url(),
            headers={"Content-Type": "application/json"},
            # The pool authorises each attempt: which key goes out is decided
            # per request, because a retry after a 429 must not reuse the key
            # that was just told to slow down.
            credentials=_llm_credentials(),
            json_body={
                "model": selected_model,
                "temperature": temperature,
                "messages": messages,
            },
            timeout=(30, settings.llm_timeout_seconds),
            label="LLM",
        )
        body = httpclient.json_body(response, "LLM")
    except httpclient.HTTPClientError as exc:
        # The cause travels as a message of its own, so the client can say both
        # what was running and precisely how the provider failed.
        raise AIProviderError(
            "err.ai.llmRequestFailed", operation=operation, cause=exc.message
        ) from exc

    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("err.ai.llmMalformedResponse", operation=operation) from exc


def _resolve_transformers_device(value: str) -> int:
    normalized = value.strip().lower()
    if normalized == "auto":
        try:
            import torch

            return 0 if torch.cuda.is_available() else -1
        except ImportError:
            return -1
    if normalized == "cpu":
        return -1
    try:
        return int(normalized)
    except ValueError as exc:
        raise AIProviderError("err.ai.badTransformersDevice") from exc


@lru_cache(maxsize=2)
def _transformers_pipeline(model_name: str, device_name: str):
    if not model_name.strip():
        raise AIProviderError("err.ai.transformersModelRequired")
    try:
        from transformers import pipeline
    except ImportError as exc:
        raise AIProviderError("err.ai.transformersMissing") from exc
    try:
        return pipeline(
            "translation",
            model=model_name,
            device=_resolve_transformers_device(device_name),
        )
    except Exception as exc:
        raise AIProviderError(
            "err.ai.transformersLoadFailed", model=model_name, cause=str(exc)
        ) from exc


def _translate_batch_transformers(
    texts: list[str],
    target_language: str,
    *,
    model_name: str | None = None,
) -> list[str]:
    expected_language = settings.transformers_target_language.strip().casefold()
    if expected_language and target_language.strip().casefold() != expected_language:
        raise AIProviderError(
            "err.ai.transformersTargetMismatch",
            language=settings.transformers_target_language,
        )
    chosen_model = (model_name or settings.translation_model).strip()
    if not chosen_model:
        raise AIProviderError("err.ai.transformersModelRequired")
    translator = _transformers_pipeline(
        chosen_model,
        settings.transformers_device,
    )
    try:
        results = translator(texts, max_length=512)
    except Exception as exc:
        raise AIProviderError("err.ai.transformersFailed", cause=str(exc)) from exc
    if isinstance(results, dict):
        results = [results]
    try:
        return [str(item["translation_text"]).strip() for item in results]
    except (KeyError, TypeError) as exc:
        raise AIProviderError("err.ai.transformersBadOutput") from exc


def _extract_json_value(content: str, error_code: str = "err.ai.notJson"):
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    starts = [position for position in (cleaned.find("{"), cleaned.find("[")) if position >= 0]
    if starts:
        start = min(starts)
        closing = "}" if cleaned[start] == "{" else "]"
        end = cleaned.rfind(closing)
        if end > start:
            cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIResponseFormatError(error_code) from exc
