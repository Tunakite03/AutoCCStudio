"""Provider adapters used by the application-facing AI and TTS facades.

The implementations deliberately import their heavy backends lazily. Importing
the FastAPI app must not load Whisper, transformers, or edge-tts just to expose
the capability endpoint.
"""

from .transcription import get_transcription_provider
from .translation import get_translation_provider, resolve_translation_provider_name
from .tts import get_tts_provider, resolve_tts_provider_name

__all__ = [
    "get_transcription_provider",
    "get_translation_provider",
    "get_tts_provider",
    "resolve_translation_provider_name",
    "resolve_tts_provider_name",
]
