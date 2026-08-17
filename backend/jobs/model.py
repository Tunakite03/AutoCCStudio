"""The shape of a job and the projection the API is allowed to expose.

Jobs stay plain dicts: they are persisted as JSON, patched field by field by
long-running workers, and read by templates that predate any schema. What this
module pins down is the vocabulary — which statuses exist, which fields are
public, and what a progress report looks like.
"""

from __future__ import annotations

import re
import uuid

from ..subtitles import strip_speaker_labels

# uuid4().hex — anything else in a path parameter is a probe, not a job.
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_READY = "ready"
STATUS_ERROR = "error"
# Its own status, not an error: a stopped run keeps everything it had already
# written, and the UI must not offer to "retry" it as if something broke.
STATUS_CANCELLED = "cancelled"

KIND_TRANSCRIPTION = "transcription"
KIND_SUBTITLE_IMPORT = "subtitle_import"

PHASE_QUEUED = "queued"
PHASE_TRANSCRIBING = "transcribing"
PHASE_ANALYZING = "analyzing"
PHASE_TRANSLATING = "translating"


def is_valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.fullmatch(job_id))


def new_job_id() -> str:
    return uuid.uuid4().hex


def make_progress(
    phase: str,
    *,
    current: int = 0,
    total: int | None = None,
    message: str = "",
) -> dict:
    """A progress report for a phase.

    `total` is None while a phase cannot know its own size — Deepgram is one
    opaque request, so it reports a phase and a message but no ratio.
    """

    ratio = None
    if total:
        ratio = round(min(max(current / total, 0.0), 1.0), 4)
    return {
        "phase": phase,
        "current": current,
        "total": total,
        "ratio": ratio,
        "message": message,
    }


def new_job(
    kind: str,
    video_name: str | None = None,
    subtitle_name: str | None = None,
) -> dict:
    """Build a job. Nothing touches disk until the store persists it."""

    return {
        "id": new_job_id(),
        "revision": 0,
        "kind": kind,
        "status": STATUS_PROCESSING if kind == KIND_TRANSCRIPTION else STATUS_READY,
        "error": None,
        "progress": None,
        # Raised by a request, lowered by the worker that honours it. A thread
        # cannot be killed from outside, so stopping is always cooperative.
        "cancel_requested": False,
        "video_name": video_name,
        "subtitle_name": subtitle_name,
        "video_path": None,
        "subtitle_path": None,
        "source_language": None,
        "transcription_provider": None,
        "transcription_model": None,
        "speaker_analysis_requested": False,
        "speaker_analysis_status": None,
        "speaker_analysis_error": None,
        "speaker_analysis_report": None,
        "target_language": None,
        "translation_provider": None,
        "translation_model": None,
        "detected_language": None,
        "cues": [],
    }


def clean_cues(cues: list[dict]) -> list[dict]:
    """Drop legacy [S1]/[1] prefixes without disturbing dialogue line breaks."""

    cleaned_cues = []
    for cue in cues:
        cleaned = dict(cue)
        cleaned["text"] = strip_speaker_labels(str(cue.get("text", "")))
        cleaned["translation"] = strip_speaker_labels(str(cue.get("translation", "")))
        cleaned_cues.append(cleaned)
    return cleaned_cues


def public_job(job: dict) -> dict:
    """The client-facing projection.

    Built field by field on purpose: internal bookkeeping (filesystem paths, the
    deletion tombstone) must never reach a response by simply being added to the
    job dict somewhere.
    """

    return {
        "id": job["id"],
        "revision": int(job.get("revision", 0)),
        "kind": job["kind"],
        "status": job["status"],
        "error": job.get("error"),
        "progress": job.get("progress"),
        # Public because it is the whole "Đang dừng…" state: the request landed,
        # the worker has not reached its next checkpoint yet.
        "cancel_requested": bool(job.get("cancel_requested")),
        "video_name": job.get("video_name"),
        "subtitle_name": job.get("subtitle_name"),
        "video_available": bool(job.get("video_path")),
        "detected_language": job.get("detected_language"),
        "source_language": job.get("source_language"),
        "transcription_provider": job.get("transcription_provider"),
        "transcription_model": job.get("transcription_model"),
        "speaker_analysis_requested": bool(job.get("speaker_analysis_requested")),
        "speaker_analysis_status": job.get("speaker_analysis_status"),
        "speaker_analysis_error": job.get("speaker_analysis_error"),
        "speaker_analysis_report": job.get("speaker_analysis_report"),
        "target_language": job.get("target_language"),
        "translation_provider": job.get("translation_provider"),
        "translation_model": job.get("translation_model"),
        "translation_style": job.get("translation_style"),
        "translation_style_notes": job.get("translation_style_notes"),
        "cues": clean_cues(job.get("cues", [])),
    }
