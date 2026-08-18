"""Speech synthesis for the dubbing pass.

Deliberately the same shape as the translation adapters in `ai.py`: one thin
function per provider behind a single `synthesize`, so swapping the free Edge
endpoint for a paid one later is a new branch here and nothing anywhere else.

`mock` is not a stub left in by accident. Fitting a line to its cue, assembling
a track and reporting on the result are all worth testing, and none of them
should need a network, an API key, or a voice that might be renamed upstream.
"""

from __future__ import annotations

import asyncio
import math
import struct
import wave
from pathlib import Path

from ..core.config import get_logger, settings
from ..core.messages import CodedError, Message
from ..infrastructure.providers.tts import (
    PROVIDERS,
    TTS_EDGE,
    TTS_MOCK,
    VOICES,
    get_tts_provider,
    resolve_tts_provider_name,
)

__all__ = [
    "PROVIDERS",
    "TTS_EDGE",
    "TTS_MOCK",
    "TTSProviderError",
    "VOICE_NAME_LIMIT",
    "VOICES",
    "default_voice",
    "is_configured",
    "list_voices",
    "resolve_tts_provider",
    "synthesize",
]

logger = get_logger("tts")

OP_DUB = Message("op.dub")


class TTSProviderError(CodedError):
    """A voice could not be synthesised: provider missing, refused, or silent."""


# Long enough for a voice id, short enough that nothing else fits.
VOICE_NAME_LIMIT = 64

# What `mock` pretends a voice sounds like: a plausible speaking rate, so a
# fitted line in a test is fitted for the same reason a real one would be.
MOCK_SECONDS_PER_CHAR = 0.062
MOCK_SAMPLE_RATE = 24000
MOCK_TONE_HZ = 180.0


def resolve_tts_provider(name: str) -> str:
    """Normalise a provider name, falling back to the configured default."""

    raw = (name or "").strip()
    resolved = resolve_tts_provider_name(raw, settings.tts_provider)
    if resolved is None:
        provider_name = raw.lower() or settings.tts_provider.strip().lower()
        raise TTSProviderError("err.tts.badProvider", provider=provider_name)
    return resolved


def _voice_option(value: str, name: str, hint: str) -> dict:
    """One picker entry, in the shape `api/system.py` uses for model options.

    The name is a proper noun and travels as data; the hint reads as a sentence,
    so it travels as a code the client renders in its own language.
    """

    return {"value": value, "name": name, "hint": Message(hint).as_dict()}


def list_voices(provider: str | None = None) -> list[dict]:
    """The picker's options for a provider, with the configured voice present."""

    try:
        resolved = resolve_tts_provider(provider or "")
    except TTSProviderError:
        return []
    options = [_voice_option(**voice) for voice in VOICES.get(resolved, [])]
    configured = settings.tts_voice.strip()
    if configured and not any(option["value"] == configured for option in options):
        options.insert(0, _voice_option(configured, configured, "voice.fromEnv"))
    return options


def default_voice(provider: str | None = None) -> str:
    """The voice a run uses when the request names none."""

    configured = settings.tts_voice.strip()
    options = list_voices(provider)
    if configured and any(option["value"] == configured for option in options):
        return configured
    return options[0]["value"] if options else configured


def is_configured(provider: str | None = None) -> bool:
    """Whether this install can actually synthesise speech right now."""

    try:
        resolved = resolve_tts_provider(provider or "")
    except TTSProviderError:
        return False
    selected = get_tts_provider(resolved)
    return selected is not None and selected.is_configured()


def synthesize(
    text: str,
    voice: str,
    destination_stem: Path,
    *,
    provider: str | None = None,
    rate: str = "+0%",
) -> Path:
    """Render `text` to an audio file next to `destination_stem`.

    The provider picks the container — Edge returns mp3, `mock` writes wav — so
    the written path is returned rather than assumed. Everything downstream
    decodes to PCM anyway, which is why the format is allowed to differ.
    """

    resolved = resolve_tts_provider(provider or "")
    spoken = " ".join(str(text).split())
    if not spoken:
        raise TTSProviderError("err.tts.emptyText")
    name = (voice or "").strip() or default_voice(resolved)
    if len(name) > VOICE_NAME_LIMIT:
        raise TTSProviderError("err.tts.badVoice")

    destination_stem.parent.mkdir(parents=True, exist_ok=True)
    selected = get_tts_provider(resolved)
    if selected is None:  # resolve_tts_provider already validates this registry lookup.
        raise TTSProviderError("err.tts.badProvider", provider=resolved)
    return selected.synthesize(spoken, name, destination_stem, rate)


def _synthesize_edge(text: str, voice: str, destination: Path, rate: str) -> Path:
    try:
        import edge_tts
    except ImportError as exc:
        raise TTSProviderError("err.tts.edgeMissing") from exc

    async def render() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await asyncio.wait_for(
            communicate.save(str(destination)), timeout=settings.tts_timeout_seconds
        )

    try:
        # A job worker is a plain thread with no event loop of its own, so this
        # owns one for the length of the call rather than borrowing the app's.
        asyncio.run(render())
    except TimeoutError as exc:
        destination.unlink(missing_ok=True)
        raise TTSProviderError(
            "err.tts.timeout", seconds=settings.tts_timeout_seconds
        ) from exc
    except TTSProviderError:
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        logger.warning("edge-tts failed for voice %s: %s", voice, exc)
        raise TTSProviderError("err.tts.requestFailed", cause=str(exc)[:300]) from exc

    if not destination.exists() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise TTSProviderError("err.tts.emptyAudio")
    return destination


def _synthesize_mock(text: str, destination: Path) -> Path:
    """A tone whose length tracks the text, so timing logic has something real."""

    seconds = max(0.2, len(text) * MOCK_SECONDS_PER_CHAR)
    frames = int(seconds * MOCK_SAMPLE_RATE)
    samples = bytearray()
    for index in range(frames):
        value = int(9000 * math.sin(2 * math.pi * MOCK_TONE_HZ * index / MOCK_SAMPLE_RATE))
        samples += struct.pack("<h", value)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(MOCK_SAMPLE_RATE)
        handle.writeframes(bytes(samples))
    return destination
