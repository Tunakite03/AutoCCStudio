"""Health and capability reporting."""

from __future__ import annotations

import importlib.util
from urllib.parse import urlparse

from fastapi import APIRouter

from ..ai import (
    TRANSLATION_MOCK,
    TRANSLATION_TRANSFORMERS,
    resolve_translation_provider,
)
from ..config import settings
from ..media import find_ffmpeg
from ..translation_style import style_options

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


# Every OpenAI-compatible endpoint speaks the same protocol but serves a
# different catalogue, and a model name from the wrong catalogue is a 400 at
# translate time rather than anything the UI can warn about. So the list is
# keyed by endpoint host and the picker only ever offers what LLM_BASE_URL can
# actually run.
LLM_MODELS_BY_HOST = {
    "mistral.ai": [
        {"value": "mistral-large-latest", "label": "Mistral Large — chất lượng cao"},
        {"value": "mistral-medium-latest", "label": "Mistral Medium — cân bằng"},
        {"value": "mistral-small-latest", "label": "Mistral Small — nhanh, tự nhiên"},
        {"value": "open-mistral-nemo", "label": "Mistral Nemo 12B — nhẹ, rẻ"},
    ],
    "openai.com": [
        {"value": "gpt-4o", "label": "GPT-4o — thông minh, văn phong mượt"},
        {"value": "gpt-4o-mini", "label": "GPT-4o Mini — nhanh, chuẩn xác"},
    ],
    "deepseek.com": [
        {"value": "deepseek-chat", "label": "DeepSeek V3 — chi tiết, tự nhiên"},
    ],
}

# Ollama and LM Studio serve whatever has been pulled locally, so this is a
# starting point rather than a catalogue.
LOCAL_LLM_MODELS = [
    {"value": "qwen2.5:7b", "label": "Qwen 2.5 7B — khuyên dùng Ollama"},
    {"value": "qwen2.5:14b", "label": "Qwen 2.5 14B — dịch sắc thái tốt"},
    {"value": "qwen2.5:32b", "label": "Qwen 2.5 32B — dịch xuất sắc"},
    {"value": "llama3.1:8b", "label": "Llama 3.1 8B — phổ biến"},
]

TRANSFORMERS_MODELS = [
    {"value": "Helsinki-NLP/opus-mt-en-vi", "label": "Opus-MT En-Vi (Helsinki-NLP)"},
    {"value": "facebook/nllb-200-distilled-600M", "label": "NLLB-200 Distilled 600M (Meta)"},
    {"value": "vinai/vinai-translate-en2vi", "label": "VinAI Translate En-Vi"},
]


def _llm_endpoint_models(base_url: str) -> list[dict]:
    """The models the configured endpoint will accept, most capable first."""

    host = (urlparse(base_url.strip()).hostname or base_url.strip()).lower()
    for suffix, models in LLM_MODELS_BY_HOST.items():
        if host == suffix or host.endswith(f".{suffix}"):
            return list(models)
    return list(LOCAL_LLM_MODELS)


def _translation_models() -> dict[str, list[dict]]:
    """The picker's options, with the configured model guaranteed present."""

    options = _llm_endpoint_models(settings.llm_base_url)
    configured = settings.llm_model.strip()
    if configured and not any(item["value"] == configured for item in options):
        options.insert(0, {"value": configured, "label": f"{configured} — từ .env"})
    return {
        "openai_compatible": options,
        "transformers": list(TRANSFORMERS_MODELS),
    }


@router.get("/health")
def health() -> dict:
    return {"ok": True, "app": "AutoCC", "version": "0.1.0"}


def _translation_model(provider: str) -> str:
    """The model translation will actually call.

    TRANSLATION_MODEL only feeds the local transformers pipeline; a hosted
    provider translates with LLM_MODEL. Reporting the raw setting made the UI
    show a backend name where every other row shows a model.
    """

    if provider == TRANSLATION_MOCK:
        return TRANSLATION_MOCK
    if provider == TRANSLATION_TRANSFORMERS:
        return settings.translation_model
    return settings.llm_model


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
    transformers_available = importlib.util.find_spec("transformers") is not None
    llm_configured = bool(settings.llm_base_url.strip())
    return {
        "transcription_provider": settings.transcription_provider.lower(),
        "whisper_model": settings.whisper_model,
        "whisper_available": importlib.util.find_spec("faster_whisper") is not None,
        "deepgram_model": settings.deepgram_model,
        "deepgram_configured": bool(settings.deepgram_api_key.strip()),
        "transcription_models": TRANSCRIPTION_MODELS,
        "translation_provider": translation_provider,
        "translation_model": _translation_model(translation_provider),
        "translation_models": _translation_models(),
        "llm_endpoint": urlparse(settings.llm_base_url.strip()).hostname or "",
        "transformers_available": transformers_available,
        "llm_configured": llm_configured,
        # How many keys the rotation has to work with — the count only, never a
        # key. Without it there is no way to tell a mis-parsed LLM_API_KEY list
        # from a working one until a job dies on a rate limit.
        "llm_key_count": len(settings.llm_api_keys),
        "translation_configured": _translation_configured(translation_provider),
        "translation_styles": style_options(),
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
