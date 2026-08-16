"""Subtitle parsing and serialization for SRT and WebVTT."""

from __future__ import annotations

import re
from typing import Iterable


TIMESTAMP_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):"
    r"(?P<seconds>\d{2})(?:[\.,](?P<millis>\d{1,3}))?$"
)
SPEAKER_LABEL_RE = re.compile(
    r"^[ \t]*\[(?:s(?:peaker)?[ \t]*)?\d+\][ \t]*",
    re.IGNORECASE | re.MULTILINE,
)


class SubtitleParseError(ValueError):
    """Raised when a subtitle cannot be parsed."""


def strip_speaker_labels(text: str) -> str:
    """Remove legacy [S1]/[1] prefixes while preserving dialogue line breaks."""

    return SPEAKER_LABEL_RE.sub("", str(text))


def balance_lines(text: str, max_line_len: int = 42, max_lines: int = 2) -> str:
    """Balance text into at most `max_lines` (default 2 lines) with natural break points."""

    cleaned = strip_speaker_labels(str(text)).strip()
    if not cleaned:
        return ""

    # Normalize whitespace within each existing line
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""

    # If it's already 2 lines or fewer and all lines fit max_line_len, preserve it
    if len(lines) <= max_lines and all(len(line) <= max_line_len for line in lines):
        return "\n".join(lines)

    # Flatten text to rebalance
    flat_text = " ".join(lines)
    if len(flat_text) <= max_line_len:
        return flat_text

    # If max_lines is 1, return flat text
    if max_lines <= 1:
        return flat_text

    # Find the optimal split point for 2 lines near the middle
    midpoint = len(flat_text) // 2
    best_index = -1
    best_score = float("inf")

    # Look for candidate split spaces
    spaces = [match.start() for match in re.finditer(r"\s+", flat_text)]
    if not spaces:
        return flat_text

    for space_idx in spaces:
        left = flat_text[:space_idx].strip()
        right = flat_text[space_idx:].strip()
        if not left or not right:
            continue

        # Prefer breaking after punctuation (,, ;, :, ., ?, !)
        has_punct = left[-1] in {",", ";", ":", ".", "?", "!", "—", "–", '"', "'", "”"}
        # Distance from middle
        distance = abs(space_idx - midpoint)
        # Penalize if either line exceeds max_line_len
        overflow_penalty = max(0, len(left) - max_line_len) * 5 + max(0, len(right) - max_line_len) * 5
        punct_bonus = -15 if has_punct else 0
        score = distance + overflow_penalty + punct_bonus

        if score < best_score:
            best_score = score
            best_index = space_idx

    if best_index > 0:
        line1 = flat_text[:best_index].strip()
        line2 = flat_text[best_index:].strip()
        return f"{line1}\n{line2}"

    return flat_text


def _split_into_sentences(text: str) -> list[str]:
    """Split text into sentence-like segments, respecting abbreviations and numbers."""
    cleaned = strip_speaker_labels(str(text)).strip()
    if not cleaned:
        return []

    # Preserve explicit line breaks as sentence boundaries if present
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    raw_sentences = []
    for line in lines:
        # Match sentence-ending punctuation followed by space or end of string
        # Avoid splitting on decimals (e.g. 5.50) or common abbreviations
        splits = re.split(r"(?<=[.?!…])\s+(?=[A-ZÀ-Ỹ0-9\"'“‘])|(?<=[。？！])", line)
        for segment in splits:
            seg = segment.strip()
            if seg:
                raw_sentences.append(seg)

    return raw_sentences or [cleaned]


