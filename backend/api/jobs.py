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
from ..dubbing import DubbingError, dub_text, resolve_preference
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
from ..jobs.tasks import (
    dubbing_task,
    speaker_analysis_task,
    transcription_task,
    translation_task,
)
from ..media import find_ffmpeg
from ..messages import detail
from ..subtitles import (
    SubtitleParseError,
    format_subtitle,
    parse_subtitle,
    split_long_cues,
)
from ..styles import is_valid_style_id
from ..translation_style import STYLE_AUTO, STYLE_NOTES_LIMIT, STYLES
from ..tts import (
    TTSProviderError,
    VOICE_NAME_LIMIT,
    default_voice,
    is_configured as tts_is_configured,
    resolve_tts_provider,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


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
    # Which saved style the two fields above came from, when they came from one.
    # It names the shortcut, never the rules: translation reads `style` and
    # `style_notes`, so a style deleted since is a forgotten name, not a failure.
    style_ref: str = ""
    provider: str = ""
    model: str = ""
    # 0-based; cues before it keep the translation they already have. 0 is both
    # "start at the beginning" and "translate everything", which is the same run.
    from_cue: int = 0


class DubPayload(BaseModel):
    voice: str = ""
    provider: str = ""
    # Empty means "whatever the install is configured for" — the client only
    # sends these when the user has actually moved them. `prefer` is settable
    # per run rather than only through DUB_PREFER so the two strategies can be
    # compared on the same project without restarting the server.
    prefer: str = ""
    original_gain: float | None = None
    shorten: bool | None = None


# ── Helpers ──────────────────────────────────────────────────────────


@contextmanager
def _claim(job_id: str, busy_code: str = "err.job.busy") -> Iterator[dict]:
    """Open a job for a transition that is illegal while it is busy.

    Checking status and marking the job as taken happens under one lock, so two
    simultaneous requests cannot both decide the job was free. `busy_code` names
    the refusal for the client — the default says only that the job is running.
    """

    with store.edit(job_id) as job:
        if job.get("status") == STATUS_PROCESSING:
            raise JobConflict(busy_code)
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
                        detail=detail("err.upload.tooLarge", limitMb=settings.max_upload_mb),
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
    # Recursive: the dub cache is a subdirectory, and a project whose voiced
    # segments outweigh its video should not read as the smaller of the two.
    size_bytes = sum(item.stat().st_size for item in job_dir.rglob("*") if item.is_file())
    video_path = job.get("video_path")

    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "error": job.get("error"),
        # Empty rather than a placeholder: naming an untitled project is the
        # client's job, in the client's language.
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


def _resolve_engine(provider: str, model: str, model_size: str = "") -> tuple[str, str]:
    """Validate the requested engine and settle on a model name."""

    provider = provider.strip().lower()
    if provider == "whisper":
        provider = "faster_whisper"
    if provider not in {"faster_whisper", "deepgram"}:
        raise HTTPException(status_code=400, detail=detail("err.transcription.badProvider"))
    if provider == "deepgram" and not settings.deepgram_api_key.strip():
        raise HTTPException(
            status_code=400, detail=detail("err.transcription.deepgramNotConfigured")
        )
    default_model = settings.deepgram_model if provider == "deepgram" else settings.whisper_model
    selected_model = model.strip() or model_size.strip() or default_model
    if len(selected_model) > 128:
        raise HTTPException(status_code=400, detail=detail("err.model.nameTooLong"))
    return provider, selected_model


from .. import ai


def _resolve_translation_engine(provider: str, model: str) -> tuple[str, str]:
    """Validate the translation engine and settle on a model name."""

    raw_provider = provider.strip() or ai.translation.settings.translation_provider
    resolved_provider = ai.resolve_translation_provider(raw_provider)

    if (
        resolved_provider == ai.TRANSLATION_OPENAI_COMPATIBLE
        and not ai.translation.settings.llm_base_url.strip()
    ):
        raise HTTPException(
            status_code=400, detail=detail("err.translation.llmNotConfigured")
        )
    if resolved_provider == ai.TRANSLATION_TRANSFORMERS:
        default_transformers_model = ai.translation.settings.translation_model.strip()
        if not (model.strip() or default_transformers_model):
            raise HTTPException(
                status_code=400,
                detail=detail("err.translation.transformersModelMissing"),
            )

    default_model = (
        ai.translation.settings.translation_model
        if resolved_provider == ai.TRANSLATION_TRANSFORMERS
        else ai.translation.settings.llm_model
    )
    selected_model = model.strip() or default_model
    if len(selected_model) > 128:
        raise HTTPException(status_code=400, detail=detail("err.model.nameTooLong"))
    return resolved_provider, selected_model


def _resolve_dub_engine(provider: str, voice: str) -> tuple[str, str]:
    """Validate the voice engine and settle on a voice.

    ffmpeg is checked here rather than in the worker: a dub that dies twenty
    minutes in because nothing can decode the synthesised audio is a worse way
    to learn the same thing.
    """

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
    job["cancel_requested"] = False
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
        raise HTTPException(status_code=400, detail=detail("err.subtitle.unsupported"))

    job = new_job(KIND_SUBTITLE_IMPORT, subtitle_name=file.filename or "subtitle.srt")
    with _new_job_directory(job) as directory:
        destination = directory / f"source{suffix}"
        await _save_upload(file, destination)
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
        raise HTTPException(status_code=400, detail=detail("err.upload.videoMissing"))
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
    with _claim(job_id) as job:
        video_path = job.get("video_path")
        if not video_path or not Path(video_path).exists():
            raise HTTPException(status_code=400, detail=detail("err.job.videoGone"))
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
            status_code=400, detail=detail("err.speakerAnalysis.notConfigured")
        )

    with _claim(job_id) as job:
        if not job.get("cues"):
            raise HTTPException(status_code=400, detail=detail("err.job.noCuesToAnalyze"))
        job["status"] = STATUS_PROCESSING
        job["error"] = None
        job["cancel_requested"] = False
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
        raise HTTPException(status_code=400, detail=detail("err.translation.targetMissing"))
    style_notes = payload.style_notes.strip()
    if len(style_notes) > STYLE_NOTES_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=detail("err.translation.styleNotesTooLong", limit=STYLE_NOTES_LIMIT),
        )
    style = payload.style.strip().lower() or STYLE_AUTO
    if style != STYLE_AUTO and style not in STYLES:
        raise HTTPException(status_code=400, detail=detail("err.translation.badStyle"))
    style_ref = payload.style_ref.strip()
    if not is_valid_style_id(style_ref):
        style_ref = ""

    resolved_provider, selected_model = _resolve_translation_engine(
        payload.provider, payload.model
    )

    if payload.from_cue < 0:
        raise HTTPException(status_code=400, detail=detail("err.translation.badFromCue"))

    with _claim(job_id) as job:
        if not job.get("cues"):
            raise HTTPException(status_code=400, detail=detail("err.job.noCuesToTranslate"))
        if payload.from_cue >= len(job["cues"]):
            raise HTTPException(
                status_code=400, detail=detail("err.translation.noCuesFromHere")
            )
        job["status"] = STATUS_PROCESSING
        job["error"] = None
        job["cancel_requested"] = False
        job["target_language"] = target_language
        job["translation_provider"] = resolved_provider
        job["translation_model"] = selected_model
        job["translation_style"] = style
        job["translation_style_notes"] = style_notes
        job["translation_style_ref"] = style_ref
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
            from_cue=payload.from_cue,
        ),
    )
    return snapshot


