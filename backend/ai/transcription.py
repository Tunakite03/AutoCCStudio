"""Speech-to-text: local faster-whisper, or the hosted Deepgram API."""

from __future__ import annotations

import mimetypes
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from .. import httpclient
from ..cancellation import OperationCancelled
from ..config import settings
from ..media import extract_transcription_audio, media_duration_seconds
from ..messages import Message
from ..subtitles import (
    CJK_CLAUSE_ENDERS,
    CJK_SENTENCE_ENDERS,
    CueStyle,
    balance_lines,
    enforce_cue_timing,
    join_tokens,
    merge_short_cues,
    split_long_cue,
    style_for_text,
)
from .shared import AIProviderError, ProgressCallback, _report


@lru_cache(maxsize=max(1, settings.whisper_model_cache))
def _whisper_model(model_size: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AIProviderError("err.ai.whisperMissing") from exc
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def _is_sentence_ender(token: str) -> bool:
    token = token.strip()
    if not token:
        return False
    if re.search(r"[.?!…\"'”’]+$", token):
        if re.search(r"\d\.\d+$", token):
            return False
        return True
    return token[-1] in set(CJK_SENTENCE_ENDERS)


def _is_clause_ender(token: str) -> bool:
    token = token.strip()
    if not token:
        return False
    return token[-1] in ({",", ";", ":", "—", "–"} | set(CJK_CLAUSE_ENDERS))


def _record_token(record: dict) -> str:
    return str(record.get("token") or record.get("raw_word") or "").strip()


def _records_style(records: list[dict]) -> CueStyle:
    """Pick the reading budget from a sample of what was actually recognised."""

    return style_for_text(join_tokens(_record_token(record) for record in records[:400]))


def _segment_words_into_cues(
    records: list[dict],
    media_duration: float | None = None,
    style: CueStyle | None = None,
) -> list[dict]:
    """Segment word records into cues that are readable at their own length.

    Three passes, because one is not enough. The split pass cuts on the best
    boundary it can reach before the budget runs out; the merge pass puts back
    together the fragments a recogniser handed over pre-chopped; the timing pass
    gives whatever is left a duration a human can actually read. Doing only the
    first — which is what this used to do — produces exactly the transcript that
    prompted the rewrite: half-sentence cues on screen for 0.6 seconds.

    The budget comes from the script: 42 characters of English and 42 characters
    of Chinese are not the same subtitle.
    """

    if not records:
        return []

    active = style or _records_style(records)

    cues: list[dict] = []
    current_words: list[dict] = []
    # break_scores[i] rates the boundary *after* current_words[i]: how natural a
    # place it is to end a cue. Filled in as the next word arrives, because the
    # silence after a word is only known once there is a next word.
    break_scores: list[float] = []

    def group_text(words: list[dict]) -> str:
        return join_tokens(_record_token(word) for word in words)

    def flush_group(words: list[dict]):
        if not words:
            return
        start = max(float(words[0]["start"]), 0.0)
        end = max(float(words[-1]["end"]), start + 0.12)
        if media_duration is not None:
            start = min(start, media_duration)
            end = min(end, media_duration)
        if end <= start:
            return

        clean_text = balance_lines(
            group_text(words),
            max_line_len=active.max_line_chars,
            max_lines=active.max_lines,
        )
        if not clean_text:
            return

        cues.append(
            {
                "id": len(cues) + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": clean_text,
                "translation": "",
                "speaker": words[0].get("speaker"),
            }
        )

    def best_break_index() -> int | None:
        """The nicest boundary inside the current group that can carry a cue.

        Without this, an overflowing group is cut wherever the budget happens to
        run out — mid-phrase, and in Chinese mid-word. Reaching back to the
        pause or the comma a few syllables earlier costs nothing and is the
        difference between a cue that reads and one that does not.
        """

        group_start = float(current_words[0]["start"])
        best_index: int | None = None
        best_score = 0.0
        for index in range(len(current_words) - 1):
            left = current_words[: index + 1]
            left_text = group_text(left)
            left_duration = float(left[-1]["end"]) - group_start
            if active.too_short(left_text, left_duration):
                continue
            if not active.fits(left_text, left_duration):
                # Every later index is longer still, so nothing beyond fits.
                break
            # Nudge towards the last usable boundary so cues stay full.
            score = break_scores[index] + (index / len(current_words)) * 5
            if score > best_score:
                best_score, best_index = score, index
        # A score this low means no real boundary was found, only the positional
        # nudge — in that case the caller's own cut point is no worse.
        return best_index if best_score >= 10 else None

    for word in records:
        if not current_words:
            current_words.append(word)
            break_scores.append(0.0)
            continue

        prev_word = current_words[-1]
        group_start = float(current_words[0]["start"])
        group_end = float(prev_word["end"])
        group_duration = group_end - group_start

        curr_start = float(word.get("start", group_end))
        curr_end = float(word.get("end", curr_start))

        silence_gap = max(0.0, curr_start - group_end)
        speaker_changed = (
            word.get("speaker") is not None
            and prev_word.get("speaker") is not None
            and word["speaker"] != prev_word["speaker"]
        )

        prev_token = _record_token(prev_word)
        prev_is_sentence = _is_sentence_ender(prev_token)
        prev_is_clause = _is_clause_ender(prev_token)

        # Now that the following word is known, rate the boundary behind it.
        break_scores[-1] = (
            (100.0 if prev_is_sentence else 60.0 if prev_is_clause else 0.0)
            + min(silence_gap, 1.0) * 40.0
        )

        projected_len = len(group_text(current_words + [word]))
        projected_duration = curr_end - group_start
        # Splitting a group that is already below the floor only creates two
        # unreadable cues instead of one, so a soft boundary has to wait.
        group_is_fragment = active.too_short(group_text(current_words), group_duration)
        overflows = projected_len > active.max_chars or projected_duration > active.max_duration

        if speaker_changed or overflows:
            should_split = True
        elif silence_gap >= active.split_gap and not group_is_fragment:
            should_split = True
        elif prev_is_sentence and not group_is_fragment:
            should_split = True
        elif prev_is_clause and not group_is_fragment and (
            projected_len > active.max_chars * 0.75
            or projected_duration > active.max_duration * 0.75
        ):
            should_split = True
        else:
            should_split = False

        if not should_split:
            current_words.append(word)
            break_scores.append(0.0)
            continue

        # A budget overflow has no opinion about where the sentence breaks, so
        # it defers to the best boundary already passed. A speaker change does.
        cut = None if speaker_changed else best_break_index() if overflows else None
        if cut is None:
            flush_group(current_words)
            current_words, break_scores = [word], [0.0]
        else:
            flush_group(current_words[: cut + 1])
            current_words = current_words[cut + 1 :] + [word]
            break_scores = break_scores[cut + 1 :] + [0.0]

    if current_words:
        flush_group(current_words)

    cues = merge_short_cues(cues, style=active)
    cues = enforce_cue_timing(cues, style=active, media_duration=media_duration)

    for index, cue in enumerate(cues, start=1):
        cue["id"] = index

    return cues


def transcribe_video(
    video_path: Path,
    model_size: str | None = None,
    language: str | None = None,
    provider: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[dict], str | None]:
    transcription_provider = (provider or settings.transcription_provider).strip().lower()
    if transcription_provider == "deepgram":
        return transcribe_video_deepgram(
            video_path,
            language=language,
            model=model_size,
            on_progress=on_progress,
        )
    if transcription_provider not in {"faster_whisper", "whisper"}:
        raise AIProviderError("err.ai.badTranscriptionProvider")

    model = _whisper_model(
        model_size or settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
    )
    whisper_lang: str | None = None
    if language and language.lower() not in {"auto", "multi"}:
        whisper_lang = language.split("-")[0].lower()

    _report(on_progress, 0, None, Message("progress.loadingModel"))
    try:
        media_duration = media_duration_seconds(video_path)
        try:
            segments, info = model.transcribe(
                str(video_path),
                language=whisper_lang,
                vad_filter=True,
                beam_size=5,
                word_timestamps=True,
            )
        except (TypeError, ValueError):
            segments, info = model.transcribe(
                str(video_path),
                language=whisper_lang,
                vad_filter=True,
                beam_size=5,
            )
        cues = []
        # Words are pooled across segments rather than segmented one segment at
        # a time: a Whisper segment boundary is a decoding artefact, and cutting
        # there means a two-word segment can never rejoin the sentence it
        # belongs to.
        pending_records: list[dict] = []

        def flush_records():
            if pending_records:
                cues.extend(
                    _segment_words_into_cues(pending_records, media_duration=media_duration)
                )
                pending_records.clear()

        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            start = max(float(segment.start), 0.0)
            end = max(float(segment.end), start)
            if media_duration is not None:
                start = min(start, media_duration)
                end = min(end, media_duration)
            if end <= start:
                continue

            # faster-whisper decodes lazily, so the segment we just consumed is a
            # truthful position along the media timeline.
            _report(
                on_progress,
                int(end),
                int(media_duration) if media_duration else None,
                Message("progress.transcribing"),
            )

            words = getattr(segment, "words", None)
            records = []
            if words:
                for w in words:
                    token = getattr(w, "word", "").strip()
                    if not token:
                        continue
                    w_start = max(float(getattr(w, "start", start)), 0.0)
                    w_end = max(float(getattr(w, "end", w_start)), w_start)
                    records.append(
                        {
                            "token": token,
                            "raw_word": token,
                            "start": w_start,
                            "end": w_end,
                            "speaker": None,
                            "confidence": _optional_confidence(getattr(w, "probability", None)),
                        }
                    )
            if records:
                pending_records.extend(records)
                continue

            # No word timings for this segment, so it cannot join the pool.
            flush_records()
            cue_item = {
                "id": 0,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "translation": "",
            }
            cues.extend(split_long_cue(cue_item))

        flush_records()

        for index, cue in enumerate(cues, start=1):
            cue["id"] = index
    except OperationCancelled:
        # `on_progress` stops the run from inside the decoding loop. That is not
        # a Whisper failure and must not be relabelled as one.
        raise
    except Exception as exc:  # provider errors vary by media/model backend
        raise AIProviderError("err.ai.whisperFailed", cause=str(exc)) from exc
    return cues, getattr(info, "language", None)


MEDIA_MIME_TYPES = {
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
}


def _upload_content_type(upload_path: Path) -> str:
    return (
        MEDIA_MIME_TYPES.get(upload_path.suffix.lower())
        or mimetypes.guess_type(upload_path.name)[0]
        or "application/octet-stream"
    )


def _request_deepgram(
    video_path: Path,
    params: dict[str, str],
    on_progress: ProgressCallback | None = None,
) -> dict:
    if not settings.deepgram_api_key.strip():
        raise AIProviderError("err.ai.deepgramKeyMissing")

    url = f"{settings.deepgram_base_url.rstrip('/')}/v1/listen?{urlencode(params)}"
    _report(on_progress, 0, None, Message("progress.extractingAudio"))
    extracted_audio = extract_transcription_audio(video_path)
    upload_path = extracted_audio if extracted_audio is not None else video_path

    # A retry cannot reuse a consumed file handle, so each attempt opens its own.
    handles = []

    def open_media():
        handle = upload_path.open("rb")
        handles.append(handle)
        return handle

    _report(on_progress, 0, None, Message("progress.uploadingAudio", {"provider": "Deepgram"}))
    try:
        response = httpclient.post(
            url,
            headers={
                "Authorization": f"Token {settings.deepgram_api_key.strip()}",
                "Content-Type": _upload_content_type(upload_path),
            },
            timeout=(30, settings.deepgram_timeout_seconds),
            label="Deepgram",
            body_factory=open_media,
        )
        payload = httpclient.json_body(response, "Deepgram")
    except httpclient.HTTPClientError as exc:
        # Adopted, not restated: the HTTP layer already knows exactly what failed.
        raise AIProviderError(exc.message) from exc
    finally:
        for handle in handles:
            handle.close()
        if extracted_audio is not None:
            extracted_audio.unlink(missing_ok=True)

    if not isinstance(payload, dict):
        raise AIProviderError("err.ai.deepgramBadJson")
    _report(on_progress, 0, None, Message("progress.processingResult", {"provider": "Deepgram"}))
    return payload


def _deepgram_detected_language(payload: dict, fallback: str | None) -> str | None:
    try:
        detected = payload["results"]["channels"][0].get("detected_language")
    except (KeyError, IndexError, TypeError, AttributeError):
        detected = None
    return str(detected).strip() if detected else fallback


def _optional_speaker_id(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_confidence(value) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return round(min(max(confidence, 0.0), 1.0), 3)


def _find_token_start(transcript: str, token: str, cursor: int) -> tuple[int, int] | None:
    token = token.strip()
    if not token:
        return None
    match = re.search(
        rf"(?<!\w){re.escape(token)}(?!\w)",
        transcript[cursor:],
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return cursor + match.start(), cursor + match.end()


def _deepgram_word_turns(
    utterance: dict,
    transcript: str,
    fallback_speaker: int | None,
) -> tuple[str, list[dict]]:
    """Insert line breaks using Deepgram's per-word speaker evidence."""

    words = utterance.get("words")
    if not isinstance(words, list) or not words:
        return transcript, []

    records = []
    previous_speaker = fallback_speaker
    for word in words:
        if not isinstance(word, dict):
            continue
        token = str(word.get("punctuated_word") or word.get("word") or "").strip()
        if not token:
            continue
        speaker = _optional_speaker_id(word.get("speaker"))
        if speaker is None:
            speaker = previous_speaker
        if speaker is not None:
            previous_speaker = speaker
        records.append(
            {
                "token": token,
                "raw_word": str(word.get("word") or "").strip(),
                "speaker": speaker,
                "confidence": _optional_confidence(word.get("speaker_confidence")),
            }
        )
    if not records:
        return transcript, []

    runs = []
    for record in records:
        if not runs or runs[-1]["speaker"] != record["speaker"]:
            runs.append(
                {
                    "speaker": record["speaker"],
                    "records": [record],
                }
            )
        else:
            runs[-1]["records"].append(record)

    boundaries = []
    cursor = 0
    mapped = True
    previous = records[0]["speaker"]
    for record in records:
        span = _find_token_start(transcript, record["token"], cursor)
        if span is None and record["raw_word"] != record["token"]:
            span = _find_token_start(transcript, record["raw_word"], cursor)
        if span is None:
            mapped = False
            break
        start, cursor = span
        if record["speaker"] is not None and record["speaker"] != previous:
            boundaries.append(start)
        if record["speaker"] is not None:
            previous = record["speaker"]

    if mapped:
        layout = transcript
        for boundary in reversed(boundaries):
            layout = f"{layout[:boundary].rstrip()}\n{layout[boundary:].lstrip()}"
    else:
        reconstructed = " ".join(record["token"] for record in records)
        collapse = lambda value: re.sub(r"\s+", " ", value).strip()
        if collapse(reconstructed) != collapse(transcript):
            return transcript, []
        layout = "\n".join(
            " ".join(record["token"] for record in run["records"])
            for run in runs
        )

    lines = layout.splitlines()
    if len(lines) != len(runs):
        return transcript, []

    turns = []
    for run, line in zip(runs, lines):
        confidences = [
            record["confidence"]
            for record in run["records"]
            if record["confidence"] is not None
        ]
        turns.append(
            {
                "speaker": run["speaker"],
                "confidence": (
                    round(sum(confidences) / len(confidences), 3)
                    if confidences
                    else None
                ),
                "text": line.strip(),
            }
        )
    return layout, turns


def _deepgram_cues(payload: dict, media_duration: float | None) -> list[dict]:
    try:
        utterances = payload["results"]["utterances"]
    except (KeyError, TypeError) as exc:
        raise AIProviderError("err.ai.deepgramNoUtterances") from exc
    if not isinstance(utterances, list):
        raise AIProviderError("err.ai.deepgramBadUtterances")

    cues: list[dict] = []
    # Deepgram ends an utterance at a pause *or* at a smart-formatted sentence
    # end, and for Chinese that lands on almost every clause. Segmenting each
    # utterance on its own therefore froze those cuts in place: "看清楚" and
    # "了吧" could never become one cue however short they were. Pooling the
    # words first lets the segmenter judge the whole timeline, and the speaker
    # id on each word still keeps two people apart.
    pending_records: list[dict] = []

    def flush_records():
        if not pending_records:
            return
        sub_cues = _segment_words_into_cues(pending_records, media_duration=media_duration)
        for sub_cue in sub_cues:
            sub_cue["speaker_analysis_source"] = "deepgram_words"
        cues.extend(sub_cues)
        pending_records.clear()

    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        transcript = str(utterance.get("transcript", "")).strip()
        if not transcript:
            continue
        try:
            start = max(float(utterance["start"]), 0.0)
            end = max(float(utterance["end"]), start)
        except (KeyError, TypeError, ValueError):
            continue
        if media_duration is not None:
            start = min(start, media_duration)
            end = min(end, media_duration)
        if end <= start:
            continue

        words = utterance.get("words")
        speaker_id = _optional_speaker_id(utterance.get("speaker"))

        records = []
        if isinstance(words, list) and words:
            previous_speaker = speaker_id
            for word in words:
                if not isinstance(word, dict):
                    continue
                token = str(word.get("punctuated_word") or word.get("word") or "").strip()
                if not token:
                    continue
                speaker = _optional_speaker_id(word.get("speaker"))
                if speaker is None:
                    speaker = previous_speaker
                if speaker is not None:
                    previous_speaker = speaker
                w_start = max(float(word.get("start", start)), 0.0)
                w_end = max(float(word.get("end", w_start)), w_start)
                records.append(
                    {
                        "token": token,
                        "raw_word": str(word.get("word") or "").strip(),
                        "start": w_start,
                        "end": w_end,
                        "speaker": speaker,
                        "confidence": _optional_confidence(word.get("speaker_confidence")),
                    }
                )
        if records:
            pending_records.extend(records)
            continue

        # An utterance without word timings has no place in the pool, so
        # whatever is pooled has to be emitted before it to keep cues in order.
        flush_records()
        cue_item = {
            "id": 0,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": transcript,
            "translation": "",
            "speaker": speaker_id,
            "speaker_analysis_source": "deepgram_utterance",
        }
        cues.extend(split_long_cue(cue_item))

    flush_records()

    for index, cue in enumerate(cues, start=1):
        cue["id"] = index

    return cues


def transcribe_video_deepgram(
    video_path: Path,
    language: str | None = None,
    model: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[dict], str | None]:
    selected_model = (model or settings.deepgram_model).strip()
    if not selected_model:
        raise AIProviderError("err.ai.deepgramModelMissing")
    params = {
        "model": selected_model,
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        "diarize_model": settings.deepgram_diarize_model,
    }
    if language:
        params["language"] = language
    else:
        params["detect_language"] = "true"

    payload = _request_deepgram(video_path, params, on_progress=on_progress)
    cues = _deepgram_cues(payload, media_duration_seconds(video_path))
    if not cues:
        raise AIProviderError("err.ai.deepgramNoSpeech")
    return cues, _deepgram_detected_language(payload, language)
