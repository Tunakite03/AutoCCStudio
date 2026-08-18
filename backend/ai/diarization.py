"""Speaker-turn analysis: split a cue's transcript at inferred speaker changes."""

from __future__ import annotations

import json

from ..core.config import settings
from ..core.messages import Message
from ..domain.subtitles.layout import (
    clean_dialogue_layout,
    dialogue_break_positions,
    same_dialogue_content,
)
from ..domain.subtitles.parser import strip_speaker_labels
from .llm import _extract_json_value, _llm_completion
from .shared import OP_SPEAKER_ANALYSIS, AIResponseFormatError, ProgressCallback, _report, logger

# Compatibility aliases for tests and old imports while the shared module is
# adopted incrementally by the rest of the backend.
_clean_dialogue_layout = clean_dialogue_layout
_same_dialogue_content = same_dialogue_content
_dialogue_break_positions = dialogue_break_positions


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
    value = _extract_json_value(content, "err.ai.dialogueNotJson")
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
            return dict(zip(target_ids, value, strict=False))
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

    raise AIResponseFormatError("err.ai.dialogueBadShape")


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
        "Mark the speaker turns inside the `targets` field of the JSON below.\n"
        "- Work out where a different person starts speaking from the "
        "question-and-answer flow, the pronouns, the forms of address and the "
        "surrounding context.\n"
        "- `acoustic_speaker_turns` is per-word evidence from the audio: every "
        "line break that already comes from it must survive unchanged.\n"
        "- `speaker_hint` is only a hint about the dominant voice, or about who "
        "speaks at the start of the cue.\n"
        "- Return each target verbatim, inserting nothing except a \\n newline "
        "exactly where the speaker changes.\n"
        "- Never add labels such as [1], [2] or [S1], leading dashes, or "
        "character names.\n"
        "- Never add, delete, reorder or alter any word, punctuation mark or "
        "character.\n"
        "- If you are not sure a line has two speakers, return it untouched.\n"
        "- `context_before` and `context_after` are for reference only.\n"
        "Reply with one JSON object keyed by cue_id as a string, whose values "
        'are the dialogue, e.g. {"5":"First line.\\nSecond line."}. Return every '
        "cue_id you were given and no others.\n\n"
        + json.dumps(request_data, ensure_ascii=False)
    )
    content = _llm_completion(
        [
            {
                "role": "system",
                "content": (
                    "You are a dialogue-turn segmenter. You emit valid JSON only, "
                    "and you never change the dialogue itself."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        operation=OP_SPEAKER_ANALYSIS,
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
    _report(on_progress, 0, len(analyzed), Message("progress.analyzingTurns"))
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

        _report(on_progress, end, len(analyzed), Message("progress.analyzingTurns"))

    report["failed_cues"] = len(report["failed_cue_ids"])
    return (analyzed, report) if return_report else analyzed
