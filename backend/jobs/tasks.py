"""The three long-running job workflows.

Each follows the same shape: read what it needs from the job, do the slow work
unlocked while reporting progress, then reopen the job to write results. That
split is why a running transcription no longer blocks `GET /api/jobs/{id}`, and
why a user's edit can no longer be silently overwritten by a worker that has
been holding a stale copy of the job for ten minutes.
"""

from __future__ import annotations

from pathlib import Path

from ..ai import analyze_dialogue_turns, transcribe_video, translate_cues
from ..core.cancellation import OperationCancelled
from ..core.config import get_logger
from ..core.messages import Message
from ..domain.dubbing.aligner import CACHE_DIR, cues_fingerprint, dub_cues, policy_from_settings
from ..domain.translation.style import STYLE_AUTO
from ..infrastructure.media.ffmpeg import encode_audio, mix_dub_over_original
from .model import (
    PHASE_ANALYZING,
    PHASE_DUBBING,
    PHASE_TRANSCRIBING,
    PHASE_TRANSLATING,
    clean_cues,
)
from .runner import JobContext, finish
from .types import JobRecord

logger = get_logger("jobs.tasks")


def _phase_reporter(context: JobContext, phase: str):
    def report(current: int, total: int | None, message: Message) -> None:
        context.progress(phase, current=current, total=total, message=message)

    return report


def _apply_speaker_analysis(job: JobRecord, language: str | None, report: dict) -> None:
    """Fold an analysis report into the job's speaker-analysis status fields."""

    job["speaker_analysis_report"] = report
    failed = int(report.get("failed_cues", 0))
    total = int(report.get("total_cues", 0))
    if failed == 0:
        job["speaker_analysis_status"] = "completed"
        job["speaker_analysis_error"] = None
    elif failed < total:
        job["speaker_analysis_status"] = "partial"
        job["speaker_analysis_error"] = Message(
            "err.speakerAnalysis.partial", {"failed": failed, "total": total}
        ).as_dict()
    else:
        job["speaker_analysis_status"] = "failed"
        job["speaker_analysis_error"] = Message("err.speakerAnalysis.failed").as_dict()


def transcription_task(
    provider: str,
    model: str,
    language: str | None,
    analyze_speakers: bool,
):
    def run(context: JobContext) -> None:
        job = context.read()
        cues, detected_language = transcribe_video(
            Path(job["video_path"]),
            model_size=model,
            language=language,
            provider=provider,
            on_progress=_phase_reporter(context, PHASE_TRANSCRIBING),
        )
        cues = clean_cues(cues)
        logger.info(
            "job %s: transcription produced %d cues (detected=%s)",
            context.job_id, len(cues), detected_language,
        )

        with context.edit() as job:
            job["cues"] = cues
            job["detected_language"] = detected_language
            if not analyze_speakers:
                job["speaker_analysis_status"] = "skipped"
                finish(job)
                return
            job["speaker_analysis_status"] = "processing"
            job["speaker_analysis_error"] = None

        # Checked after the cues are safely written, never before: a stop must
        # cost the user the optional pass, not the transcript it was refining.
        context.raise_if_cancelled()
        _analyze(context, cues, detected_language or language)

    return run


def speaker_analysis_task():
    def run(context: JobContext) -> None:
        job = context.read()
        context.raise_if_cancelled()
        with context.edit() as opened:
            opened["speaker_analysis_status"] = "processing"
            opened["speaker_analysis_error"] = None
        _analyze(
            context,
            job.get("cues", []),
            job.get("detected_language") or job.get("source_language"),
        )

    return run


def _analyze(context: JobContext, cues: list[dict], language: str | None) -> None:
    """Run the optional speaker pass, keeping the transcript whatever happens.

    A failure here must not take the transcription with it — the cues are
    already usable, only the line-break refinement is missing.
    """

    try:
        analyzed, report = analyze_dialogue_turns(
            cues,
            language,
            return_report=True,
            on_progress=_phase_reporter(context, PHASE_ANALYZING),
        )
    except OperationCancelled:
        # A stop is not an analysis failure: the runner settles the job, and the
        # cues stay exactly as the transcription left them.
        raise
    except Exception as exc:
        logger.exception("job %s: speaker analysis failed", context.job_id)
        from .runner import describe_error

        with context.edit() as job:
            job["speaker_analysis_status"] = "failed"
            job["speaker_analysis_error"] = describe_error(exc)
            finish(job)
        return

    with context.edit() as job:
        job["cues"] = analyzed
        _apply_speaker_analysis(job, language, report)
        finish(job)


