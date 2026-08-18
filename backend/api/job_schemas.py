"""HTTP request schemas for job routes."""

from __future__ import annotations

from pydantic import BaseModel

from ..domain.translation.style import STYLE_AUTO


class CueModel(BaseModel):
    id: int = 0
    start: float
    end: float
    text: str = ""
    translation: str = ""
    speaker: int | None = None


class CuesPayload(BaseModel):
    cues: list[CueModel]


class TranslatePayload(BaseModel):
    target_language: str
    style: str = STYLE_AUTO
    style_notes: str = ""
    style_ref: str = ""
    provider: str = ""
    model: str = ""
    from_cue: int = 0


class DubPayload(BaseModel):
    voice: str = ""
    provider: str = ""
    prefer: str = ""
    original_gain: float | None = None
    shorten: bool | None = None
