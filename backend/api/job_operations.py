"""Long-running analysis, translation, and dubbing job transitions."""

from __future__ import annotations

from fastapi import HTTPException

from ..core.config import Settings
from ..core.messages import detail
from ..domain.dubbing.aligner import DubbingError, dub_text, resolve_preference
from ..domain.subtitles.styles import is_valid_style_id
from ..domain.translation.style import STYLE_AUTO, STYLE_NOTES_LIMIT, STYLES
from ..jobs import runner
from ..jobs.model import STATUS_PROCESSING, public_job
from ..jobs.tasks import dubbing_task, speaker_analysis_task, translation_task
from .job_schemas import DubPayload, TranslatePayload
from .job_shared import claim, resolve_dub_engine, resolve_translation_engine


def start_speaker_analysis(job_id: str, app_settings: Settings) -> dict:
    analysis_model = (
        app_settings.speaker_analysis_model or app_settings.llm_model
    ).strip()
    if not app_settings.llm_base_url.strip() or not analysis_model:
        raise HTTPException(
            status_code=400,
            detail=detail("err.speakerAnalysis.notConfigured"),
        )

    with claim(job_id) as job:
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


def start_translation(
    job_id: str,
    payload: TranslatePayload,
    app_settings: Settings,
) -> dict:
    target_language = payload.target_language.strip()
    if not target_language:
        raise HTTPException(status_code=400, detail=detail("err.translation.targetMissing"))
    style_notes = payload.style_notes.strip()
    if len(style_notes) > STYLE_NOTES_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=detail(
                "err.translation.styleNotesTooLong",
                limit=STYLE_NOTES_LIMIT,
            ),
        )
    style = payload.style.strip().lower() or STYLE_AUTO
    if style != STYLE_AUTO and style not in STYLES:
        raise HTTPException(status_code=400, detail=detail("err.translation.badStyle"))
    style_ref = payload.style_ref.strip()
    if not is_valid_style_id(style_ref):
        style_ref = ""

    resolved_provider, selected_model = resolve_translation_engine(
        payload.provider,
        payload.model,
        app_settings,
    )
    if payload.from_cue < 0:
        raise HTTPException(status_code=400, detail=detail("err.translation.badFromCue"))

    with claim(job_id) as job:
        if not job.get("cues"):
            raise HTTPException(status_code=400, detail=detail("err.job.noCuesToTranslate"))
        if payload.from_cue >= len(job["cues"]):
            raise HTTPException(
                status_code=400,
                detail=detail("err.translation.noCuesFromHere"),
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


def start_dubbing(
    job_id: str,
    payload: DubPayload,
    app_settings: Settings,
) -> dict:
    resolved_provider, selected_voice = resolve_dub_engine(
        payload.provider,
        payload.voice,
    )
    gain = (
        app_settings.dub_original_gain
        if payload.original_gain is None
        else payload.original_gain
    )
    if not 0.0 <= gain <= 1.0:
        raise HTTPException(status_code=400, detail=detail("err.dub.badGain"))
    try:
        prefer = resolve_preference(payload.prefer)
    except DubbingError as exc:
        raise HTTPException(status_code=400, detail=exc.message.as_dict()) from exc

    with claim(job_id) as job:
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
