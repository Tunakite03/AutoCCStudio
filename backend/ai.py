"""AI adapters used by AutoCC.

Transcription is local through faster-whisper. Translation is deliberately
implemented against the common OpenAI-compatible chat-completions shape so it
can target Ollama, LM Studio, or a hosted endpoint without changing the app.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from . import httpclient
from .apikeys import CredentialPool
from .cancellation import OperationCancelled
from .config import get_logger, settings
from .translation_style import STYLE_AUTO, StyleBrief, build_style_brief
from .media import extract_transcription_audio, media_duration_seconds
from .subtitles import (
    CJK_CLAUSE_ENDERS,
    CJK_SENTENCE_ENDERS,
    CueStyle,
    balance_lines,
    enforce_cue_timing,
    join_tokens,
    merge_short_cues,
    split_long_cue,
    split_long_cues,
    strip_speaker_labels,
    style_for_text,
)


class AIProviderError(RuntimeError):
    """Raised when an AI provider is unavailable or returns invalid output."""


class AIResponseFormatError(AIProviderError):
    """Raised when an AI provider responds but violates the requested JSON shape."""


logger = get_logger("ai")

# Reports (done, total_or_None, message) as a phase advances.
ProgressCallback = Callable[[int, int | None, str], None]


def _report(on_progress: ProgressCallback | None, current: int, total: int | None, message: str) -> None:
    if on_progress is not None:
        on_progress(current, total, message)


@lru_cache(maxsize=max(1, settings.whisper_model_cache))
def _whisper_model(model_size: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AIProviderError(
            "Chưa cài faster-whisper. Chạy: pip install -r requirements.txt"
        ) from exc
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
        raise AIProviderError(
            "TRANSCRIPTION_PROVIDER phải là faster_whisper hoặc deepgram"
        )

    model = _whisper_model(
        model_size or settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
    )
    whisper_lang: str | None = None
    if language and language.lower() not in {"auto", "multi"}:
        whisper_lang = language.split("-")[0].lower()

    _report(on_progress, 0, None, "Đang nạp model nhận dạng")
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
                "Đang nhận dạng lời thoại",
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
        raise AIProviderError(f"Whisper không thể xử lý video: {exc}") from exc
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
        raise AIProviderError(
            "Chưa cấu hình DEEPGRAM_API_KEY. Thêm key vào file .env rồi khởi động lại app."
        )

    url = f"{settings.deepgram_base_url.rstrip('/')}/v1/listen?{urlencode(params)}"
    _report(on_progress, 0, None, "Đang tách audio để tải lên")
    extracted_audio = extract_transcription_audio(video_path)
    upload_path = extracted_audio if extracted_audio is not None else video_path

    # A retry cannot reuse a consumed file handle, so each attempt opens its own.
    handles = []

    def open_media():
        handle = upload_path.open("rb")
        handles.append(handle)
        return handle

    _report(on_progress, 0, None, "Đang gửi audio tới Deepgram")
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
        raise AIProviderError(str(exc)) from exc
    finally:
        for handle in handles:
            handle.close()
        if extracted_audio is not None:
            extracted_audio.unlink(missing_ok=True)

    if not isinstance(payload, dict):
        raise AIProviderError("Deepgram trả về JSON không hợp lệ")
    _report(on_progress, 0, None, "Đang xử lý kết quả từ Deepgram")
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
        raise AIProviderError("Deepgram response thiếu results.utterances") from exc
    if not isinstance(utterances, list):
        raise AIProviderError("Deepgram response có utterances không hợp lệ")

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
        raise AIProviderError("Cần chọn model Deepgram trước khi nhận dạng")
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
        raise AIProviderError("Deepgram không tìm thấy lời thoại có timestamp trong video")
    return cues, _deepgram_detected_language(payload, language)


def _completion_url() -> str:
    base = settings.llm_base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


_llm_pace_lock = threading.Lock()
_llm_next_call_at = 0.0

_llm_pool_lock = threading.Lock()
_llm_pool: tuple[tuple[str, ...], CredentialPool] | None = None


def _llm_credentials() -> CredentialPool | None:
    """The shared key pool for the configured LLM keys, or None if there are none.

    Shared, and rebuilt only when the configured keys themselves change: the
    cooldowns are the whole value here. A pool created per call would forget
    which key was just rate limited and hand it straight back — the first batch
    of the next cue would walk into the same 429.
    """

    keys = settings.llm_api_keys
    if not keys:
        return None
    global _llm_pool
    with _llm_pool_lock:
        if _llm_pool is None or _llm_pool[0] != keys:
            logger.info("LLM key pool: %s key(s) configured", len(keys))
            _llm_pool = (keys, CredentialPool(keys))
        return _llm_pool[1]


def _wait_for_llm_slot() -> None:
    """Keep LLM_MIN_INTERVAL_SECONDS between outbound LLM calls.

    Hosted providers meter requests per second and a translation is a long burst
    of small calls, so the cheapest rate limit is the one never triggered. The
    sleep happens under the lock on purpose: two jobs translating at once have
    to queue behind each other, not each keep their own pace.
    """

    interval = max(0.0, settings.llm_min_interval_seconds)
    if not interval:
        return
    global _llm_next_call_at
    with _llm_pace_lock:
        wait = _llm_next_call_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _llm_next_call_at = time.monotonic() + interval


def _llm_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    operation: str,
    model: str | None = None,
) -> str:
    if not settings.llm_base_url.strip():
        raise AIProviderError(f"Chưa cấu hình LLM_BASE_URL cho {operation}")
    selected_model = (model or settings.llm_model).strip()
    if not selected_model:
        raise AIProviderError(f"Chưa cấu hình model LLM cho {operation}")

    _wait_for_llm_slot()
    try:
        response = httpclient.post(
            _completion_url(),
            headers={"Content-Type": "application/json"},
            # The pool authorises each attempt: which key goes out is decided
            # per request, because a retry after a 429 must not reuse the key
            # that was just told to slow down.
            credentials=_llm_credentials(),
            json_body={
                "model": selected_model,
                "temperature": temperature,
                "messages": messages,
            },
            timeout=(30, settings.llm_timeout_seconds),
            label="LLM",
        )
        body = httpclient.json_body(response, "LLM")
    except httpclient.HTTPClientError as exc:
        raise AIProviderError(f"{exc} (khi {operation})") from exc

    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError(
            f"Phản hồi LLM thiếu choices[0].message.content khi {operation}"
        ) from exc


def _resolve_transformers_device(value: str) -> int:
    normalized = value.strip().lower()
    if normalized == "auto":
        try:
            import torch

            return 0 if torch.cuda.is_available() else -1
        except ImportError:
            return -1
    if normalized == "cpu":
        return -1
    try:
        return int(normalized)
    except ValueError as exc:
        raise AIProviderError(
            "TRANSFORMERS_DEVICE phải là auto, cpu hoặc số GPU"
        ) from exc


@lru_cache(maxsize=2)
def _transformers_pipeline(model_name: str, device_name: str):
    if not model_name.strip():
        raise AIProviderError(
            "TRANSLATION_MODEL là bắt buộc khi TRANSLATION_PROVIDER=transformers"
        )
    try:
        from transformers import pipeline
    except ImportError as exc:
        raise AIProviderError(
            "Chưa cài transformers/torch cho local translation"
        ) from exc
    try:
        return pipeline(
            "translation",
            model=model_name,
            device=_resolve_transformers_device(device_name),
        )
    except Exception as exc:
        raise AIProviderError(f"Không tải được translation model {model_name}: {exc}") from exc


def _translate_batch_transformers(
    texts: list[str],
    target_language: str,
    *,
    model_name: str | None = None,
) -> list[str]:
    expected_language = settings.transformers_target_language.strip().casefold()
    if expected_language and target_language.strip().casefold() != expected_language:
        raise AIProviderError(
            "Translation model local hiện chỉ được cấu hình cho "
            f"{settings.transformers_target_language}"
        )
    chosen_model = (model_name or settings.translation_model).strip()
    if not chosen_model:
        raise AIProviderError(
            "TRANSLATION_MODEL là bắt buộc khi TRANSLATION_PROVIDER=transformers"
        )
    translator = _transformers_pipeline(
        chosen_model,
        settings.transformers_device,
    )
    try:
        results = translator(texts, max_length=512)
    except Exception as exc:
        raise AIProviderError(f"Local translation model không xử lý được cue: {exc}") from exc
    if isinstance(results, dict):
        results = [results]
    try:
        return [str(item["translation_text"]).strip() for item in results]
    except (KeyError, TypeError) as exc:
        raise AIProviderError("Local translation model trả về dữ liệu không hợp lệ") from exc


def _extract_json_value(content: str, error_subject: str = "Model"):
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    starts = [position for position in (cleaned.find("{"), cleaned.find("[")) if position >= 0]
    if starts:
        start = min(starts)
        closing = "}" if cleaned[start] == "{" else "]"
        end = cleaned.rfind(closing)
        if end > start:
            cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIResponseFormatError(
            f"{error_subject} không trả về JSON hợp lệ"
        ) from exc


def _clean_dialogue_layout(text: str) -> str:
    without_labels = strip_speaker_labels(str(text)).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.strip() for line in without_labels.split("\n") if line.strip())


def _same_dialogue_content(left: str, right: str) -> bool:
    collapse = lambda value: re.sub(r"\s+", " ", value).strip()
    return collapse(left) == collapse(right)


def _dialogue_break_positions(text: str) -> set[int]:
    positions = set()
    word_count = 0
    lines = _clean_dialogue_layout(text).splitlines()
    for line in lines[:-1]:
        word_count += len(re.findall(r"\S+", line))
        if word_count:
            positions.add(word_count)
    return positions


def _speaker_analysis_item(index: int, cue: dict) -> dict:
    return {
        "cue_id": index + 1,
        "start": round(float(cue.get("start", 0)), 3),
        "end": round(float(cue.get("end", 0)), 3),
        "speaker_hint": cue.get("speaker"),
        "acoustic_speaker_turns": cue.get("speaker_turns", []),
        "text": _clean_dialogue_layout(str(cue.get("text", ""))),
    }


def _extract_dialogue_map(
    content: str,
    targets: list[tuple[int, dict]],
) -> dict[int, str]:
    value = _extract_json_value(content, "AI phân tích lượt thoại")
    target_ids = [index + 1 for index, _cue in targets]
    if isinstance(value, dict) and "results" in value:
        value = value["results"]

    results: dict[int, str] = {}
    if isinstance(value, dict):
        for raw_id, text in value.items():
            try:
                cue_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if cue_id in target_ids and isinstance(text, str):
                results[cue_id] = text
        return results

    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        if len(value) == len(target_ids):
            return dict(zip(target_ids, value))
        if len(target_ids) == 1 and value:
            return {target_ids[0]: value[0]}
        return {}

    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("cue_id", item.get("id", item.get("index")))
            text = item.get("text")
            try:
                cue_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if cue_id in target_ids and isinstance(text, str):
                results[cue_id] = text
        return results

    raise AIResponseFormatError(
        "AI phân tích lượt thoại không trả về object theo cue_id"
    )


def _analyze_dialogue_batch(
    targets: list[tuple[int, dict]],
    context_before: list[tuple[int, dict]],
    context_after: list[tuple[int, dict]],
    language: str | None,
) -> dict[int, str]:
    request_data = {
        "language": language or "auto",
        "context_before": [
            _speaker_analysis_item(index, cue) for index, cue in context_before
        ],
        "targets": [_speaker_analysis_item(index, cue) for index, cue in targets],
        "context_after": [
            _speaker_analysis_item(index, cue) for index, cue in context_after
        ],
    }
    prompt = (
        "Phân tích lượt thoại trong trường targets của JSON bên dưới. Dựa vào câu hỏi/"
        "trả lời, đại từ, cách xưng hô và ngữ cảnh để nhận ra chỗ một người khác bắt đầu "
        "nói. acoustic_speaker_turns là bằng chứng theo từng từ từ audio và các xuống "
        "dòng có sẵn từ nguồn này phải được giữ nguyên. speaker_hint chỉ là gợi ý cho "
        "giọng chính hoặc đầu cue. Với mỗi target, trả lại nguyên văn và chỉ chèn thêm "
        "ký tự xuống dòng \\n tại ranh giới đổi người nói. Không thêm nhãn [1], [2], "
        "[S1], gạch đầu dòng hay tên nhân vật. Không thêm, xóa, sửa hoặc đảo bất kỳ từ, "
        "dấu câu hay ký tự nào khác. Nếu không chắc thì giữ nguyên. Chỉ trả về một JSON "
        "object có key là cue_id dạng chuỗi và value là lời thoại, ví dụ "
        '{"5":"Câu một.\\nCâu hai."}. Không bỏ sót, đổi hoặc thêm cue_id. '
        "Hai trường context chỉ để tham khảo.\n\n"
        + json.dumps(request_data, ensure_ascii=False)
    )
    content = _llm_completion(
        [
            {
                "role": "system",
                "content": (
                    "Bạn là bộ phân đoạn lượt thoại. Chỉ xuất JSON hợp lệ và tuyệt đối "
                    "không sửa nội dung lời thoại."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        operation="phân tích lượt thoại",
        model=settings.speaker_analysis_model or settings.llm_model,
    )
    return _extract_dialogue_map(content, targets)


def _validated_dialogue_proposal(original: str, result: str) -> tuple[str | None, str | None]:
    proposed = _clean_dialogue_layout(result)
    if not _same_dialogue_content(original, proposed):
        return None, "changed_content"
    if not _dialogue_break_positions(original).issubset(
        _dialogue_break_positions(proposed)
    ):
        return None, "removed_acoustic_break"
    return proposed, None


def analyze_dialogue_turns(
    cues: list[dict],
    language: str | None = None,
    batch_size: int = 5,
    return_report: bool = False,
    on_progress: ProgressCallback | None = None,
) -> list[dict] | tuple[list[dict], dict]:
    """Insert line breaks at inferred speaker changes without changing transcript text.

    One LLM round trip per batch, plus a retry per cue that comes back unusable,
    so a long transcript is a long sequence of calls — `on_progress` is how the
    caller keeps the UI honest about that.
    """

    empty_report = {
        "total_cues": 0,
        "acoustic_split_cues": 0,
        "ai_modified_cues": 0,
        "retried_cues": 0,
        "failed_cues": 0,
        "failed_cue_ids": [],
    }
    if not cues:
        return ([], empty_report) if return_report else []
    analyzed = []
    for cue in cues:
        cleaned = dict(cue)
        cleaned["text"] = _clean_dialogue_layout(str(cue.get("text", "")))
        cleaned["translation"] = strip_speaker_labels(
            str(cue.get("translation", ""))
        )
        analyzed.append(cleaned)

    report = {
        **empty_report,
        "total_cues": len(analyzed),
        "acoustic_split_cues": sum(
            1 for cue in analyzed if "\n" in str(cue.get("text", ""))
        ),
    }
    size = max(1, int(batch_size))
    _report(on_progress, 0, len(analyzed), "Đang phân tích lượt thoại")
    for offset in range(0, len(analyzed), size):
        end = min(offset + size, len(analyzed))
        targets = list(enumerate(analyzed[offset:end], start=offset))
        before_start = max(0, offset - 2)
        context_before = list(
            enumerate(analyzed[before_start:offset], start=before_start)
        )
        context_after = list(enumerate(analyzed[end : end + 2], start=end))
        try:
            results = _analyze_dialogue_batch(
                targets,
                context_before,
                context_after,
                language,
            )
        except AIResponseFormatError as exc:
            logger.warning(
                "dialogue batch %s-%s returned an unusable shape, falling back to "
                "per-cue retries: %s",
                offset + 1, end, exc,
            )
            results = {}

        for index, cue in targets:
            original = _clean_dialogue_layout(str(cue.get("text", "")))
            result = results.get(index + 1)
            proposed, failure = (
                _validated_dialogue_proposal(original, result)
                if result is not None
                else (None, "missing")
            )
            if proposed is None:
                report["retried_cues"] += 1
                before_start = max(0, index - 2)
                single_before = list(
                    enumerate(analyzed[before_start:index], start=before_start)
                )
                single_after = list(
                    enumerate(analyzed[index + 1 : index + 3], start=index + 1)
                )
                try:
                    retry = _analyze_dialogue_batch(
                        [(index, cue)],
                        single_before,
                        single_after,
                        language,
                    ).get(index + 1)
                except AIResponseFormatError as exc:
                    logger.warning("dialogue retry for cue %s failed: %s", index + 1, exc)
                    retry = None
                proposed, failure = (
                    _validated_dialogue_proposal(original, retry)
                    if retry is not None
                    else (None, failure)
                )

            if proposed is None:
                report["failed_cue_ids"].append(index + 1)
                cue["speaker_analysis_failure"] = failure
                continue
            if proposed != original:
                report["ai_modified_cues"] += 1
                source = str(cue.get("speaker_analysis_source") or "").strip()
                cue["speaker_analysis_source"] = (
                    f"{source}+llm" if source else "llm"
                )
            cue["text"] = proposed

        _report(on_progress, end, len(analyzed), "Đang phân tích lượt thoại")

    report["failed_cues"] = len(report["failed_cue_ids"])
    return (analyzed, report) if return_report else analyzed


TRANSLATION_MOCK = "mock"
TRANSLATION_TRANSFORMERS = "transformers"
TRANSLATION_OPENAI_COMPATIBLE = "openai_compatible"


def resolve_translation_provider(name: str) -> str:
    """Map a configured provider name onto the backend that will serve it.

    Anything that is not the mock or a local transformers pipeline is spoken to
    over the OpenAI-compatible chat API, so `TRANSLATION_PROVIDER=MistralAI` is
    a working configuration. This has to be the only place that decides, or the
    capabilities endpoint ends up reporting a provider as unconfigured while
    translation quietly works.
    """

    normalized = name.strip().lower()
    if normalized in {TRANSLATION_MOCK, TRANSLATION_TRANSFORMERS}:
        return normalized
    return TRANSLATION_OPENAI_COMPATIBLE


def _extract_translation_map(content, line_ids: list[int]) -> dict[int, str]:
    """Realign what came back with what was sent, by id.

    A bare JSON array is positional, so a model that merges two short lines into
    one sentence shifts every translation after it and the batch has to be
    thrown away. Ids survive that merge: the swallowed line simply comes back
    missing, and only that line needs retranslating.

    Takes either the raw response text or a value already decoded from it, so a
    reply carrying both translations and glossary updates is parsed once.
    """

    value = (
        _extract_json_value(content, "Model dịch")
        if isinstance(content, str)
        else content
    )
    if isinstance(value, dict):
        for key in ("translations", "results"):
            inner = value.get(key)
            if isinstance(inner, (dict, list)):
                value = inner
                break

    wanted = set(line_ids)
    results: dict[int, str] = {}
    if isinstance(value, dict):
        for raw_id, text in value.items():
            try:
                line_id = int(str(raw_id).strip())
            except (TypeError, ValueError):
                continue
            if line_id in wanted and isinstance(text, str):
                results[line_id] = text
        return results

    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            # Positional, so trustworthy only when nothing was merged or dropped.
            if len(value) == len(line_ids):
                return dict(zip(line_ids, value))
            if len(line_ids) == 1 and value:
                return {line_ids[0]: value[0]}
            return {}
        for item in value:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id", item.get("line_id", item.get("index")))
            text = item.get("text", item.get("translation"))
            try:
                line_id = int(str(raw_id).strip())
            except (TypeError, ValueError):
                continue
            if line_id in wanted and isinstance(text, str):
                results[line_id] = text
        return results

    raise AIResponseFormatError("Model dịch không trả về bản dịch theo id")


TRANSLATION_CONTEXT_BEFORE = 4
TRANSLATION_CONTEXT_AFTER = 2
# Enough for a cast list plus recurring terms; past that the prompt costs more
# than the consistency is worth.
GLOSSARY_LIMIT = 40
GLOSSARY_TERM_LIMIT = 60


def _line_payload(line: dict, *, with_translation: bool = False) -> dict:
    """One subtitle line as the model sees it, with empty fields left out."""

    payload = {"text": line.get("text", "")}
    speaker = line.get("speaker")
    if speaker is not None and str(speaker).strip() != "":
        payload["speaker"] = speaker
    if with_translation and str(line.get("translation", "")).strip():
        payload["translation"] = line["translation"]
    return payload


def _merge_glossary(
    glossary: dict[str, str],
    learned,
    pinned: frozenset[str] = frozenset(),
) -> None:
    """Fold this batch's terms into the running glossary, newest last.

    Kept small and in insertion order on purpose: the entries that survive are
    the ones the film keeps using, and the prompt stays a fixed size no matter
    how long the transcript is. Pinned terms are the style's own, so the model
    can neither redefine nor evict them.
    """

    if not isinstance(learned, dict):
        return
    for source, translation in learned.items():
        term, value = str(source).strip(), str(translation).strip()
        if not term or not value or term in pinned:
            continue
        if len(term) > GLOSSARY_TERM_LIMIT or len(value) > GLOSSARY_TERM_LIMIT:
            continue
        glossary.pop(term, None)
        glossary[term] = value
    for term in list(glossary):
        if len(glossary) <= GLOSSARY_LIMIT:
            break
        if term not in pinned:
            del glossary[term]


def _translate_lines_llm(
    lines: list[dict],
    target_language: str,
    *,
    context_before: list[dict],
    context_after: list[dict],
    glossary: dict[str, str],
    style_rules: tuple[str, ...] = (),
    model: str | None = None,
) -> tuple[dict[int, str], dict]:
    request: dict = {
        "target_language": target_language,
        "lines": {
            str(line_id): _line_payload(line)
            for line_id, line in enumerate(lines, start=1)
        },
    }
    if glossary:
        request["glossary"] = glossary
    if context_before:
        request["context_before"] = [
            _line_payload(line, with_translation=True) for line in context_before
        ]
    if context_after:
        request["context_after"] = [_line_payload(line) for line in context_after]

    prompt = (
        "Bạn là biên dịch viên phụ đề phim. Dịch các dòng trong `lines` sang "
        f"{target_language}.\n"
        "- Chỉ dịch `lines`. `context_before` (đã dịch), `context_after` và "
        "`glossary` chỉ để hiểu mạch truyện, không dịch và không trả về.\n"
        "- Lời thoại phải nối mạch với `context_before`: đại từ, xưng hô và cách "
        "gọi tên phải nhất quán với những gì đã dịch trước đó.\n"
        "- `speaker` là mã người nói; cùng mã là cùng một nhân vật, nên giữ "
        "nguyên giọng điệu và cách xưng hô của nhân vật đó xuyên suốt.\n"
        "- Mỗi khóa trong `lines` là một dòng phụ đề: dịch riêng từng khóa, "
        "không gộp, không tách, không bỏ khóa nào. Một câu bị cắt qua hai dòng "
        "thì dịch sao cho ghép lại vẫn thành câu.\n"
        "- Không thêm nhãn người nói, không chú thích, không giải thích.\n"
        "- Giữ nguyên tên riêng trừ khi `glossary` đã quy định cách gọi khác.\n"
        "- Các mục đã có sẵn trong `glossary` là bắt buộc: dịch đúng như vậy, "
        "không tự đổi sang cách gọi khác.\n"
        + "".join(f"- {rule}\n" for rule in style_rules)
        + "Trả về JSON: {\"translations\": {khóa: bản dịch}, \"glossary\": "
        "{nguyên bản: cách dịch chuẩn}}. Trong `glossary` chỉ thêm tên riêng, "
        "cách xưng hô giữa các nhân vật và thuật ngữ sẽ còn lặp lại về sau.\n\n"
        + json.dumps(request, ensure_ascii=False)
    )
    content = _llm_completion(
        [
            {
                "role": "system",
                "content": "Bạn chỉ xuất JSON hợp lệ, không dùng markdown fence.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        operation="dịch phụ đề",
        model=model,
    )
    value = _extract_json_value(content, "Model dịch")
    learned = value.get("glossary") if isinstance(value, dict) else None
    translations = _extract_translation_map(value, list(range(1, len(lines) + 1)))
    return translations, learned


def _missing_translation_ids(results: dict[int, str], line_ids: list[int]) -> list[int]:
    return [
        line_id for line_id in line_ids if not str(results.get(line_id, "")).strip()
    ]


def _translate_batch_llm(
    lines: list[dict],
    target_language: str,
    *,
    context_before: list[dict] = (),
    context_after: list[dict] = (),
    glossary: dict[str, str] | None = None,
    style: StyleBrief | None = None,
    model: str | None = None,
) -> list[str]:
    """One request per batch, then per-line repair for whatever came back missing.

    A batch that comes back one item short used to fail the entire translation.
    Repairing it costs a few single-line calls on a bad batch and nothing at all
    on a good one, which is the trade the old strict length check got wrong.

    The repair calls carry the same context as the batch: a line retranslated in
    isolation is exactly the disconnected line this all exists to avoid.
    """

    if not lines:
        return []
    line_ids = list(range(1, len(lines) + 1))
    terms = glossary if glossary is not None else {}
    brief = style or build_style_brief(STYLE_AUTO)

    def attempt(subset: list[int], before: list[dict], after: list[dict]) -> dict[int, str]:
        """Translate `subset` and return the results under the caller's ids."""

        try:
            partial, learned = _translate_lines_llm(
                [lines[line_id - 1] for line_id in subset],
                target_language,
                context_before=before,
                context_after=after,
                glossary=terms,
                style_rules=brief.rules,
                model=model,
            )
        except AIResponseFormatError as exc:
            logger.warning("translation batch returned an unusable shape: %s", exc)
            return {}
        _merge_glossary(terms, learned, brief.pinned())
        return {
            subset[index - 1]: text
            for index, text in partial.items()
            if 1 <= index <= len(subset)
        }

    batch_before, batch_after = list(context_before), list(context_after)
    results = attempt(line_ids, batch_before, batch_after)
    missing = _missing_translation_ids(results, line_ids)
    if missing and len(missing) == len(line_ids):
        # Nothing usable came back at all — retry the batch once before paying
        # for a request per line.
        results = attempt(line_ids, batch_before, batch_after)
        missing = _missing_translation_ids(results, line_ids)
    for line_id in missing:
        logger.warning("retranslating line %s of the batch on its own", line_id)
        # The lines around it inside this batch are the nearest context there is,
        # and by now most of them have been translated.
        local_before = batch_before + [
            {**lines[index - 1], "translation": results.get(index, "")}
            for index in line_ids[: line_id - 1]
        ]
        local_after = lines[line_id:] + batch_after
        results.update(
            attempt(
                [line_id],
                local_before[-TRANSLATION_CONTEXT_BEFORE:],
                local_after[:TRANSLATION_CONTEXT_AFTER],
            )
        )

    still_missing = _missing_translation_ids(results, line_ids)
    if still_missing:
        shown = ", ".join(str(line_id) for line_id in still_missing[:5])
        raise AIProviderError(
            f"Model không dịch được {len(still_missing)}/{len(line_ids)} dòng "
            f"trong lô (dòng {shown})"
        )
    return [results[line_id] for line_id in line_ids]


