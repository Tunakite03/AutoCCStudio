"""Text-to-speech provider contracts and the built-in adapter registry."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

TTS_EDGE = "edge"
TTS_MOCK = "mock"
PROVIDERS = (TTS_EDGE, TTS_MOCK)

VOICES: dict[str, list[dict[str, str]]] = {
    TTS_EDGE: [
        {"value": "vi-VN-HoaiMyNeural", "name": "Hoài My", "hint": "voice.vi.female"},
        {"value": "vi-VN-NamMinhNeural", "name": "Nam Minh", "hint": "voice.vi.male"},
        {"value": "en-US-AriaNeural", "name": "Aria", "hint": "voice.en.female"},
        {"value": "en-US-GuyNeural", "name": "Guy", "hint": "voice.en.male"},
        {"value": "ja-JP-NanamiNeural", "name": "Nanami", "hint": "voice.ja.female"},
        {"value": "ko-KR-SunHiNeural", "name": "Sun-Hi", "hint": "voice.ko.female"},
        {"value": "zh-CN-XiaoxiaoNeural", "name": "Xiaoxiao", "hint": "voice.zh.female"},
    ],
    TTS_MOCK: [{"value": "mock", "name": "Mock", "hint": "voice.mock"}],
}


class TTSProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    def synthesize(
        self,
        text: str,
        voice: str,
        destination_stem: Path,
        rate: str,
    ) -> Path: ...


@dataclass(frozen=True)
class EdgeTTSProvider:
    name: str = TTS_EDGE

    def is_configured(self) -> bool:
        return importlib.util.find_spec("edge_tts") is not None

    def synthesize(
        self,
        text: str,
        voice: str,
        destination_stem: Path,
        rate: str,
    ) -> Path:
        from ...ai import tts as implementation

        return implementation._synthesize_edge(
            text,
            voice,
            destination_stem.with_suffix(".mp3"),
            rate,
        )


@dataclass(frozen=True)
class MockTTSProvider:
    name: str = TTS_MOCK

    def is_configured(self) -> bool:
        return True

    def synthesize(
        self,
        text: str,
        voice: str,
        destination_stem: Path,
        rate: str,
    ) -> Path:
        del voice, rate
        from ...ai import tts as implementation

        return implementation._synthesize_mock(text, destination_stem.with_suffix(".wav"))


_PROVIDERS: dict[str, TTSProvider] = {
    TTS_EDGE: EdgeTTSProvider(),
    TTS_MOCK: MockTTSProvider(),
}
_ALIASES = {"edge-tts": TTS_EDGE, "edge_tts": TTS_EDGE, "microsoft": TTS_EDGE}


def resolve_tts_provider_name(name: str, default: str) -> str | None:
    normalized = name.strip().lower() or default.strip().lower()
    resolved = _ALIASES.get(normalized, normalized)
    return resolved if resolved in _PROVIDERS else None


def get_tts_provider(name: str) -> TTSProvider | None:
    return _PROVIDERS.get(name)
