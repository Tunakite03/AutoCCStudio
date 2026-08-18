"""Fitting spoken lines to their cues, and assembling them into one track.

A translated line almost never takes exactly as long to say as the cue it
belongs to. Three things can be done about it, in this order of preference:

1. speed the recording up a little — keeps the sync, costs nothing;
2. let it run into the silence that follows — keeps the voice natural;
3. ask the LLM to say the same thing in fewer words, and try again.

The order matters: each step is cheaper and less destructive than the one after
it, so nothing reaches the LLM that a 6% speed-up would have solved.
`fit_segment` is where that decision lives, and it is deliberately a pure
function — every boundary case is worth a test, and none of them should need
audio to reproduce.

Assembly writes the track sample by sample rather than handing ffmpeg a filter
graph: a feature-length video is a thousand cues, and a thousand `adelay` inputs
is past what a command line can carry on any platform.
"""

from __future__ import annotations

import hashlib
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace as replace_fields
from pathlib import Path
from typing import Callable

from . import tts
from .cancellation import OperationCancelled, clear_stop_check, set_stop_check
from .config import get_logger, settings
from .media import (
    DUB_SAMPLE_RATE,
    DUB_SAMPLE_WIDTH,
    decode_to_pcm,
    pcm_seconds,
    retime_pcm,
    trim_silence,
)
from .messages import CodedError, Message
from .subtitles import strip_speaker_labels

logger = get_logger("dubbing")

OP_DUB = Message("op.dub")


class DubbingError(CodedError):
    """The dub could not be produced at all."""


# How a line ended up fitting its cue. Reported per run so the UI can say "12
# lines had to be sped up" instead of leaving the user to notice it by ear.
FIT_EXACT = "exact"
FIT_SPED_UP = "sped_up"
FIT_SPILL = "spill"
FIT_OVERFLOW = "overflow"

TRACK_NAME = "track.wav"
CACHE_DIR = "dub"

# Which of the first two strategies a run reaches for first. Speeding a line up
# holds it against its subtitle; letting it spill keeps the delivery but lets
# picture and sound drift apart. Both are defensible, and which one is right is
# a property of the material, not of the code.
PREFER_SPEED = "speed"
PREFER_NATURAL = "natural"
PREFERENCES = (PREFER_SPEED, PREFER_NATURAL)

ProgressCallback = Callable[[int, int | None, Message], None]


@dataclass(frozen=True)
class FitPolicy:
    """The limits the three strategies work inside."""

    # Above this a voice stops sounding like a person in a hurry and starts
    # sounding like a fast-forward button.
    max_speedup: float = 1.25
    # Below this the correction is inaudible and not worth a re-encode.
    min_speedup: float = 1.02
    # How far a line may run past its cue, when the silence is there to take it.
    max_spill: float = 1.2
    # Never eat the whole gap: landing exactly on the next cue sounds like an
    # interruption even when the arithmetic says it fits.
    spill_guard: float = 0.08
    shorten_attempts: int = 2
    # Ask for a little less than what would just fit — a model asked for exactly
    # enough reliably comes back one word over.
    shorten_margin: float = 0.92
    min_budget: float = 0.2
    min_chars: int = 8
    prefer: str = PREFER_SPEED


@dataclass(frozen=True)
class FitDecision:
    fit: str
    tempo: float = 1.0
    # Set only when the first two strategies were not enough: the character
    # budget the LLM has to rewrite the line into.
    target_chars: int | None = None


def resolve_preference(name: str) -> str:
    """Normalise a strategy preference, falling back to the configured one."""

    value = (name or "").strip().lower()
    if not value:
        value = settings.dub_prefer.strip().lower() or PREFER_SPEED
    if value not in PREFERENCES:
        raise DubbingError("err.dub.badPreference", preference=value)
    return value


def policy_from_settings(**overrides) -> FitPolicy:
    try:
        prefer = resolve_preference("")
    except DubbingError:
        # A typo in DUB_PREFER should cost a log line, not every dub on the box.
        logger.warning(
            "DUB_PREFER=%r is not a preference; using %s",
            settings.dub_prefer,
            PREFER_SPEED,
        )
        prefer = PREFER_SPEED
    policy = FitPolicy(
        max_speedup=max(1.0, settings.dub_max_speedup),
        max_spill=max(0.0, settings.dub_max_spill_seconds),
        prefer=prefer,
    )
    return replace_fields(policy, **overrides) if overrides else policy