def _translate_batch(
    lines: list[dict],
    target_language: str,
    *,
    context_before: list[dict] = (),
    context_after: list[dict] = (),
    glossary: dict[str, str] | None = None,
    style: StyleBrief | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> list[str]:
    """Translate one batch of subtitle lines.

    Context, glossary and style are only meaningful to a chat model; the mock
    and the local sentence-pair pipeline see one line at a time either way.
    """

    texts = [str(line.get("text", "")) for line in lines]
    resolved_provider = resolve_translation_provider(provider or settings.translation_provider)
    if resolved_provider == TRANSLATION_MOCK:
        return [f"[{target_language}] {text}" for text in texts]
    if resolved_provider == TRANSLATION_TRANSFORMERS:
        return _translate_batch_transformers(texts, target_language, model_name=model)
    return _translate_batch_llm(
        lines,
        target_language,
        context_before=context_before,
        context_after=context_after,
        glossary=glossary,
        style=style,
        model=model,
    )


def _seed_translated_lines(
    cues: list[dict],
    lines: list[dict],
    results: list[str],
    resume_at: int,
) -> None:
    """Fill `results` with the translation the skipped cues already carry.

    Two reasons it cannot be skipped: the resumed batches quote these lines back
    to the model as context, and every cue is rebuilt from `results` at the end
    — an unseeded prefix would silently erase the work being resumed from.
    """

    owned: dict[int, list[int]] = {}
    for index, line in enumerate(lines):
        if line["cue"] < resume_at:
            owned.setdefault(line["cue"], []).append(index)

    for cue_index, indexes in owned.items():
        existing = [
            value
            for value in str(cues[cue_index].get("translation", "")).splitlines()
            if value.strip()
        ]
        if len(existing) != len(indexes):
            # Hand-edited into a different number of lines. Keeping it whole on
            # the first line loses the layout; dropping it would lose the work.
            existing = ["\n".join(existing)] + [""] * (len(indexes) - 1)
        for index, value in zip(indexes, existing):
            results[index] = value.strip()


def translate_cues(
    cues: list[dict],
    target_language: str,
    batch_size: int = 20,
    on_batch: Callable[[int, int, list[dict]], None] | None = None,
    *,
    source_language: str | None = None,
    style: str = STYLE_AUTO,
    style_notes: str = "",
    provider: str | None = None,
    model: str | None = None,
    from_cue: int = 0,
) -> list[dict]:
    """Translate line by line, keeping dialogue breaks intact.

    Each batch carries the lines around it and a glossary the model keeps
    extending, because a film is one conversation: translated twenty isolated
    lines at a time, pronouns lose their referent and characters change how they
    address each other from scene to scene.

    `style` (with `source_language` deciding what "auto" means) seeds that same
    glossary with terms the model is not allowed to redefine, which is how "đại
    ca" stays đại ca instead of becoming a correct, unwatchable "anh cả".

    `on_batch(done, total, cues_so_far)` fires after every batch. A long
    translation is dozens of provider calls and any one of them can fail, so the
    caller uses this both to report progress and to checkpoint what is already
    translated — a failure at batch 40 should not throw away batches 1-39.

    `from_cue` resumes: cues before that index keep the translation they already
    carry and are only re-read as context, so a run that was stopped (or a scene
    the user wants re-done) costs the provider calls it actually needs.
    """

    if not target_language.strip():
        raise AIProviderError("Cần chọn ngôn ngữ đích trước khi dịch")
    translated = [dict(cue) for cue in cues]
    lines: list[dict] = []
    for cue_index, cue in enumerate(translated):
        cue["text"] = _clean_dialogue_layout(str(cue.get("text", "")))
        for line in cue["text"].splitlines():
            if line.strip():
                lines.append(
                    {
                        "cue": cue_index,
                        "text": line.strip(),
                        "speaker": cue.get("speaker"),
                    }
                )
    results: list[str] = ["" for _line in lines]
    resume_at = max(0, int(from_cue))
    if resume_at:
        _seed_translated_lines(translated, lines, results, resume_at)

    def apply_translations() -> list[dict]:
        per_cue: list[list[str]] = [[] for _cue in translated]
        for line, value in zip(lines, results):
            if value:
                per_cue[line["cue"]].append(value)
        for cue_index, (cue, values) in enumerate(zip(translated, per_cue)):
            if not values and cue_index < resume_at:
                # Nothing of ours belongs to this cue (blank source text), and
                # it is behind the resume point — leave its translation alone.
                continue
            cue["translation"] = "\n".join(values)
        return translated

    total = len(lines)
    size = max(1, int(batch_size))
    start = next((index for index, line in enumerate(lines) if line["cue"] >= resume_at), total)
    brief = build_style_brief(style, source_language, style_notes)
    logger.info(
        "translating %s of %s lines as %s (%s pinned terms)",
        total - start, total, brief.label, len(brief.terms),
    )
    glossary: dict[str, str] = brief.glossary()
    for offset in range(start, total, size):
        end = min(offset + size, total)
        batch = lines[offset:end]
        translations = _translate_batch(
            batch,
            target_language.strip(),
            context_before=[
                {**lines[index], "translation": results[index]}
                for index in range(max(0, offset - TRANSLATION_CONTEXT_BEFORE), offset)
            ],
            context_after=lines[end : end + TRANSLATION_CONTEXT_AFTER],
            glossary=glossary,
            style=brief,
            provider=provider,
            model=model,
        )
        if len(translations) != len(batch):
            raise AIProviderError(
                f"Model trả về {len(translations)} bản dịch cho {len(batch)} dòng"
            )
        for index, value in enumerate(translations, start=offset):
            results[index] = strip_speaker_labels(value).strip()
        if on_batch is not None:
            on_batch(end, total, apply_translations())

    return apply_translations()
