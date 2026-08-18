"""Error types, op tags and progress-reporting plumbing shared by every AI adapter."""

from __future__ import annotations

from typing import Callable

from ..config import get_logger
from ..messages import CodedError, Message


class AIProviderError(CodedError):
    """Raised when an AI provider is unavailable or returns invalid output."""


class AIResponseFormatError(AIProviderError):
    """Raised when an AI provider responds but violates the requested JSON shape."""


logger = get_logger("ai")

# Which run a provider error belongs to, for the message that reports it.
OP_TRANSCRIBE = Message("op.transcribe")
OP_TRANSLATE = Message("op.translate")
OP_SPEAKER_ANALYSIS = Message("op.speakerAnalysis")
OP_DUB_SHORTEN = Message("op.dubShorten")

# Reports (done, total_or_None, message) as a phase advances.
ProgressCallback = Callable[[int, int | None, Message], None]


def _report(
    on_progress: ProgressCallback | None,
    current: int,
    total: int | None,
    message: Message,
) -> None:
    if on_progress is not None:
        on_progress(current, total, message)
