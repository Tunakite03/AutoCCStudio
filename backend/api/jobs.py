"""Job lifecycle: create, inspect, edit, export, and start background work."""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from ..ai import (
    TRANSLATION_OPENAI_COMPATIBLE,
    TRANSLATION_TRANSFORMERS,
    resolve_translation_provider,
)
from ..config import settings
from ..jobs import JobConflict, runner, store
from ..jobs.model import (
    KIND_SUBTITLE_IMPORT,
    KIND_TRANSCRIPTION,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_PROCESSING,
    new_job,
    public_job,
)
from ..jobs.tasks import speaker_analysis_task, transcription_task, translation_task
from ..subtitles import (
    SubtitleParseError,
    format_subtitle,
    parse_subtitle,
    split_long_cues,
)
from ..translation_style import STYLE_AUTO, STYLES

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Long enough for a cast list and a page of house rules, short enough that it
# cannot bloat every batch prompt.
STYLE_NOTES_LIMIT = 2000


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
    provider: str = ""
    model: str = ""


# ── Helpers ──────────────────────────────────────────────────────────


@contextmanager
def _claim(job_id: str, reason: str) -> Iterator[dict]:
    """Open a job for a transition that is illegal while it is busy.

    Checking status and marking the job as taken happens under one lock, so two
    simultaneous requests cannot both decide the job was free.
    """

    with store.edit(job_id) as job:
        if job.get("status") == STATUS_PROCESSING:
            raise JobConflict(reason)
        yield job


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    size = 0
    try:
        with destination.open("wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File vượt quá giới hạn {settings.max_upload_mb} MB",
                    )
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


@contextmanager
def _new_job_directory(job: dict) -> Iterator[Path]:
    """Give a not-yet-persisted job a directory, cleaned up if creation fails.

    A rejected upload used to leave an empty directory behind forever: it had no
    job.json, so the project list skipped it and nothing ever collected it.
    """

    directory = store.job_dir(job["id"])
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    except Exception:
        store.discard_from_memory(job["id"])
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _job_summary(job: dict, job_dir: Path) -> dict:
    """The compact shape the dashboard lists — never the cue payload."""

    cues = job.get("cues", [])
    translated = sum(1 for cue in cues if str(cue.get("translation", "")).strip())
    try:
        updated_at = (job_dir / "job.json").stat().st_mtime
    except OSError:
        updated_at = 0.0
    size_bytes = sum(item.stat().st_size for item in job_dir.glob("*") if item.is_file())
    video_path = job.get("video_path")

    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "error": job.get("error"),
        "name": job.get("video_name") or job.get("subtitle_name") or "Project không tên",
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


def _resolve_engine(provider: str, model: str, model_size: str = "") -> tuple[str, str]:
    """Validate the requested engine and settle on a model name."""

    provider = provider.strip().lower()
    if provider == "whisper":
        provider = "faster_whisper"
    if provider not in {"faster_whisper", "deepgram"}:
        raise HTTPException(status_code=400, detail="Provider nhận dạng không hợp lệ")
    if provider == "deepgram" and not settings.deepgram_api_key.strip():
        raise HTTPException(
            status_code=400,
            detail="Deepgram chưa được cấu hình. Thêm DEEPGRAM_API_KEY vào file .env rồi khởi động lại app.",
        )
    default_model = settings.deepgram_model if provider == "deepgram" else settings.whisper_model
    selected_model = model.strip() or model_size.strip() or default_model
    if len(selected_model) > 128:
        raise HTTPException(status_code=400, detail="Tên model quá dài")
    return provider, selected_model


from .. import ai


def _resolve_translation_engine(provider: str, model: str) -> tuple[str, str]:
    """Validate the translation engine and settle on a model name."""

    raw_provider = provider.strip() or ai.settings.translation_provider
    resolved_provider = ai.resolve_translation_provider(raw_provider)

    if resolved_provider == ai.TRANSLATION_OPENAI_COMPATIBLE and not ai.settings.llm_base_url.strip():
        raise HTTPException(
            status_code=400,
            detail="LLM chưa được cấu hình. Thêm LLM_BASE_URL vào file .env rồi khởi động lại app.",
        )
    if resolved_provider == ai.TRANSLATION_TRANSFORMERS:
        default_transformers_model = ai.settings.translation_model.strip()
        if not (model.strip() or default_transformers_model):
            raise HTTPException(
                status_code=400,
                detail="TRANSLATION_MODEL chưa được cấu hình cho Transformers local.",
            )

    default_model = (
        ai.settings.translation_model
        if resolved_provider == ai.TRANSLATION_TRANSFORMERS
        else ai.settings.llm_model
    )
    selected_model = model.strip() or default_model
    if len(selected_model) > 128:
        raise HTTPException(status_code=400, detail="Tên model quá dài")
    return resolved_provider, selected_model


def _normalise_language(source_language: str) -> str | None:
    return None if source_language.lower() in {"", "auto"} else source_language.strip()


