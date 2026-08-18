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
from ..messages import Message
from ..translation_style import style_options
from ..tts import default_voice, is_configured as tts_is_configured, list_voices, resolve_tts_provider

router = APIRouter(prefix="/api", tags=["system"])


def _option(value: str, hint: str | None = None, *, name: str | None = None) -> dict:
    """One picker entry: a value, the name it is known by, and a hint code.

    The name is a proper noun — a model is called Nova-3 in every language — so
    it is data. The hint is the part that reads as a sentence, so it travels as a
    code the client resolves.
    """

    option = {"value": value, "name": name or value}
    if hint:
        option["hint"] = Message(hint).as_dict()
    return option


TRANSCRIPTION_MODELS = {
    "faster_whisper": [
        _option("tiny", "model.whisper.tiny"),
        _option("base", "model.whisper.base"),
        _option("small", "model.whisper.small"),
        _option("medium", "model.whisper.medium"),
        _option("large-v3", "model.whisper.largeV3"),
    ],
    "deepgram": [
        _option("nova-3", "model.deepgram.nova3", name="Nova-3"),
        _option("nova-2", "model.deepgram.nova2", name="Nova-2"),
        _option("nova-2-meeting", "model.deepgram.nova2Meeting", name="Nova-2 Meeting"),
        _option("nova-2-video", "model.deepgram.nova2Video", name="Nova-2 Video"),
    ],
}


# Every OpenAI-compatible endpoint speaks the same protocol but serves a
# different catalogue, and a model name from the wrong catalogue is a 400 at
# translate time rather than anything the UI can warn about. So the list is
# keyed by endpoint host and the picker only ever offers what LLM_BASE_URL can
# actually run.
LLM_MODELS_BY_HOST = {
    "mistral.ai": [
        _option("mistral-large-latest", "model.llm.topQuality", name="Mistral Large"),
        _option("mistral-medium-latest", "model.llm.balanced", name="Mistral Medium"),
        _option("mistral-small-latest", "model.llm.fastNatural", name="Mistral Small"),
        _option("open-mistral-nemo", "model.llm.lightCheap", name="Mistral Nemo 12B"),
    ],
    "openai.com": [
        _option("gpt-4o", "model.llm.smartProse", name="GPT-4o"),
        _option("gpt-4o-mini", "model.llm.fastAccurate", name="GPT-4o Mini"),
    ],
    "deepseek.com": [
        _option("deepseek-chat", "model.llm.detailedNatural", name="DeepSeek V3"),
    ],
}

# Ollama and LM Studio serve whatever has been pulled locally, so this is a
# starting point rather than a catalogue.
LOCAL_LLM_MODELS = [
    _option("qwen2.5:7b", "model.llm.ollamaPick", name="Qwen 2.5 7B"),
    _option("qwen2.5:14b", "model.llm.goodNuance", name="Qwen 2.5 14B"),
    _option("qwen2.5:32b", "model.llm.excellent", name="Qwen 2.5 32B"),
    _option("llama3.1:8b", "model.llm.popular", name="Llama 3.1 8B"),
]

TRANSFORMERS_MODELS = [
    _option("Helsinki-NLP/opus-mt-en-vi", name="Opus-MT En-Vi (Helsinki-NLP)"),
    _option("facebook/nllb-200-distilled-600M", name="NLLB-200 Distilled 600M (Meta)"),
    _option("vinai/vinai-translate-en2vi", name="VinAI Translate En-Vi"),
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
        options.insert(0, _option(configured, "model.fromEnv"))
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
    ffmpeg_available = find_ffmpeg() is not None
    try:
        tts_provider = resolve_tts_provider("")
    except Exception:
        # A typo in TTS_PROVIDER must not take the whole capability probe down
        # with it — every other engine on this page still works.
        tts_provider = ""
    tts_configured = bool(tts_provider) and tts_is_configured(tts_provider)
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
        "ffmpeg": ffmpeg_available,
        "max_concurrent_jobs": settings.max_concurrent_jobs,
        "tts_provider": tts_provider,
        "tts_voice": default_voice(tts_provider),
        "tts_voices": list_voices(tts_provider),
        "tts_configured": tts_configured,
        # Dubbing needs a voice *and* something to decode it with: the mix, the
        # export and every duration measurement in between go through ffmpeg.
        "dubbing_configured": tts_configured and ffmpeg_available,
        "dub_original_gain": settings.dub_original_gain,
        "dub_shorten_with_llm": settings.dub_shorten_with_llm and llm_configured,
    }