def split_text_into_chunks(
    text: str,
    max_chars: int = 75,
    total_duration: float | None = None,
    max_duration: float = 6.0,
) -> list[str]:
    """Split long text into readable chunks (max chars and max duration per chunk)."""
    sentences = _split_into_sentences(text)
    if not sentences:
        return []

    # Calculate approx seconds per char if duration is provided
    total_chars = max(1, sum(len(s) for s in sentences))
    sec_per_char = (total_duration / total_chars) if (total_duration and total_duration > 0) else None

    # Estimate max chars for this text so that duration <= max_duration
    effective_max_chars = max_chars
    if sec_per_char and sec_per_char > 0:
        duration_char_limit = int(max_duration / sec_per_char)
        if duration_char_limit >= 20:
            effective_max_chars = min(max_chars, duration_char_limit)

    units: list[str] = []
    for sentence in sentences:
        if len(sentence) <= effective_max_chars:
            units.append(sentence)
            continue

        # Split long sentence by clauses (comma, semicolon, colon, dash)
        clauses = re.split(r"(?<=[,;:—–])\s+", sentence)
        for clause in clauses:
            cl = clause.strip()
            if not cl:
                continue
            if len(cl) <= effective_max_chars:
                units.append(cl)
                continue

            # Split remaining long clause by word boundaries
            words = cl.split()
            current_words: list[str] = []
            current_len = 0
            for word in words:
                word_len = len(word) + (1 if current_words else 0)
                if current_words and current_len + word_len > effective_max_chars:
                    units.append(" ".join(current_words))
                    current_words = [word]
                    current_len = len(word)
                else:
                    current_words.append(word)
                    current_len += word_len
            if current_words:
                units.append(" ".join(current_words))

    # Pack smaller consecutive units together up to effective_max_chars
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for unit in units:
        unit_len = len(unit) + (1 if current_chunk else 0)
        # If adding unit exceeds effective_max_chars, flush current_chunk
        if current_chunk and current_len + unit_len > effective_max_chars:
            chunks.append(" ".join(current_chunk))
            current_chunk = [unit]
            current_len = len(unit)
        else:
            current_chunk.append(unit)
            current_len += unit_len

    if current_chunk:
        chunks.append(" ".join(current_chunk))

    return chunks


def split_long_cue(
    cue: dict,
    max_chars: int = 75,
    max_duration: float = 6.0,
    max_lines: int = 2,
) -> list[dict]:
    """Split a single long cue into multiple smaller, comfortable subtitle cues."""
    start = max(0.0, float(cue.get("start", 0.0)))
    end = max(start, float(cue.get("end", start)))
    duration = end - start
    raw_text = strip_speaker_labels(str(cue.get("text", ""))).strip()
    raw_translation = strip_speaker_labels(str(cue.get("translation", ""))).strip()

    if not raw_text:
        return [dict(cue)]

    flat_text = re.sub(r"\s+", " ", raw_text).strip()
    # If already short enough and line count is <= max_lines
    if duration <= max_duration and len(flat_text) <= max_chars and raw_text.count("\n") < max_lines:
        balanced_cue = dict(cue)
        balanced_cue["text"] = balance_lines(raw_text, max_line_len=40, max_lines=max_lines)
        if raw_translation:
            balanced_cue["translation"] = balance_lines(raw_translation, max_line_len=40, max_lines=max_lines)
        return [balanced_cue]

    text_chunks = split_text_into_chunks(
        raw_text,
        max_chars=max_chars,
        total_duration=duration,
        max_duration=max_duration,
    )
    if len(text_chunks) <= 1:
        balanced_cue = dict(cue)
        balanced_cue["text"] = balance_lines(raw_text, max_line_len=40, max_lines=max_lines)
        if raw_translation:
            balanced_cue["translation"] = balance_lines(raw_translation, max_line_len=40, max_lines=max_lines)
        return [balanced_cue]

    # Handle translation chunks if present
    trans_chunks = (
        split_text_into_chunks(
            raw_translation,
            max_chars=max_chars,
            total_duration=duration,
            max_duration=max_duration,
        )
        if raw_translation
        else []
    )

    total_weight = sum(max(1, len(re.sub(r"\s+", " ", chunk))) for chunk in text_chunks)
    result_cues: list[dict] = []
    elapsed_weight = 0

    for index, chunk in enumerate(text_chunks):
        chunk_weight = max(1, len(re.sub(r"\s+", " ", chunk)))
        sub_start = start + (elapsed_weight / total_weight) * duration
        sub_end = start + ((elapsed_weight + chunk_weight) / total_weight) * duration
        elapsed_weight += chunk_weight

        # Pick matching translation chunk or map proportionally
        sub_translation = ""
        if len(trans_chunks) == len(text_chunks):
            sub_translation = trans_chunks[index]
        elif trans_chunks and index < len(trans_chunks):
            sub_translation = trans_chunks[index]

        result_cues.append(
            {
                "id": 0,
                "start": round(sub_start, 3),
                "end": round(max(sub_start + 0.12, sub_end), 3),
                "text": balance_lines(chunk, max_line_len=40, max_lines=max_lines),
                "translation": balance_lines(sub_translation, max_line_len=40, max_lines=max_lines),
                "speaker": cue.get("speaker"),
            }
        )

    return result_cues



