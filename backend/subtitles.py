"""Subtitle parsing and serialization for SRT and WebVTT."""

from __future__ import annotations

import re
from dataclasses import dataclass
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


# ── Script awareness ─────────────────────────────────────────────
#
# Every length budget below used to be a plain character count, which silently
# assumes a Latin script: 40 characters is a comfortable subtitle line in
# English and roughly two and a half lines in Chinese. Worse, all the splitting
# was anchored on whitespace, and Chinese has none — so a Chinese cue could
# neither be wrapped nor split, and the only breaks left were the blunt ones.

# Han, Hiragana, Katakana, Hangul, plus the fullwidth/CJK punctuation blocks.
CJK_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]"
)
# Punctuation that closes a sentence in CJK typesetting.
CJK_SENTENCE_ENDERS = "。？！…‥"
# Punctuation that closes a clause — a good, cheap break point.
CJK_CLAUSE_ENDERS = "，、；：·"
# Must never start a line: closing brackets and trailing marks.
CJK_NO_LINE_START = "。？！…‥，、；：·》）］｝」』〉”’%）"
# Must never end a line: opening brackets.
CJK_NO_LINE_END = "《（［｛「『〈“‘（"


def is_cjk_char(char: str) -> bool:
    return bool(CJK_RE.match(char))


def cjk_ratio(text: str) -> float:
    """Share of the letters in `text` that are CJK ideographs or kana."""

    letters = [char for char in str(text) if not char.isspace()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if is_cjk_char(char)) / len(letters)


def is_cjk_text(text: str) -> bool:
    """True when the text should be measured and broken as CJK.

    The threshold is low on purpose: a Chinese line carrying a Latin name or a
    number is still a Chinese line, and mis-measuring it as Latin is what makes
    cues run two and a half lines long.
    """

    return cjk_ratio(text) >= 0.2


def display_width(text: str) -> int:
    """Length in Latin-equivalent columns, counting CJK glyphs as two."""

    return sum(2 if is_cjk_char(char) else 1 for char in str(text))


@dataclass(frozen=True)
class CueStyle:
    """The readability budget one script imposes on a cue.

    Values follow the usual broadcast guidance: about 42 characters per line
    and 17-21 characters per second for Latin, roughly 16 glyphs per line and
    9 glyphs per second for CJK, where each glyph carries far more meaning.
    """

    max_line_chars: int
    max_lines: int
    max_chars: int
    max_cps: float
    min_chars: int
    min_duration: float
    max_duration: float
    min_gap: float
    merge_gap: float
    split_gap: float

    def fits(self, text: str, duration: float) -> bool:
        """True when `text` can be read comfortably in `duration` seconds."""

        length = len(text.replace("\n", ""))
        return (
            length <= self.max_chars
            and duration <= self.max_duration
            and (duration <= 0 or length / duration <= self.max_cps)
        )

    def too_short(self, text: str, duration: float) -> bool:
        """True when the cue is a fragment: too little text, or gone too fast."""

        length = len(text.replace("\n", ""))
        return length < self.min_chars or duration < self.min_duration


LATIN_STYLE = CueStyle(
    max_line_chars=42,
    max_lines=2,
    max_chars=84,
    max_cps=21.0,
    min_chars=12,
    min_duration=1.0,
    max_duration=7.0,
    min_gap=0.08,
    merge_gap=0.35,
    split_gap=0.7,
)

CJK_STYLE = CueStyle(
    max_line_chars=16,
    max_lines=2,
    max_chars=32,
    max_cps=9.0,
    min_chars=5,
    min_duration=1.0,
    max_duration=7.0,
    min_gap=0.08,
    merge_gap=0.45,
    split_gap=0.7,
)


def style_for_text(text: str) -> CueStyle:
    return CJK_STYLE if is_cjk_text(text) else LATIN_STYLE


def join_tokens(tokens: Iterable[str]) -> str:
    """Join recognised words, omitting the space CJK never writes."""

    joined = ""
    for token in tokens:
        token = str(token).strip()
        if not token:
            continue
        if joined and not (
            is_cjk_char(joined[-1])
            or is_cjk_char(token[0])
            or token[0] in CJK_NO_LINE_START
        ):
            joined += " "
        joined += token
    return joined