DUB_PREVIEW_NAME = "preview.m4a"


def dubbing_task(
    provider: str,
    voice: str,
    *,
    original_gain: float,
    prefer: str | None = None,
    shorten: bool | None = None,
    llm_model: str | None = None,
):
    """Voice every cue, then lay the result over the original audio.

    Two stages with a checkpoint between them: synthesis is the long one and its
    results are already on disk in the segment cache, so a failure while mixing
    costs the mix and nothing else.
    """

    def run(context: JobContext) -> None:
        job = context.read()
        context.raise_if_cancelled()
        with context.edit() as opened:
            opened["dubbing_status"] = "processing"
            opened["dubbing_error"] = None
            opened["dubbing_provider"] = provider
            opened["dubbing_voice"] = voice

        job_dir = context.store.job_dir(context.job_id)
        policy = policy_from_settings(**({"prefer": prefer} if prefer else {}))
        track, report = dub_cues(
            job.get("cues", []),
            job_dir=job_dir,
            voice=voice,
            provider=provider,
            policy=policy,
            target_language=job.get("target_language"),
            shorten=shorten,
            llm_model=llm_model,
            on_progress=_phase_reporter(context, PHASE_DUBBING),
            stop_check=context.raise_if_cancelled,
        )

        context.raise_if_cancelled()
        context.progress(PHASE_DUBBING, message=Message("progress.dubMixing"))
        preview_path = job_dir / CACHE_DIR / DUB_PREVIEW_NAME
        video_path = job.get("video_path")
        if video_path and Path(video_path).exists():
            mix_dub_over_original(
                Path(video_path), track, preview_path, original_gain=original_gain
            )
        else:
            # A subtitle-only project has nothing to mix under: the dub is the
            # whole soundtrack, and it is still worth listening back to.
            encode_audio(track, preview_path)

        logger.info(
            "job %s: dub voiced %d/%d cues (%d failed)",
            context.job_id,
            report.get("voiced_cues", 0),
            report.get("total_cues", 0),
            report.get("failed_cues", 0),
        )

        with context.edit() as opened:
            opened["dub_audio_path"] = str(preview_path)
            opened["dubbing_report"] = report
            # Fingerprint what was actually voiced, not what the job holds now.
            # Cue edits are refused while a job runs, so the two agree — and if
            # that ever stops being true, this reports stale rather than fresh.
            opened["dubbing_fingerprint"] = cues_fingerprint(job.get("cues", []))
            _apply_dubbing_status(opened, report)
            finish(opened)

    return run


def _apply_dubbing_status(job: JobRecord, report: dict) -> None:
    """A dub with silent lines is partial, not broken — the same as analysis."""

    failed = int(report.get("failed_cues", 0))
    if failed == 0:
        job["dubbing_status"] = "completed"
        job["dubbing_error"] = None
        return
    job["dubbing_status"] = "partial"
    job["dubbing_error"] = Message(
        "err.dub.partial",
        {"failed": failed, "total": int(report.get("total_cues", 0))},
    ).as_dict()


def translation_task(
    target_language: str,
    *,
    source_language: str | None = None,
    style: str = STYLE_AUTO,
    style_notes: str = "",
    provider: str | None = None,
    model: str | None = None,
    from_cue: int = 0,
):
    def run(context: JobContext) -> None:
        job = context.read()
        context.raise_if_cancelled()

        def checkpoint(done: int, total: int, cues_so_far: list[dict]) -> None:
            # Persisted, not just published: if a later batch fails, everything
            # translated so far is already safe on disk.
            snapshot = [dict(cue) for cue in cues_so_far]
            context.checkpoint(lambda opened: opened.update(cues=snapshot))
            context.progress(
                PHASE_TRANSLATING,
                current=done,
                total=total,
                message=Message("progress.translated", {"done": done, "total": total}),
            )

        cues = translate_cues(
            job.get("cues", []),
            target_language,
            on_batch=checkpoint,
            source_language=source_language
            or job.get("detected_language")
            or job.get("source_language"),
            style=style,
            style_notes=style_notes,
            provider=provider or job.get("translation_provider"),
            model=model or job.get("translation_model"),
            from_cue=from_cue,
        )

        with context.edit() as opened:
            opened["cues"] = cues
            opened["target_language"] = target_language
            if provider:
                opened["translation_provider"] = provider
            if model:
                opened["translation_model"] = model
            finish(opened)

    return run