def dub_text(cue: dict) -> str:
    """What a cue should be voiced as: its translation, or its source line.

    Falling back to the source is what lets a project be voiced before it is
    translated — a subtitle read aloud in its own language is still a voice-over.
    Line breaks mark speaker turns, and a spoken line has no room for them.
    """

    for field in ("translation", "text"):
        value = strip_speaker_labels(str(cue.get(field, "")))
        spoken = " ".join(value.split())
        if spoken:
            return spoken
    return ""


def cues_fingerprint(cues: list[dict]) -> str:
    """Everything about the cues that decides what the dub sounds like.

    The words, because they are what is spoken; the timings, because they are
    where it is spoken. Nothing else — restyling a translation the voice never
    reads, or renaming the project, must not age a dub that is still correct.

    Cues with nothing to say are skipped, so inserting a blank cue between two
    lines does not invalidate a recording it cannot possibly have changed.
    """

    parts = []
    for cue in cues:
        spoken = dub_text(cue)
        if not spoken:
            continue
        start = round(float(cue.get("start", 0.0)), 3)
        end = round(float(cue.get("end", 0.0)), 3)
        parts.append(f"{start}|{end}|{spoken}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def dub_is_stale(job: dict) -> bool:
    """Whether the dub this project holds was made from the cues it holds now.

    Without this a user edits one line, exports, and ships the take from before
    the edit — the old wording, at the old timings, with nothing on screen
    saying so. Answered on every projection of the job rather than cached on a
    flag, because the number of ways cues can change (an edit, a re-translation,
    a split, an undo) is exactly the number of places a flag would be forgotten.
    """

    if not job.get("dub_audio_path"):
        return False
    fingerprint = job.get("dubbing_fingerprint")
    if not fingerprint:
        # A dub from before this was recorded. It may well be current, but the
        # only honest answer to "was it made from these cues?" is "cannot tell",
        # and of the two ways to be wrong this is the one that costs a re-run
        # rather than a wrong export.
        return True
    return fingerprint != cues_fingerprint(job.get("cues", []))


def budget_for(cues: list[dict], index: int, policy: FitPolicy) -> tuple[float, float]:
    """How long this cue's line may run: on its own, and using the gap after it."""

    cue = cues[index]
    start = float(cue.get("start", 0.0))
    end = float(cue.get("end", 0.0))
    hard = max(end - start, policy.min_budget)

    following: float | None = None
    for later in cues[index + 1 :]:
        later_start = float(later.get("start", 0.0))
        if later_start >= start:
            following = later_start
            break
    if following is None:
        # Nothing after it, so the only limit is how far a listener will follow
        # a line that has visibly left its subtitle behind.
        return hard, hard + policy.max_spill

    gap = following - (start + hard)
    usable = min(max(gap - policy.spill_guard, 0.0), policy.max_spill)
    return hard, hard + usable


def fit_segment(
    duration: float,
    hard: float,
    spill: float,
    policy: FitPolicy,
    *,
    text_length: int = 0,
    can_shorten: bool = True,
) -> FitDecision:
    """Decide how a recording of `duration` is made to fit its cue.

    `policy.prefer` picks which of the first two strategies is tried first, and
    that choice is visible in the numbers: on a 20-line sample, preferring speed
    sped 8 lines up and spilled none, because a line that could have run into
    real silence was tightened against its subtitle instead.

    Pure arithmetic on purpose: no audio, no provider, no filesystem — which is
    what makes the awkward cases (a cue with no gap after it, a line twice too
    long, a 1% overrun) cheap enough to all be covered by tests.
    """

    if duration <= hard:
        return FitDecision(FIT_EXACT)

    if policy.prefer == PREFER_NATURAL:
        # Spend the silence first and only then touch the delivery, so a line
        # with room after it is never sped up at all.
        if duration <= spill:
            return FitDecision(FIT_SPILL)
    else:
        ratio_hard = duration / hard
        if ratio_hard <= policy.max_speedup:
            if ratio_hard < policy.min_speedup and duration <= spill:
                return FitDecision(FIT_SPILL)
            return FitDecision(FIT_SPED_UP, tempo=ratio_hard)
        if duration <= spill:
            return FitDecision(FIT_SPILL)

    # Both preferences meet here: the line needs the silence *and* a speed-up.
    ratio_spill = duration / spill
    if ratio_spill < policy.min_speedup:
        # Over by a fraction of a percent. Re-encoding it would be audible work
        # for an inaudible gain, so it simply runs a few milliseconds long.
        return FitDecision(FIT_SPILL)
    if ratio_spill <= policy.max_speedup:
        return FitDecision(FIT_SPED_UP, tempo=ratio_spill)

    if can_shorten and text_length:
        # What the line would have to shrink to, given it will also be sped up
        # to the limit afterwards. Asking for less than that shortens twice over.
        room = spill * policy.max_speedup
        target = max(policy.min_chars, int(text_length * (room / duration) * policy.shorten_margin))
        if target < text_length:
            return FitDecision(FIT_OVERFLOW, tempo=policy.max_speedup, target_chars=target)

    return FitDecision(FIT_OVERFLOW, tempo=policy.max_speedup)


# Bumped whenever what we *do* to a rendered line changes — trimming, sample
# rate, anything downstream of the provider. Without it a cache written by the
# previous version quietly keeps answering for audio this version would not have
# produced.
RENDER_VERSION = 2


def cache_key(provider: str, voice: str, text: str) -> str:
    """What makes two renders the same render. Nothing else may change the audio."""

    seed = f"{provider}|{voice}|{RENDER_VERSION}|{text}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def _render_one(
    text: str,
    *,
    cache_dir: Path,
    provider: str,
    voice: str,
) -> tuple[bytes, bool]:
    """Synthesise one line, or read back the PCM of a line already rendered.

    Only the decoded PCM is kept. The provider's own mp3 is deleted once it has
    been decoded: it is the slow part that is worth caching, not the container.
    """

    raw_path = cache_dir / f"seg_{cache_key(provider, voice, text)}.raw"
    if raw_path.exists() and raw_path.stat().st_size:
        return raw_path.read_bytes(), True

    audio_path = tts.synthesize(text, voice, raw_path.with_suffix(""), provider=provider)
    try:
        pcm = trim_silence(decode_to_pcm(audio_path))
    finally:
        audio_path.unlink(missing_ok=True)
    if not pcm:
        raise tts.TTSProviderError("err.tts.emptyAudio")
    raw_path.write_bytes(pcm)
    return pcm, False


