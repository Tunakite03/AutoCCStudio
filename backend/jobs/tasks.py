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
from ..config import get_logger
from .model import (
    PHASE_ANALYZING,
    PHASE_TRANSCRIBING,
    PHASE_TRANSLATING,
    clean_cues,
)
from .runner import JobContext, finish

logger = get_logger("jobs.tasks")


def _phase_reporter(context: JobContext, phase: str):
    def report(current: int, total: int | None, message: str) -> None:
        context.progress(phase, current=current, total=total, message=message)

    return report


def _apply_speaker_analysis(job: dict, language: str | None, report: dict) -> None:
    """Fold an analysis report into the job's speaker-analysis status fields."""

    job["speaker_analysis_report"] = report
    failed = int(report.get("failed_cues", 0))
    total = int(report.get("total_cues", 0))
    if failed == 0:
        job["speaker_analysis_status"] = "completed"
        job["speaker_analysis_error"] = None
    elif failed < total:
        job["speaker_analysis_status"] = "partial"
        job["speaker_analysis_error"] = (
            f"AI giữ nguyên {failed}/{total} cue không đạt validation"
        )
    else:
        job["speaker_analysis_status"] = "failed"
        job["speaker_analysis_error"] = (
            "AI không trả về cue hợp lệ; đã giữ kết quả diarization âm thanh"
        )


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

        _analyze(context, cues, detected_language or language)

    return run


def speaker_analysis_task():
    def run(context: JobContext) -> None:
        job = context.read()
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


def translation_task(target_language: str):
    def run(context: JobContext) -> None:
        job = context.read()

        def checkpoint(done: int, total: int, cues_so_far: list[dict]) -> None:
            # Persisted, not just published: if a later batch fails, everything
            # translated so far is already safe on disk.
            snapshot = [dict(cue) for cue in cues_so_far]
            context.checkpoint(lambda opened: opened.update(cues=snapshot))
            context.progress(
                PHASE_TRANSLATING,
                current=done,
                total=total,
                message=f"Đã dịch {done}/{total} dòng",
            )

        cues = translate_cues(
            job.get("cues", []),
            target_language,
            on_batch=checkpoint,
        )

        with context.edit() as opened:
            opened["cues"] = cues
            opened["target_language"] = target_language
            finish(opened)

    return run
