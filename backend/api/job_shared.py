"""Shared validation, upload, and transition helpers for job routes."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, UploadFile

from ..ai import (
    TRANSLATION_OPENAI_COMPATIBLE,
    TRANSLATION_TRANSFORMERS,
    get_translation_settings,
    resolve_translation_provider,
)
from ..ai.tts import (
    VOICE_NAME_LIMIT,
    TTSProviderError,
    default_voice,
    resolve_tts_provider,
)
from ..ai.tts import (
    is_configured as tts_is_configured,
)
from ..core.config import Settings
from ..core.messages import detail
from ..infrastructure.media.ffmpeg import find_ffmpeg
from ..jobs import JobConflict, store
from ..jobs.model import STATUS_PROCESSING
from ..jobs.types import JobRecord


@contextmanager
def claim(job_id: str, busy_code: str = "err.job.busy") -> Iterator[JobRecord]:
    """Atomically refuse or claim a transition that is illegal while busy."""

    with store.edit(job_id) as job:
        if job.get("status") == STATUS_PROCESSING:
            raise JobConflict(busy_code)
        yield job


async def save_upload(
    upload: UploadFile,
    destination: Path,
    app_settings: Settings,
) -> None:
    size = 0
    try:
        with destination.open("wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > app_settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=detail(
                            "err.upload.tooLarge",
                            limitMb=app_settings.max_upload_mb,
                        ),
                    )
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


@contextmanager
def new_job_directory(job: JobRecord) -> Iterator[Path]:
    """Create a new job directory and remove it if creation is rejected."""

    directory = store.job_dir(job["id"])
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    except Exception:
        store.discard_from_memory(job["id"])
        shutil.rmtree(directory, ignore_errors=True)
        raise


def job_summary(job: JobRecord, job_dir: Path) -> dict:
    """The compact shape the dashboard lists, without cue payloads."""

    cues = job.get("cues", [])
    translated = sum(1 for cue in cues if str(cue.get("translation", "")).strip())
    try:
        updated_at = (job_dir / "job.json").stat().st_mtime
    except OSError:
        updated_at = 0.0
    size_bytes = sum(item.stat().st_size for item in job_dir.rglob("*") if item.is_file())
    video_path = job.get("video_path")

    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "error": job.get("error"),
        "name": job.get("video_name") or job.get("subtitle_name") or "",
        "video_available": bool(video_path) and Path(video_path).exists(),
        "has_thumbnail": (job_dir / "thumb.jpg").exists(),
        "cue_count": len(cues),
        "translated_count": translated,
        "duration_seconds": max((float(cue.get("end", 0)) for cue in cues), default=0.0),
        "detected_language": job.get("detected_language"),
        "source_language": job.get("source_language"),
        "target_language": job.get("target_language"),
        "transcription_provider": job.get("transcription_provider"),
        "translation_provider": job.get("translation_provider"),
        "translation_model": job.get("translation_model"),
        "speaker_analysis_status": job.get("speaker_analysis_status"),
        "updated_at": updated_at,
        "size_bytes": size_bytes,
    }


def resolve_transcription_engine(
    provider: str,
    model: str,
    model_size: str,
    app_settings: Settings,
) -> tuple[str, str]:
    provider = provider.strip().lower()
    if provider == "whisper":
        provider = "faster_whisper"
    if provider not in {"faster_whisper", "deepgram"}:
        raise HTTPException(status_code=400, detail=detail("err.transcription.badProvider"))
    if provider == "deepgram" and not app_settings.deepgram_api_key.strip():
        raise HTTPException(
            status_code=400,
            detail=detail("err.transcription.deepgramNotConfigured"),
        )
    default_model = (
        app_settings.deepgram_model if provider == "deepgram" else app_settings.whisper_model
    )
    selected_model = model.strip() or model_size.strip() or default_model
    if len(selected_model) > 128:
        raise HTTPException(status_code=400, detail=detail("err.model.nameTooLong"))
    return provider, selected_model


def resolve_translation_engine(
    provider: str,
    model: str,
    app_settings: Settings,
) -> tuple[str, str]:
    runtime_settings = get_translation_settings()
    raw_provider = provider.strip() or runtime_settings.translation_provider
    resolved_provider = resolve_translation_provider(raw_provider)

    if (
        resolved_provider == TRANSLATION_OPENAI_COMPATIBLE
        and not runtime_settings.llm_base_url.strip()
    ):
        raise HTTPException(
            status_code=400,
            detail=detail("err.translation.llmNotConfigured"),
        )
    if resolved_provider == TRANSLATION_TRANSFORMERS:
        default_transformers_model = runtime_settings.translation_model.strip()
        if not (model.strip() or default_transformers_model):
            raise HTTPException(
                status_code=400,
                detail=detail("err.translation.transformersModelMissing"),
            )

    default_model = (
        runtime_settings.translation_model
        if resolved_provider == TRANSLATION_TRANSFORMERS
        else runtime_settings.llm_model
    )
    selected_model = model.strip() or default_model
    if len(selected_model) > 128:
        raise HTTPException(status_code=400, detail=detail("err.model.nameTooLong"))
    return resolved_provider, selected_model


def resolve_dub_engine(provider: str, voice: str) -> tuple[str, str]:
    try:
        resolved = resolve_tts_provider(provider)
    except TTSProviderError as exc:
        raise HTTPException(status_code=400, detail=exc.message.as_dict()) from exc
    if not tts_is_configured(resolved):
        raise HTTPException(status_code=400, detail=detail("err.dub.notConfigured"))
    if not find_ffmpeg():
        raise HTTPException(status_code=503, detail=detail("err.ffmpeg.missing"))

    selected_voice = voice.strip() or default_voice(resolved)
    if not selected_voice:
        raise HTTPException(status_code=400, detail=detail("err.dub.voiceMissing"))
    if len(selected_voice) > VOICE_NAME_LIMIT:
        raise HTTPException(status_code=400, detail=detail("err.dub.badVoice"))
    return resolved, selected_voice


def normalise_language(source_language: str) -> str | None:
    return None if source_language.lower() in {"", "auto"} else source_language.strip()


def start_transcription_fields(
    job: JobRecord,
    provider: str,
    model: str,
    source_language: str,
    analyze_speakers: bool,
) -> None:
    job["status"] = STATUS_PROCESSING
    job["error"] = None
    job["cancel_requested"] = False
    job["cues"] = []
    job["source_language"] = normalise_language(source_language)
    job["transcription_provider"] = provider
    job["transcription_model"] = model
    job["speaker_analysis_requested"] = analyze_speakers
    job["speaker_analysis_status"] = "pending" if analyze_speakers else "skipped"
    job["speaker_analysis_error"] = None
    job["speaker_analysis_report"] = None


def safe_suffix(filename: str | None, fallback: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix and len(suffix) <= 10 else fallback
