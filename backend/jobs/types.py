"""Static job-state vocabulary that stays JSON/dict compatible at runtime."""

from __future__ import annotations

from typing import Any, Required, TypedDict


class CueRecord(TypedDict, total=False):
    id: Required[int]
    start: Required[float]
    end: Required[float]
    text: Required[str]
    translation: Required[str]
    speaker: int | None
    speaker_turns: list[dict[str, Any]]
    speaker_analysis_source: str
    speaker_analysis_failure: str


class ProgressRecord(TypedDict, total=False):
    phase: Required[str]
    current: Required[int]
    total: int | None
    ratio: float | None
    message: dict[str, Any] | None


class JobRecord(TypedDict, total=False):
    """Persisted job fields.

    ``total=False`` is intentional: old job.json files predate newer features
    such as dubbing and speaker analysis. Required identity fields document the
    minimum record while optional fields let those files load unchanged.
    """

    id: Required[str]
    revision: Required[int]
    kind: Required[str]
    status: Required[str]
    error: dict[str, Any] | None
    progress: ProgressRecord | None
    cancel_requested: bool
    deleted: bool
    video_name: str | None
    subtitle_name: str | None
    video_path: str | None
    subtitle_path: str | None
    source_language: str | None
    detected_language: str | None
    transcription_provider: str | None
    transcription_model: str | None
    speaker_analysis_requested: bool
    speaker_analysis_status: str | None
    speaker_analysis_error: dict[str, Any] | None
    speaker_analysis_report: dict[str, Any] | None
    target_language: str | None
    translation_provider: str | None
    translation_model: str | None
    translation_style: str | None
    translation_style_notes: str | None
    translation_style_ref: str | None
    dubbing_status: str | None
    dubbing_error: dict[str, Any] | None
    dubbing_report: dict[str, Any] | None
    dubbing_provider: str | None
    dubbing_voice: str | None
    dubbing_fingerprint: str | None
    dub_audio_path: str | None
    cues: Required[list[CueRecord]]
