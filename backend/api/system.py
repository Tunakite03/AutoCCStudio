"""Health and capability reporting."""

from __future__ import annotations

import importlib.util

from fastapi import APIRouter

from ..ai import (
    TRANSLATION_MOCK,
    TRANSLATION_TRANSFORMERS,
    resolve_translation_provider,
)
from ..config import settings
from ..media import find_ffmpeg

router = APIRouter(prefix="/api", tags=["system"])

TRANSCRIPTION_MODELS = {
    "faster_whisper": [
        {"value": "tiny", "label": "tiny — nhanh, nhẹ"},
        {"value": "base", "label": "base — cân bằng"},
        {"value": "small", "label": "small — khuyến nghị CPU"},
        {"value": "medium", "label": "medium — chính xác hơn"},
        {"value": "large-v3", "label": "large-v3 — tốt nhất, nặng"},
    ],
    "deepgram": [
        {"value": "nova-3", "label": "Nova-3 — khuyến nghị, nhiều người"},
        {"value": "nova-2", "label": "Nova-2 — tương thích rộng"},
        {"value": "nova-2-meeting", "label": "Nova-2 Meeting — họp, English"},
        {"value": "nova-2-video", "label": "Nova-2 Video — video, English"},
    ],
}


@router.get("/health")
def health() -> dict:
    return {"ok": True, "app": "AutoCC", "version": "0.1.0"}


def _translation_configured(provider: str) -> bool:
    """Mirrors what the translator will actually do with this configuration."""

    if provider == TRANSLATION_MOCK:
        return True
    if provider == TRANSLATION_TRANSFORMERS:
        return bool(settings.translation_model) and (
            importlib.util.find_spec("transformers") is not None
        )
    return bool(settings.llm_base_url)


@router.get("/capabilities")
def capabilities() -> dict:
    """What this install can actually do — never the credentials that enable it."""

    translation_provider = resolve_translation_provider(settings.translation_provider)
    return {
        "transcription_provider": settings.transcription_provider.lower(),
        "whisper_model": settings.whisper_model,
        "whisper_available": importlib.util.find_spec("faster_whisper") is not None,
        "deepgram_model": settings.deepgram_model,
        "deepgram_configured": bool(settings.deepgram_api_key.strip()),
        "transcription_models": TRANSCRIPTION_MODELS,
        "translation_provider": translation_provider,
        "translation_model": settings.translation_model,
        "translation_configured": _translation_configured(translation_provider),
        "llm_model": settings.llm_model,
        "speaker_analysis_configured": bool(
            settings.llm_base_url.strip()
            and (settings.speaker_analysis_model or settings.llm_model).strip()
        ),
        "speaker_analysis_model": (
            settings.speaker_analysis_model or settings.llm_model
        ),
        "ffmpeg": find_ffmpeg() is not None,
        "max_concurrent_jobs": settings.max_concurrent_jobs,
    }
