"""AI adapters used by AutoCC.

Transcription is local through faster-whisper. Translation is deliberately
implemented against the common OpenAI-compatible chat-completions shape so it
can target Ollama, LM Studio, or a hosted endpoint without changing the app.

Split by concern so each piece can be read and changed on its own:
`transcription` (faster-whisper + Deepgram), `llm` (the chat-completions and
local-transformers model adapters), `diarization` (speaker-turn analysis) and
`translation` (batching, glossary continuity, dub shortening). This package
re-exports the public surface so callers keep importing from `backend.ai`
unchanged.
"""

from __future__ import annotations

from ..core.config import settings
from .diarization import analyze_dialogue_turns
from .shared import AIProviderError, AIResponseFormatError
from .transcription import transcribe_video, transcribe_video_deepgram
from .translation import (
    TRANSLATION_MOCK,
    TRANSLATION_OPENAI_COMPATIBLE,
    TRANSLATION_TRANSFORMERS,
    get_translation_settings,
    resolve_translation_provider,
    shorten_for_dubbing,
    translate_cues,
)

__all__ = [
    "AIProviderError",
    "AIResponseFormatError",
    "settings",
    "TRANSLATION_MOCK",
    "TRANSLATION_OPENAI_COMPATIBLE",
    "TRANSLATION_TRANSFORMERS",
    "analyze_dialogue_turns",
    "get_translation_settings",
    "resolve_translation_provider",
    "shorten_for_dubbing",
    "transcribe_video",
    "transcribe_video_deepgram",
    "translate_cues",
]