def strip_speaker_labels(text: str) -> str:
    """Remove legacy [S1]/[1] prefixes while preserving dialogue line breaks."""

    return SPEAKER_LABEL_RE.sub("", str(text))


def _break_candidates(text: str) -> list[int]:
    """Indices where a line break is typographically allowed.

    Latin breaks at spaces. CJK has none, so every character boundary is a legal
    break except the ones that would strand a closing mark at the start of a
    line or an opening bracket at the end of one.
    """

    spaces = [match.start() for match in re.finditer(r"\s+", text)]
    if spaces or not is_cjk_text(text):
        return spaces
    return [
        index
        for index in range(1, len(text))
        if text[index] not in CJK_NO_LINE_START
        and text[index - 1] not in CJK_NO_LINE_END
    ]


def balance_lines(text: str, max_line_len: int | None = None, max_lines: int = 2) -> str:
    """Balance text into at most `max_lines` (default 2 lines) with natural break points.

    `max_line_len` defaults to whatever the script of the text can carry, so a
    caller that does not care about the distinction gets a readable Chinese line
    instead of a Latin-length one.
    """

    cleaned = strip_speaker_labels(str(text)).strip()
    if not cleaned:
        return ""

    if max_line_len is None:
        max_line_len = style_for_text(cleaned).max_line_chars

    # Normalize whitespace within each existing line
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""

    # If it's already 2 lines or fewer and all lines fit max_line_len, preserve it
    if len(lines) <= max_lines and all(len(line) <= max_line_len for line in lines):
        return "\n".join(lines)

    # Flatten text to rebalance
    flat_text = join_tokens(lines) if is_cjk_text(cleaned) else " ".join(lines)
    if len(flat_text) <= max_line_len:
        return flat_text

    # If max_lines is 1, return flat text
    if max_lines <= 1:
        return flat_text

    # Find the optimal split point for 2 lines near the middle
    midpoint = len(flat_text) // 2
    best_index = -1
    best_score = float("inf")

    candidates = _break_candidates(flat_text)
    if not candidates:
        return flat_text

    sentence_enders = {".", "?", "!", "…"} | set(CJK_SENTENCE_ENDERS)
    clause_enders = {",", ";", ":", "—", "–", '"', "'", "”"} | set(CJK_CLAUSE_ENDERS)

    for index in candidates:
        left = flat_text[:index].strip()
        right = flat_text[index:].strip()
        if not left or not right:
            continue

        # Prefer breaking after punctuation, sentence ends most of all.
        if left[-1] in sentence_enders:
            punct_bonus = -25
        elif left[-1] in clause_enders:
            punct_bonus = -15
        else:
            punct_bonus = 0
        # Distance from middle
        distance = abs(index - midpoint)
        # Penalize if either line exceeds max_line_len
        overflow_penalty = max(0, len(left) - max_line_len) * 5 + max(0, len(right) - max_line_len) * 5
        score = distance + overflow_penalty + punct_bonus

        if score < best_score:
            best_score = score
            best_index = index

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
        # Avoid splitting on decimals (e.g. 5.50) or common abbreviations.
        # CJK writes no space after a full stop, so its enders split on sight.
        splits = re.split(
            rf"(?<=[.?!…])\s+(?=[A-ZÀ-Ỹ0-9\"'“‘])|(?<=[{CJK_SENTENCE_ENDERS}])",
            line,
        )
        for segment in splits:
            seg = segment.strip()
            if seg:
                raw_sentences.append(seg)

    return raw_sentences or [cleaned]


def _split_into_clauses(sentence: str) -> list[str]:
    """Break a sentence at clause punctuation, with or without a following space."""

    parts = re.split(
        rf"(?<=[,;:—–])\s+|(?<=[{CJK_CLAUSE_ENDERS}])",
        sentence,
    )
    return [part.strip() for part in parts if part.strip()]