def _render_many(
    items: list[tuple[int, str]],
    *,
    cache_dir: Path,
    provider: str,
    voice: str,
    concurrency: int,
    stop_check: Callable[[], None] | None,
    on_done: Callable[[int, bool], None] | None = None,
) -> tuple[dict[int, bytes], dict[int, str]]:
    """Render lines in parallel, keeping whatever succeeds.

    One line the provider refuses must not cost the other nine hundred: failures
    are collected and reported, and the cue is left silent. The cache means the
    retry that follows only pays for what actually failed.
    """

    rendered: dict[int, bytes] = {}
    failures: dict[int, str] = {}

    def work(item: tuple[int, str]) -> tuple[int, bytes | None, bool, str]:
        index, text = item
        # Pool threads are not the thread the runner registered its stop check
        # on, so this hands it over for the length of the call — nested waits
        # inside a provider then stop for the same reason everything else does.
        if stop_check is not None:
            set_stop_check(stop_check)
            stop_check()
        try:
            pcm, cached = _render_one(
                text, cache_dir=cache_dir, provider=provider, voice=voice
            )
            return index, pcm, cached, ""
        except OperationCancelled:
            raise
        except Exception as exc:
            logger.warning("dub: cue %s could not be voiced: %s", index, exc)
            return index, None, False, str(exc)
        finally:
            if stop_check is not None:
                clear_stop_check()

    with ThreadPoolExecutor(
        max_workers=max(1, concurrency), thread_name_prefix="autocc-tts"
    ) as pool:
        for index, pcm, cached, error in pool.map(work, items):
            if pcm is None:
                failures[index] = error
            else:
                rendered[index] = pcm
            if on_done is not None:
                on_done(index, cached)

    return rendered, failures


def _write_silence(handle, frames: int, sample_rate: int) -> None:
    """Pad with silence a second at a time, so a long gap is not one allocation."""

    remaining = max(0, frames)
    while remaining > 0:
        size = min(sample_rate, remaining)
        handle.writeframes(b"\x00" * (size * DUB_SAMPLE_WIDTH))
        remaining -= size


