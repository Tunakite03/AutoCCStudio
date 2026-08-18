"""Transcription provider contracts and the built-in adapter registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ProgressCallback = Callable[[int, int | None, Any], None]
TranscriptionResult = tuple[list[dict], str | None]


class TranscriptionProvider(Protocol):
    name: str

    def transcribe(
        self,
        video_path: Path,
        *,
        model: str | None,
        language: str | None,
        on_progress: ProgressCallback | None,
    ) -> TranscriptionResult: ...


@dataclass(frozen=True)
class FasterWhisperProvider:
    name: str = "faster_whisper"

    def transcribe(
        self,
        video_path: Path,
        *,
        model: str | None,
        language: str | None,
        on_progress: ProgressCallback | None,
    ) -> TranscriptionResult:
        # Lazy to keep the registry independent from backend.ai's import cycle.
        from ...ai import transcription as implementation

        return implementation._transcribe_faster_whisper(
            video_path,
            model_size=model,
            language=language,
            on_progress=on_progress,
        )


@dataclass(frozen=True)
class DeepgramProvider:
    name: str = "deepgram"

    def transcribe(
        self,
        video_path: Path,
        *,
        model: str | None,
        language: str | None,
        on_progress: ProgressCallback | None,
    ) -> TranscriptionResult:
        from ...ai import transcription as implementation

        return implementation.transcribe_video_deepgram(
            video_path,
            language=language,
            model=model,
            on_progress=on_progress,
        )


_PROVIDERS: dict[str, TranscriptionProvider] = {
    "faster_whisper": FasterWhisperProvider(),
    "deepgram": DeepgramProvider(),
}
_ALIASES = {"whisper": "faster_whisper"}


def get_transcription_provider(name: str) -> TranscriptionProvider | None:
    normalized = name.strip().lower()
    return _PROVIDERS.get(_ALIASES.get(normalized, normalized))