@router.post("/{job_id}/dub")
def start_dubbing(job_id: str, payload: DubPayload) -> dict:
    """Read the project aloud and lay the result over the original audio."""

    resolved_provider, selected_voice = _resolve_dub_engine(payload.provider, payload.voice)
    gain = settings.dub_original_gain if payload.original_gain is None else payload.original_gain
    if not 0.0 <= gain <= 1.0:
        raise HTTPException(status_code=400, detail=detail("err.dub.badGain"))
    try:
        prefer = resolve_preference(payload.prefer)
    except DubbingError as exc:
        raise HTTPException(status_code=400, detail=exc.message.as_dict()) from exc

    with _claim(job_id) as job:
        if not any(dub_text(cue) for cue in job.get("cues", [])):
            raise HTTPException(status_code=400, detail=detail("err.job.noCuesToDub"))
        job["status"] = STATUS_PROCESSING
        job["error"] = None
        job["cancel_requested"] = False
        job["dubbing_status"] = "pending"
        job["dubbing_error"] = None
        job["dubbing_report"] = None
        job["dubbing_provider"] = resolved_provider
        job["dubbing_voice"] = selected_voice
        # The old preview belongs to the old voice and the old cues. Dropping it
        # now is what stops the player from offering yesterday's dub while
        # today's is still rendering.
        job["dub_audio_path"] = None
        snapshot = public_job(job)

    runner.submit(
        job_id,
        "dubbing",
        dubbing_task(
            resolved_provider,
            selected_voice,
            original_gain=gain,
            prefer=prefer,
            shorten=payload.shorten,
        ),
    )
    return snapshot


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """Ask the running worker to stop at its next checkpoint.

    Only ever a request. A worker is a thread that cannot be interrupted from
    outside, and a provider call already in flight (Deepgram sends one request
    for the whole video) has to come back before anything is noticed — the job
    stays `processing` with the flag raised until then, which is exactly the
    "đang dừng…" the UI shows.
    """

    with store.edit(job_id) as job:
        if job.get("status") != STATUS_PROCESSING:
            raise JobConflict("err.job.notRunning")
        job["cancel_requested"] = True
        return public_job(job)


@router.put("/{job_id}/cues")
def update_cues(job_id: str, payload: CuesPayload) -> dict:
    cues = []
    for index, cue in enumerate(payload.cues, start=1):
        if cue.end <= cue.start:
            raise HTTPException(
                status_code=400, detail=detail("err.cue.endBeforeStart", cue=index)
            )
        if cue.start < 0:
            raise HTTPException(
                status_code=400, detail=detail("err.cue.negativeStart", cue=index)
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

    # A worker owns job["cues"] while it runs and would overwrite these edits on
    # completion, so refuse the edit rather than silently discard it later.
    with _claim(job_id, "err.job.busyCueEdit") as job:
        job["cues"] = cues
        if job["status"] in {STATUS_ERROR, STATUS_CANCELLED}:
            # The user has taken the cues in hand; the project is theirs again,
            # not a run that ended badly.
            job["status"] = STATUS_COMPLETED
            job["error"] = None
        return public_job(job)


@router.post("/{job_id}/split-long-cues")
def split_job_long_cues(job_id: str) -> dict:
    with _claim(job_id) as job:
        if not job.get("cues"):
            raise HTTPException(status_code=400, detail=detail("err.job.noCuesToSplit"))
        job["cues"] = split_long_cues(job["cues"])
        return public_job(job)


@router.get("/{job_id}/download")
def download_subtitle(job_id: str, format: str = "srt", track: str = "source") -> Response:
    job = store.read(job_id)
    format_name = format.lower().lstrip(".")
    if format_name not in {"srt", "vtt"}:
        raise HTTPException(status_code=400, detail=detail("err.download.badFormat"))
    if track not in {"source", "translated"}:
        raise HTTPException(status_code=400, detail=detail("err.download.badTrack"))
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