def _start_transcription_fields(
    job: dict,
    provider: str,
    model: str,
    source_language: str,
    analyze_speakers: bool,
) -> None:
    job["status"] = STATUS_PROCESSING
    job["error"] = None
    job["cues"] = []
    job["source_language"] = _normalise_language(source_language)
    job["transcription_provider"] = provider
    job["transcription_model"] = model
    job["speaker_analysis_requested"] = analyze_speakers
    job["speaker_analysis_status"] = "pending" if analyze_speakers else "skipped"
    job["speaker_analysis_error"] = None
    job["speaker_analysis_report"] = None


# ── Collection ───────────────────────────────────────────────────────


@router.get("")
def list_jobs() -> dict:
    """Every project still on disk, newest first."""

    return {"jobs": store.summaries(_job_summary)}


@router.delete("/{job_id}")
def delete_job(job_id: str) -> dict:
    store.delete(job_id)
    return {"deleted": job_id}


@router.post("/import-subtitle")
async def import_subtitle(file: Annotated[UploadFile, File(...)]) -> dict:
    suffix = _safe_suffix(file.filename, ".srt")
    if suffix not in {".srt", ".vtt"}:
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ file .srt hoặc .vtt")

    job = new_job(KIND_SUBTITLE_IMPORT, subtitle_name=file.filename or "subtitle.srt")
    with _new_job_directory(job) as directory:
        destination = directory / f"source{suffix}"
        await _save_upload(file, destination)
        try:
            cues = parse_subtitle(destination.read_text(encoding="utf-8-sig"), suffix)
        except (OSError, UnicodeError, SubtitleParseError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Không đọc được subtitle: {exc}"
            ) from exc
        if not cues:
            raise HTTPException(status_code=400, detail="Subtitle không có cue hợp lệ")
        job["subtitle_path"] = str(destination)
        job["cues"] = cues
        store.create(job)
    return public_job(job)


@router.post("/transcribe")
async def start_transcription(
    video: Annotated[UploadFile, File(...)],
    source_language: Annotated[str, Form()] = "auto",
    provider: Annotated[str, Form()] = settings.transcription_provider,
    model: Annotated[str, Form()] = "",
    model_size: Annotated[str, Form()] = "",
    analyze_speakers: Annotated[bool, Form()] = False,
) -> dict:
    if not video.filename:
        raise HTTPException(status_code=400, detail="Thiếu file video")
    resolved_provider, selected_model = _resolve_engine(provider, model, model_size)

    job = new_job(KIND_TRANSCRIPTION, video_name=video.filename)
    with _new_job_directory(job) as directory:
        destination = directory / f"video{_safe_suffix(video.filename, '.bin')}"
        await _save_upload(video, destination)
        job["video_path"] = str(destination)
        _start_transcription_fields(
            job, resolved_provider, selected_model, source_language, analyze_speakers
        )
        store.create(job)

    runner.submit(
        job["id"],
        "transcription",
        transcription_task(
            resolved_provider, selected_model, job["source_language"], analyze_speakers
        ),
    )
    return public_job(job)


def _safe_suffix(filename: str | None, fallback: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix and len(suffix) <= 10 else fallback


# ── Single job ───────────────────────────────────────────────────────


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    return store.get(job_id)


@router.post("/{job_id}/transcribe")
def restart_transcription(
    job_id: str,
    source_language: Annotated[str, Form()] = "auto",
    provider: Annotated[str, Form()] = settings.transcription_provider,
    model: Annotated[str, Form()] = "",
    analyze_speakers: Annotated[bool, Form()] = False,
) -> dict:
    """Run recognition again on the video already stored with the job.

    Reopening a project leaves the browser without the original file, and a
    re-upload of a multi-hundred-megabyte video would be pure waste.
    """

    resolved_provider, selected_model = _resolve_engine(provider, model)
    with _claim(job_id, "Job đang xử lý") as job:
        video_path = job.get("video_path")
        if not video_path or not Path(video_path).exists():
            raise HTTPException(
                status_code=400, detail="Project này không còn video trên máy chủ"
            )
        _start_transcription_fields(
            job, resolved_provider, selected_model, source_language, analyze_speakers
        )
        language = job["source_language"]
        snapshot = public_job(job)

    runner.submit(
        job_id,
        "transcription",
        transcription_task(resolved_provider, selected_model, language, analyze_speakers),
    )
    return snapshot


@router.post("/{job_id}/analyze-speakers")
def start_speaker_analysis(job_id: str) -> dict:
    analysis_model = (settings.speaker_analysis_model or settings.llm_model).strip()
    if not settings.llm_base_url.strip() or not analysis_model:
        raise HTTPException(
            status_code=400, detail="Chưa cấu hình LLM phân tích lượt thoại"
        )

    with _claim(job_id, "Job đang xử lý") as job:
        if not job.get("cues"):
            raise HTTPException(status_code=400, detail="Job chưa có cue để phân tích")
        job["status"] = STATUS_PROCESSING
        job["error"] = None
        job["speaker_analysis_requested"] = True
        job["speaker_analysis_status"] = "pending"
        job["speaker_analysis_error"] = None
        job["speaker_analysis_report"] = None
        snapshot = public_job(job)

    runner.submit(job_id, "speaker analysis", speaker_analysis_task())
    return snapshot


@router.post("/{job_id}/translate")
def start_translation(job_id: str, payload: TranslatePayload) -> dict:
    target_language = payload.target_language.strip()
    if not target_language:
        raise HTTPException(status_code=400, detail="Thiếu ngôn ngữ đích")
    style_notes = payload.style_notes.strip()
    if len(style_notes) > STYLE_NOTES_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"Ghi chú phong cách tối đa {STYLE_NOTES_LIMIT} ký tự",
        )
    style = payload.style.strip().lower() or STYLE_AUTO
    if style != STYLE_AUTO and style not in STYLES:
        raise HTTPException(status_code=400, detail="Phong cách dịch không hợp lệ")

    resolved_provider, selected_model = _resolve_translation_engine(
        payload.provider, payload.model
    )

    with _claim(job_id, "Job đang xử lý") as job:
        if not job.get("cues"):
            raise HTTPException(status_code=400, detail="Job chưa có cue để dịch")
        job["status"] = STATUS_PROCESSING
        job["error"] = None
        job["target_language"] = target_language
        job["translation_provider"] = resolved_provider
        job["translation_model"] = selected_model
        job["translation_style"] = style
        job["translation_style_notes"] = style_notes
        source_language = job.get("detected_language") or job.get("source_language")
        snapshot = public_job(job)

    runner.submit(
        job_id,
        "translation",
        translation_task(
            target_language,
            source_language=source_language,
            style=style,
            style_notes=style_notes,
            provider=resolved_provider,
            model=selected_model,
        ),
    )
    return snapshot


