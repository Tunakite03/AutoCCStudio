"""Job collection, editing, export, and transcription use cases."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, UploadFile
from fastapi.responses import Response

from ..core.config import Settings
from ..core.messages import detail
from ..domain.subtitles.parser import (
    SubtitleParseError,
    format_subtitle,
    parse_subtitle,
    split_long_cues,
)
from ..jobs import JobConflict, runner, store
from ..jobs.model import (
    KIND_SUBTITLE_IMPORT,
    KIND_TRANSCRIPTION,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_PROCESSING,
    new_job,
    public_job,
)
from ..jobs.tasks import transcription_task
from .job_schemas import CuesPayload
from .job_shared import (
    claim,
    job_summary,
    new_job_directory,
    resolve_transcription_engine,
    safe_suffix,
    save_upload,
    start_transcription_fields,
)


def list_jobs() -> dict:
    return {"jobs": store.summaries(job_summary)}


def delete_job(job_id: str) -> dict:
    store.delete(job_id)
    return {"deleted": job_id}


async def import_subtitle(file: UploadFile, app_settings: Settings) -> dict:
    suffix = safe_suffix(file.filename, ".srt")
    if suffix not in {".srt", ".vtt"}:
        raise HTTPException(status_code=400, detail=detail("err.subtitle.unsupported"))

    job = new_job(KIND_SUBTITLE_IMPORT, subtitle_name=file.filename or "subtitle.srt")
    with new_job_directory(job) as directory:
        destination = directory / f"source{suffix}"
        await save_upload(file, destination, app_settings)
        try:
            cues = parse_subtitle(destination.read_text(encoding="utf-8-sig"), suffix)
        except (OSError, UnicodeError, SubtitleParseError) as exc:
            raise HTTPException(
                status_code=400,
                detail=detail("err.subtitle.unreadable", cause=str(exc)),
            ) from exc
        if not cues:
            raise HTTPException(status_code=400, detail=detail("err.subtitle.noCues"))
        job["subtitle_path"] = str(destination)
        job["cues"] = cues
        store.create(job)
    return public_job(job)


async def start_transcription(
    video: UploadFile,
    source_language: str,
    provider: str,
    model: str,
    model_size: str,
    analyze_speakers: bool,
    app_settings: Settings,
) -> dict:
    if not video.filename:
        raise HTTPException(status_code=400, detail=detail("err.upload.videoMissing"))
    resolved_provider, selected_model = resolve_transcription_engine(
        provider,
        model,
        model_size,
        app_settings,
    )

    job = new_job(KIND_TRANSCRIPTION, video_name=video.filename)
    with new_job_directory(job) as directory:
        destination = directory / f"video{safe_suffix(video.filename, '.bin')}"
        await save_upload(video, destination, app_settings)
        job["video_path"] = str(destination)
        start_transcription_fields(
            job,
            resolved_provider,
            selected_model,
            source_language,
            analyze_speakers,
        )
        store.create(job)

    runner.submit(
        job["id"],
        "transcription",
        transcription_task(
            resolved_provider,
            selected_model,
            job["source_language"],
            analyze_speakers,
        ),
    )
    return public_job(job)


def get_job(job_id: str) -> dict:
    return store.get(job_id)


def restart_transcription(
    job_id: str,
    source_language: str,
    provider: str,
    model: str,
    analyze_speakers: bool,
    app_settings: Settings,
) -> dict:
    resolved_provider, selected_model = resolve_transcription_engine(
        provider,
        model,
        "",
        app_settings,
    )
    with claim(job_id) as job:
        video_path = job.get("video_path")
        if not video_path or not Path(video_path).exists():
            raise HTTPException(status_code=400, detail=detail("err.job.videoGone"))
        start_transcription_fields(
            job,
            resolved_provider,
            selected_model,
            source_language,
            analyze_speakers,
        )
        language = job["source_language"]
        snapshot = public_job(job)

    runner.submit(
        job_id,
        "transcription",
        transcription_task(
            resolved_provider,
            selected_model,
            language,
            analyze_speakers,
        ),
    )
    return snapshot


def cancel_job(job_id: str) -> dict:
    with store.edit(job_id) as job:
        if job.get("status") != STATUS_PROCESSING:
            raise JobConflict("err.job.notRunning")
        job["cancel_requested"] = True
        return public_job(job)


def update_cues(job_id: str, payload: CuesPayload) -> dict:
    cues = []
    for index, cue in enumerate(payload.cues, start=1):
        if cue.end <= cue.start:
            raise HTTPException(
                status_code=400,
                detail=detail("err.cue.endBeforeStart", cue=index),
            )
        if cue.start < 0:
            raise HTTPException(
                status_code=400,
                detail=detail("err.cue.negativeStart", cue=index),
            )
        cues.append(
            {
                "id": index,
                "start": round(cue.start, 3),
                "end": round(cue.end, 3),
                "text": cue.text,
                "translation": cue.translation,
                "speaker": cue.speaker,
            }
        )

    with claim(job_id, "err.job.busyCueEdit") as job:
        job["cues"] = cues
        if job["status"] in {STATUS_ERROR, STATUS_CANCELLED}:
            job["status"] = STATUS_COMPLETED
            job["error"] = None
        return public_job(job)


def split_job_long_cues(job_id: str) -> dict:
    with claim(job_id) as job:
        if not job.get("cues"):
            raise HTTPException(status_code=400, detail=detail("err.job.noCuesToSplit"))
        job["cues"] = split_long_cues(job["cues"])
        return public_job(job)


def download_subtitle(job_id: str, format_name: str, track: str) -> Response:
    job = store.read(job_id)
    normalized_format = format_name.lower().lstrip(".")
    if normalized_format not in {"srt", "vtt"}:
        raise HTTPException(status_code=400, detail=detail("err.download.badFormat"))
    if track not in {"source", "translated"}:
        raise HTTPException(status_code=400, detail=detail("err.download.badTrack"))
    content = format_subtitle(job.get("cues", []), normalized_format, track)
    stem = Path(job.get("video_name") or job.get("subtitle_name") or "subtitle").stem
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{stem}.{track}.{normalized_format}"'
            )
        },
    )
