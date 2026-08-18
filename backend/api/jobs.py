"""FastAPI facade for job routes.

Route signatures stay here so the public API and existing monkeypatch points
remain stable. Use-case logic lives in focused lifecycle, operation, schema,
shared-helper, and SSE modules next to this facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from ..core.config import settings
from ..jobs.types import JobRecord
from . import job_lifecycle, job_operations
from .job_events import format_sse_job_event
from .job_events import stream_job_events as _stream_job_events
from .job_schemas import CueModel, CuesPayload, DubPayload, TranslatePayload
from .job_shared import (
    claim,
    job_summary,
    new_job_directory,
    normalise_language,
    resolve_dub_engine,
    resolve_transcription_engine,
    resolve_translation_engine,
    safe_suffix,
    save_upload,
    start_transcription_fields,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# Compatibility helpers retained for tests and older internal imports.
_claim = claim
_new_job_directory = new_job_directory
_job_summary = job_summary
_normalise_language = normalise_language
_resolve_dub_engine = resolve_dub_engine
_safe_suffix = safe_suffix
_format_sse_job_event = format_sse_job_event


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    await save_upload(upload, destination, settings)


def _resolve_engine(
    provider: str,
    model: str,
    model_size: str = "",
) -> tuple[str, str]:
    return resolve_transcription_engine(provider, model, model_size, settings)


def _resolve_translation_engine(provider: str, model: str) -> tuple[str, str]:
    return resolve_translation_engine(provider, model, settings)


def _start_transcription_fields(
    job: JobRecord,
    provider: str,
    model: str,
    source_language: str,
    analyze_speakers: bool,
) -> None:
    start_transcription_fields(
        job,
        provider,
        model,
        source_language,
        analyze_speakers,
    )


@router.get("")
def list_jobs() -> dict:
    return job_lifecycle.list_jobs()


@router.delete("/{job_id}")
def delete_job(job_id: str) -> dict:
    return job_lifecycle.delete_job(job_id)


@router.post("/import-subtitle")
async def import_subtitle(file: Annotated[UploadFile, File(...)]) -> dict:
    return await job_lifecycle.import_subtitle(file, settings)


@router.post("/transcribe")
async def start_transcription(
    video: Annotated[UploadFile, File(...)],
    source_language: Annotated[str, Form()] = "auto",
    provider: Annotated[str, Form()] = settings.transcription_provider,
    model: Annotated[str, Form()] = "",
    model_size: Annotated[str, Form()] = "",
    analyze_speakers: Annotated[bool, Form()] = False,
) -> dict:
    return await job_lifecycle.start_transcription(
        video,
        source_language,
        provider,
        model,
        model_size,
        analyze_speakers,
        settings,
    )


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    return job_lifecycle.get_job(job_id)


@router.post("/{job_id}/transcribe")
def restart_transcription(
    job_id: str,
    source_language: Annotated[str, Form()] = "auto",
    provider: Annotated[str, Form()] = settings.transcription_provider,
    model: Annotated[str, Form()] = "",
    analyze_speakers: Annotated[bool, Form()] = False,
) -> dict:
    return job_lifecycle.restart_transcription(
        job_id,
        source_language,
        provider,
        model,
        analyze_speakers,
        settings,
    )


@router.post("/{job_id}/analyze-speakers")
def start_speaker_analysis(job_id: str) -> dict:
    return job_operations.start_speaker_analysis(job_id, settings)


@router.post("/{job_id}/translate")
def start_translation(job_id: str, payload: TranslatePayload) -> dict:
    return job_operations.start_translation(job_id, payload, settings)


@router.post("/{job_id}/dub")
def start_dubbing(job_id: str, payload: DubPayload) -> dict:
    return job_operations.start_dubbing(job_id, payload, settings)


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    return job_lifecycle.cancel_job(job_id)


@router.put("/{job_id}/cues")
def update_cues(job_id: str, payload: CuesPayload) -> dict:
    return job_lifecycle.update_cues(job_id, payload)


@router.post("/{job_id}/split-long-cues")
def split_job_long_cues(job_id: str) -> dict:
    return job_lifecycle.split_job_long_cues(job_id)


@router.get("/{job_id}/download")
def download_subtitle(
    job_id: str,
    format: str = "srt",
    track: str = "source",
) -> Response:
    return job_lifecycle.download_subtitle(job_id, format, track)


@router.get("/{job_id}/events")
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    return await _stream_job_events(job_id, request)


__all__ = [
    "CueModel",
    "CuesPayload",
    "DubPayload",
    "TranslatePayload",
    "router",
    "settings",
]