def split_long_cues(
    cues: Iterable[dict],
    max_chars: int = 80,
    max_duration: float = 6.5,
    max_lines: int = 2,
) -> list[dict]:
    """Process a list of cues, splitting any long dialogue cues and renumbering IDs."""
    new_cues: list[dict] = []
    for cue in cues:
        split_items = split_long_cue(
            cue,
            max_chars=max_chars,
            max_duration=max_duration,
            max_lines=max_lines,
        )
        new_cues.extend(split_items)

    for index, cue in enumerate(new_cues, start=1):
        cue["id"] = index

    return new_cues




def parse_timestamp(value: str) -> float:
    value = value.strip()
    match = TIMESTAMP_RE.match(value)
    if not match:
        raise SubtitleParseError(f"Invalid timestamp: {value}")

    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    millis_text = (match.group("millis") or "").ljust(3, "0")
    millis = int(millis_text or 0)
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def format_timestamp(seconds: float, separator: str = ",") -> str:
    total_millis = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(total_millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}{separator}{millis:03d}"


def _parse_timing_line(line: str) -> tuple[float, float]:
    if "-->" not in line:
        raise SubtitleParseError("Missing subtitle timing separator")
    start_text, end_text = line.split("-->", 1)
    # VTT allows settings after the end timestamp, for example align:start.
    end_text = end_text.strip().split(maxsplit=1)[0]
    start = parse_timestamp(start_text)
    end = parse_timestamp(end_text)
    if end <= start:
        raise SubtitleParseError("Subtitle end must be after start")
    return start, end


def parse_subtitle(content: str, format_hint: str | None = None) -> list[dict]:
    """Parse SRT/VTT text into stable cue dictionaries.

    The parser intentionally keeps cue text as-is, including line breaks and
    inline markup. Timing is normalized to seconds so the editor can use the
    same model for SRT and VTT.
    """

    normalized = content.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    hint = (format_hint or "").lower().lstrip(".")
    is_vtt = hint == "vtt" or (lines and lines[0].strip().upper().startswith("WEBVTT"))

    cues: list[dict] = []
    blocks = re.split(r"\n\s*\n", normalized.strip()) if normalized.strip() else []
    for block in blocks:
        block_lines = [line.rstrip() for line in block.split("\n")]
        if not block_lines:
            continue
        first = block_lines[0].strip().upper()
        if is_vtt and (first.startswith("WEBVTT") or first in {"NOTE", "STYLE", "REGION"}):
            continue

        timing_index = next(
            (index for index, line in enumerate(block_lines) if "-->" in line),
            None,
        )
        if timing_index is None:
            continue
        try:
            start, end = _parse_timing_line(block_lines[timing_index])
        except SubtitleParseError:
            continue
        text_lines = block_lines[timing_index + 1 :]
        while text_lines and not text_lines[0].strip():
            text_lines.pop(0)
        while text_lines and not text_lines[-1].strip():
            text_lines.pop()
        text = "\n".join(text_lines)
        if not text.strip():
            continue
        cues.append(
            {
                "id": len(cues) + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "translation": "",
            }
        )
    return cues


def _cue_text(cue: dict, track: str) -> str:
    if track == "translated" and str(cue.get("translation", "")).strip():
        return strip_speaker_labels(str(cue["translation"]))
    return strip_speaker_labels(str(cue.get("text", "")))


def format_subtitle(
    cues: Iterable[dict], format_name: str = "srt", track: str = "source"
) -> str:
    format_name = format_name.lower().lstrip(".")
    if format_name not in {"srt", "vtt"}:
        raise SubtitleParseError(f"Unsupported subtitle format: {format_name}")

    cue_list = list(cues)
    chunks: list[str] = ["WEBVTT", ""] if format_name == "vtt" else []
    for index, cue in enumerate(cue_list, start=1):
        start_separator = "." if format_name == "vtt" else ","
        start = format_timestamp(cue.get("start", 0), start_separator)
        end = format_timestamp(cue.get("end", 0), start_separator)
        text = _cue_text(cue, track).strip()
        if format_name == "srt":
            chunks.extend([str(index), f"{start} --> {end}", text, ""])
        else:
            chunks.extend([f"{start} --> {end}", text, ""])
    return "\n".join(chunks).rstrip() + "\n"