@router.put("/{job_id}/cues")
def update_cues(job_id: str, payload: CuesPayload) -> dict:
    cues = []
    for index, cue in enumerate(payload.cues, start=1):
        if cue.end <= cue.start:
            raise HTTPException(status_code=400, detail=f"Cue {index}: end phải lớn hơn start")
        if cue.start < 0:
            raise HTTPException(status_code=400, detail=f"Cue {index}: start không được âm")
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

    # A worker owns job["cues"] while it runs and would overwrite these edits on
    # completion, so refuse the edit rather than silently discard it later.
    with _claim(job_id, "Job đang xử lý, không thể sửa cue") as job:
        job["cues"] = cues
        if job["status"] == STATUS_ERROR:
            job["status"] = STATUS_COMPLETED
            job["error"] = None
        return public_job(job)


@router.post("/{job_id}/split-long-cues")
def split_job_long_cues(job_id: str) -> dict:
    with _claim(job_id, "Job đang xử lý") as job:
        if not job.get("cues"):
            raise HTTPException(status_code=400, detail="Job chưa có cue để tách")
        job["cues"] = split_long_cues(job["cues"])
        return public_job(job)


@router.get("/{job_id}/download")
def download_subtitle(job_id: str, format: str = "srt", track: str = "source") -> Response:
    job = store.read(job_id)
    format_name = format.lower().lstrip(".")
    if format_name not in {"srt", "vtt"}:
        raise HTTPException(status_code=400, detail="Format phải là srt hoặc vtt")
    if track not in {"source", "translated"}:
        raise HTTPException(status_code=400, detail="Track phải là source hoặc translated")
    content = format_subtitle(job.get("cues", []), format_name, track)
    stem = Path(job.get("video_name") or job.get("subtitle_name") or "subtitle").stem
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}.{track}.{format_name}"'},
    )


# ── Live updates ─────────────────────────────────────────────────────


def _format_sse_job_event(revision: int, payload: str) -> str:
    return f"id: {revision}\nevent: job\ndata: {payload}\n\n"


@router.get("/{job_id}/events")
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    # Resolve a missing job before StreamingResponse commits the 200 headers.
    snapshot = await asyncio.to_thread(store.get, job_id)

    try:
        last_revision = int(request.headers.get("last-event-id", "-1"))
    except ValueError:
        last_revision = -1

    async def event_stream():
        nonlocal last_revision, snapshot
        subscriber = store.subscribe(job_id)
        _, queue = subscriber
        try:
            yield "retry: 2000\n\n"
            revision = int(snapshot.get("revision", 0))
            if revision > last_revision or snapshot["status"] != STATUS_PROCESSING:
                payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                yield _format_sse_job_event(revision, payload)
                last_revision = revision
            if snapshot["status"] != STATUS_PROCESSING:
                return

            while True:
                if await request.is_disconnected():
                    return
                try:
                    revision, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if revision <= last_revision:
                    continue
                last_revision = revision
                yield _format_sse_job_event(revision, payload)
                if json.loads(payload)["status"] != STATUS_PROCESSING:
                    return
        finally:
            store.unsubscribe(job_id, subscriber)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