def assemble_track(
    segments: list[tuple[float, bytes]],
    destination: Path,
    *,
    sample_rate: int = DUB_SAMPLE_RATE,
    tail_seconds: float = 0.0,
) -> tuple[Path, float]:
    """Lay every segment at its own timestamp and return the track and its drift.

    Written forwards in one pass: a segment that would start before the previous
    one has finished is pushed to just after it rather than mixed over it. Two
    voices talking over each other is worse than a line arriving late, and the
    fitting stage has already made that case rare. How late the worst one is is
    returned, because it is the one number that says whether a run went wrong.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    cursor = 0
    max_drift = 0.0
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(DUB_SAMPLE_WIDTH)
        handle.setframerate(sample_rate)
        for start, pcm in sorted(segments, key=lambda item: item[0]):
            at = max(0, round(start * sample_rate))
            if at > cursor:
                _write_silence(handle, at - cursor, sample_rate)
                cursor = at
            elif at < cursor:
                max_drift = max(max_drift, (cursor - at) / sample_rate)
            handle.writeframes(pcm)
            cursor += len(pcm) // DUB_SAMPLE_WIDTH
        tail_frames = round(max(0.0, tail_seconds) * sample_rate)
        if tail_frames > cursor:
            _write_silence(handle, tail_frames - cursor, sample_rate)
    return destination, max_drift


def prune_cache(cache_dir: Path, keep: set[str]) -> int:
    """Drop rendered segments this dub no longer uses.

    A line the user rewrote, a voice they moved away from and a render version
    that has been superseded all leave segments behind that nothing will ask for
    again — roughly 48 KB per second of speech each, which on a feature-length
    project is the largest thing in the workspace after the video itself.
    """

    removed = 0
    for segment in cache_dir.glob("seg_*.raw"):
        if segment.stem.removeprefix("seg_") in keep:
            continue
        try:
            segment.unlink()
            removed += 1
        except OSError:
            # Housekeeping is never worth failing a finished dub over.
            logger.info("dub: could not remove stale segment %s", segment.name)
    return removed


def dub_cues(
    cues: list[dict],
    *,
    job_dir: Path,
    voice: str,
    provider: str,
    policy: FitPolicy | None = None,
    target_language: str | None = None,
    shorten: bool | None = None,
    llm_model: str | None = None,
    concurrency: int | None = None,
    on_progress: ProgressCallback | None = None,
    stop_check: Callable[[], None] | None = None,
) -> tuple[Path, dict]:
    """Voice every cue and write one track for the whole project.

    Returns the track and a report: how each line ended up fitting, how many
    were rewritten, how many the provider refused, and the worst timing drift.
    """

    policy = policy or policy_from_settings()
    allow_shortening = settings.dub_shorten_with_llm if shorten is None else bool(shorten)
    workers = concurrency or settings.tts_concurrency
    cache_dir = job_dir / CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    texts: dict[int, str] = {}
    for index, cue in enumerate(cues):
        spoken = dub_text(cue)
        if spoken:
            texts[index] = spoken
    if not texts:
        raise DubbingError("err.dub.nothingToVoice")

    total = len(texts)
    done = 0
    cached_count = 0

    def note(_index: int, cached: bool) -> None:
        nonlocal done, cached_count
        done += 1
        if cached:
            cached_count += 1
        if on_progress is not None:
            on_progress(
                done, total, Message("progress.dubbing", {"done": done, "total": total})
            )

    rendered, failures = _render_many(
        sorted(texts.items()),
        cache_dir=cache_dir,
        provider=provider,
        voice=voice,
        concurrency=workers,
        stop_check=stop_check,
        on_done=note,
    )
    if not rendered:
        raise DubbingError("err.dub.allVoicesFailed", failed=len(failures))

    decisions: dict[int, FitDecision] = {}
    pending: list[int] = []
    for index in sorted(rendered):
        hard, spill = budget_for(cues, index, policy)
        decision = fit_segment(
            pcm_seconds(rendered[index]),
            hard,
            spill,
            policy,
            text_length=len(texts[index]),
            can_shorten=allow_shortening,
        )
        decisions[index] = decision
        if decision.target_chars is not None:
            pending.append(index)

    shortened: set[int] = set()
    if pending and allow_shortening:
        shortened = _shorten_and_refit(
            cues=cues,
            texts=texts,
            rendered=rendered,
            decisions=decisions,
            pending=pending,
            policy=policy,
            cache_dir=cache_dir,
            provider=provider,
            voice=voice,
            workers=workers,
            target_language=target_language,
            llm_model=llm_model,
            stop_check=stop_check,
        )

    if stop_check is not None:
        stop_check()
    if on_progress is not None:
        on_progress(total, total, Message("progress.dubAssembling"))

    segments: list[tuple[float, bytes]] = []
    fits = {FIT_EXACT: 0, FIT_SPED_UP: 0, FIT_SPILL: 0, FIT_OVERFLOW: 0}
    for index in sorted(rendered):
        decision = decisions[index]
        fits[decision.fit] = fits.get(decision.fit, 0) + 1
        pcm = rendered[index]
        if decision.tempo > 1.0 + 1e-3:
            pcm = retime_pcm(pcm, tempo=decision.tempo)
        segments.append((float(cues[index].get("start", 0.0)), pcm))

    tail = max((float(cue.get("end", 0.0)) for cue in cues), default=0.0)
    track_path, drift = assemble_track(
        segments, cache_dir / TRACK_NAME, tail_seconds=tail
    )
    pruned = prune_cache(
        cache_dir,
        {cache_key(provider, voice, texts[index]) for index in rendered},
    )

    report = {
        "total_cues": len(cues),
        "voiced_cues": len(rendered),
        "failed_cues": len(failures),
        "cached_cues": cached_count,
        "shortened_cues": len(shortened),
        "fits": fits,
        "max_drift_seconds": round(drift, 3),
        "track_seconds": round(tail, 3),
        "pruned_segments": pruned,
        "prefer": policy.prefer,
        "voice": voice,
        "provider": provider,
    }
    logger.info("dub finished: %s", report)
    return track_path, report


def _shorten_and_refit(
    *,
    cues: list[dict],
    texts: dict[int, str],
    rendered: dict[int, bytes],
    decisions: dict[int, FitDecision],
    pending: list[int],
    policy: FitPolicy,
    cache_dir: Path,
    provider: str,
    voice: str,
    workers: int,
    target_language: str | None,
    llm_model: str | None,
    stop_check: Callable[[], None] | None,
) -> set[int]:
    """Rewrite the lines that will not fit, re-record them, and re-measure.

    Imported inside the function rather than at module scope: `ai` pulls in the
    whole translation stack, and a run with shortening turned off — or a test
    using the mock voice — has no reason to pay for it.
    """

    from .ai import shorten_for_dubbing

    shortened: set[int] = set()
    for attempt in range(policy.shorten_attempts):
        if not pending:
            break
        if stop_check is not None:
            stop_check()
        requests = [
            {"id": index, "text": texts[index], "max_chars": decisions[index].target_chars}
            for index in pending
            if decisions[index].target_chars is not None
        ]
        if not requests:
            break
        try:
            replacements = shorten_for_dubbing(requests, target_language, model=llm_model)
        except OperationCancelled:
            raise
        except Exception as exc:
            # The lines stay as they are and get sped up to the limit instead.
            # A dub with a few rushed lines beats no dub at all.
            logger.warning("dub: shortening pass %s failed: %s", attempt + 1, exc)
            break

        changed = {
            index: value
            for index, value in replacements.items()
            if index in texts and value and value != texts[index]
        }
        if not changed:
            break
        for index, value in changed.items():
            texts[index] = value
            shortened.add(index)

        refreshed, _failed = _render_many(
            sorted(changed.items()),
            cache_dir=cache_dir,
            provider=provider,
            voice=voice,
            concurrency=workers,
            stop_check=stop_check,
        )
        still_long: list[int] = []
        for index, pcm in refreshed.items():
            rendered[index] = pcm
            hard, spill = budget_for(cues, index, policy)
            decision = fit_segment(
                pcm_seconds(pcm),
                hard,
                spill,
                policy,
                text_length=len(texts[index]),
                # The last attempt takes what it gets: another rewrite would
                # only trade more meaning away for the same overflow.
                can_shorten=attempt + 1 < policy.shorten_attempts,
            )
            decisions[index] = decision
            if decision.target_chars is not None:
                still_long.append(index)
        pending = still_long

    return shortened