def _split_run(run: str, limit: int) -> list[str]:
    """Chop an unbreakable run down to `limit`, by words or by characters."""

    words = run.split()
    if len(words) > 1:
        pieces: list[str] = []
        current: list[str] = []
        current_len = 0
        for word in words:
            word_len = len(word) + (1 if current else 0)
            if current and current_len + word_len > limit:
                pieces.append(" ".join(current))
                current = [word]
                current_len = len(word)
            else:
                current.append(word)
                current_len += word_len
        if current:
            pieces.append(" ".join(current))
        return pieces

    # A single "word" longer than the limit is either a CJK run or a URL; both
    # have to be cut on a character boundary or they never get split at all.
    return [run[index : index + limit] for index in range(0, len(run), limit)] or [run]


def split_text_into_chunks(
    text: str,
    max_chars: int | None = None,
    total_duration: float | None = None,
    max_duration: float = 6.0,
) -> list[str]:
    """Split long text into readable chunks (max chars and max duration per chunk)."""
    sentences = _split_into_sentences(text)
    if not sentences:
        return []

    if max_chars is None:
        max_chars = style_for_text(text).max_chars

    # Calculate approx seconds per char if duration is provided
    total_chars = max(1, sum(len(s) for s in sentences))
    sec_per_char = (total_duration / total_chars) if (total_duration and total_duration > 0) else None

    # Estimate max chars for this text so that duration <= max_duration
    effective_max_chars = max_chars
    if sec_per_char and sec_per_char > 0:
        duration_char_limit = int(max_duration / sec_per_char)
        # A CJK glyph is worth several Latin characters, so the floor that keeps
        # a Latin chunk readable would swallow a whole Chinese cue.
        floor = 8 if is_cjk_text(text) else 20
        if duration_char_limit >= floor:
            effective_max_chars = min(max_chars, duration_char_limit)

    joiner = join_tokens if is_cjk_text(text) else " ".join

    units: list[str] = []
    for sentence in sentences:
        if len(sentence) <= effective_max_chars:
            units.append(sentence)
            continue

        for clause in _split_into_clauses(sentence):
            if len(clause) <= effective_max_chars:
                units.append(clause)
                continue
            units.extend(_split_run(clause, effective_max_chars))

    # Pack smaller consecutive units together up to effective_max_chars
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for unit in units:
        unit_len = len(unit) + (1 if current_chunk else 0)
        # If adding unit exceeds effective_max_chars, flush current_chunk
        if current_chunk and current_len + unit_len > effective_max_chars:
            chunks.append(joiner(current_chunk))
            current_chunk = [unit]
            current_len = len(unit)
        else:
            current_chunk.append(unit)
            current_len += unit_len

    if current_chunk:
        chunks.append(joiner(current_chunk))

    return chunks


def split_long_cue(
    cue: dict,
    max_chars: int | None = None,
    max_duration: float | None = None,
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

    style = style_for_text(raw_text)
    if max_chars is None:
        max_chars = style.max_chars
    if max_duration is None:
        max_duration = style.max_duration
    line_len = min(style.max_line_chars, max_chars)

    flat_text = re.sub(r"\s+", " ", raw_text).strip()
    # If already short enough and line count is <= max_lines
    if duration <= max_duration and len(flat_text) <= max_chars and raw_text.count("\n") < max_lines:
        balanced_cue = dict(cue)
        balanced_cue["text"] = balance_lines(raw_text, max_line_len=line_len, max_lines=max_lines)
        if raw_translation:
            balanced_cue["translation"] = balance_lines(raw_translation, max_lines=max_lines)
        return [balanced_cue]

    text_chunks = split_text_into_chunks(
        raw_text,
        max_chars=max_chars,
        total_duration=duration,
        max_duration=max_duration,
    )
    if len(text_chunks) <= 1:
        balanced_cue = dict(cue)
        balanced_cue["text"] = balance_lines(raw_text, max_line_len=line_len, max_lines=max_lines)
        if raw_translation:
            balanced_cue["translation"] = balance_lines(raw_translation, max_lines=max_lines)
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
                "text": balance_lines(chunk, max_line_len=line_len, max_lines=max_lines),
                "translation": balance_lines(sub_translation, max_lines=max_lines),
                "speaker": cue.get("speaker"),
            }
        )

    return result_cues



def split_long_cues(
    cues: Iterable[dict],
    max_chars: int | None = None,
    max_duration: float | None = None,
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


def _ends_sentence(text: str) -> bool:
    stripped = str(text).strip()
    if not stripped:
        return False
    return stripped[-1] in (set(".?!…") | set(CJK_SENTENCE_ENDERS))


def merge_short_cues(cues: Iterable[dict], style: CueStyle | None = None) -> list[dict]:
    """Join fragment cues back onto the neighbour they were cut from.

    Splitting alone produces the failure this fixes: a recogniser that hands
    back "看清楚" and "了吧" as two utterances leaves two cues nobody can read,
    because each is gone in under a second. A merge is allowed only when the
    result still fits the reading budget, so nothing here can create the
    opposite problem of an over-long cue.
    """

    merged: list[dict] = []
    for cue in cues:
        candidate = dict(cue)
        if not merged:
            merged.append(candidate)
            continue

        previous = merged[-1]
        prev_text = str(previous.get("text", "")).strip()
        next_text = str(candidate.get("text", "")).strip()
        if not prev_text or not next_text:
            merged.append(candidate)
            continue

        active = style or style_for_text(f"{prev_text}{next_text}")
        prev_start = float(previous.get("start", 0.0))
        prev_end = float(previous.get("end", prev_start))
        next_start = float(candidate.get("start", prev_end))
        next_end = float(candidate.get("end", next_start))

        gap = next_start - prev_end
        same_speaker = previous.get("speaker") == candidate.get("speaker")
        prev_short = active.too_short(prev_text, prev_end - prev_start)
        next_short = active.too_short(next_text, next_end - next_start)

        combined_text = (
            join_tokens([prev_text, next_text])
            if is_cjk_text(prev_text) and is_cjk_text(next_text)
            else f"{prev_text} {next_text}"
        )
        combined_text = re.sub(r"\s+", " ", combined_text.replace("\n", " ")).strip()

        should_merge = (
            same_speaker
            and gap <= active.merge_gap
            and (prev_short or next_short)
            # A finished sentence that already reads comfortably stays its own
            # cue; only a fragment gets pulled across the full stop.
            and not (_ends_sentence(prev_text) and not prev_short)
            and active.fits(combined_text, next_end - prev_start)
        )
        if not should_merge:
            merged.append(candidate)
            continue

        previous["text"] = balance_lines(combined_text, max_lines=active.max_lines)
        previous["end"] = round(next_end, 3)
        prev_translation = str(previous.get("translation", "")).strip()
        next_translation = str(candidate.get("translation", "")).strip()
        if prev_translation or next_translation:
            previous["translation"] = balance_lines(
                " ".join(part for part in (prev_translation, next_translation) if part),
                max_lines=active.max_lines,
            )

    for index, cue in enumerate(merged, start=1):
        cue["id"] = index
    return merged


def enforce_cue_timing(
    cues: Iterable[dict],
    style: CueStyle | None = None,
    media_duration: float | None = None,
) -> list[dict]:
    """Give every cue a floor duration, borrowing only from real silence.

    A cue that leaves the screen in 0.6s cannot be read no matter how short the
    line is. The time is taken from the gap after it (or before it) and never
    from a neighbour, so timings stay honest to the audio.
    """

    result = [dict(cue) for cue in cues]
    for index, cue in enumerate(result):
        active = style or style_for_text(str(cue.get("text", "")))
        start = float(cue.get("start", 0.0))
        end = max(float(cue.get("end", start)), start)
        if end - start >= active.min_duration:
            continue

        ceiling = (
            float(result[index + 1]["start"]) - active.min_gap
            if index + 1 < len(result)
            else (media_duration if media_duration is not None else start + active.min_duration)
        )
        end = max(end, min(start + active.min_duration, ceiling))

        if end - start < active.min_duration:
            floor = (
                float(result[index - 1]["end"]) + active.min_gap
                if index > 0
                else 0.0
            )
            start = min(start, max(floor, end - active.min_duration))

        cue["start"] = round(max(start, 0.0), 3)
        cue["end"] = round(max(end, cue["start"] + 0.12), 3)

    return result




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
