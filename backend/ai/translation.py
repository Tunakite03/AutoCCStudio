"""Subtitle translation: batching, glossary continuity, and dub-line shortening."""

from __future__ import annotations

import json
from typing import Callable

from ..config import settings
from ..subtitles import strip_speaker_labels
from ..translation_style import STYLE_AUTO, StyleBrief, build_style_brief
from .diarization import _clean_dialogue_layout
from .llm import _extract_json_value, _llm_completion, _translate_batch_transformers
from .shared import (
    OP_DUB_SHORTEN,
    OP_TRANSLATE,
    AIProviderError,
    AIResponseFormatError,
    logger,
)

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
        _extract_json_value(content, "err.ai.translationNotJson")
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

    raise AIResponseFormatError("err.ai.translationBadShape")


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
        "You are a film subtitle translator. Translate the lines in `lines` into "
        f"{target_language}.\n"
        "- Translate `lines` and nothing else. `context_before` (already "
        "translated), `context_after` and `glossary` are there so you can follow "
        "the story; never translate them and never return them.\n"
        "- The dialogue has to run on from `context_before`: pronouns, forms of "
        "address and the wording of names stay consistent with what was already "
        "translated.\n"
        "- `speaker` is a speaker id; the same id is the same character, so keep "
        "that character's register and the way they address others stable "
        "throughout.\n"
        "- Every key in `lines` is one subtitle line: translate each key on its "
        "own, merging none, splitting none, dropping none. Where one sentence is "
        "cut across two lines, translate so that the two still read as one "
        "sentence when joined.\n"
        "- No speaker labels, no annotations, no explanations.\n"
        "- Keep proper names as they are unless `glossary` already fixes another "
        "form for them.\n"
        "- Entries already present in `glossary` are binding: reuse them exactly "
        "and never substitute wording of your own.\n"
        + "".join(f"- {rule}\n" for rule in style_rules)
        + 'Reply with JSON: {"translations": {key: translation}, "glossary": '
        "{source term: agreed translation}}. Add to `glossary` only proper names, "
        "the forms of address used between characters, and terminology that will "
        "come up again later.\n\n"
        + json.dumps(request, ensure_ascii=False)
    )
    content = _llm_completion(
        [
            {
                "role": "system",
                "content": "You output valid JSON only, with no markdown fence.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        operation=OP_TRANSLATE,
        model=model,
    )
    value = _extract_json_value(content, "err.ai.translationNotJson")
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
        raise AIProviderError(
            "err.ai.translationIncomplete",
            missing=len(still_missing),
            total=len(line_ids),
            lines=", ".join(str(line_id) for line_id in still_missing[:5]),
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
        raise AIProviderError("err.ai.targetLanguageMissing")
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
        total - start, total, brief.key, len(brief.terms),
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
                "err.ai.translationCountMismatch",
                returned=len(translations),
                expected=len(batch),
            )
        for index, value in enumerate(translations, start=offset):
            results[index] = strip_speaker_labels(value).strip()
        if on_batch is not None:
            on_batch(end, total, apply_translations())

    return apply_translations()


# Small batches on purpose: every line here carries its own character budget,
# and a model given thirty of them starts applying one budget to all of them.
DUB_SHORTEN_BATCH = 12


def _shortened_line(original: str, candidate, limit: int) -> str | None:
    """Accept a rewrite only if it is genuinely shorter and still a line.

    A model that answers with the original text, an apology, or something longer
    has not helped — the caller is better off speeding the original up than
    swapping in a rewrite that costs meaning and buys no time.
    """

    if not isinstance(candidate, str):
        return None
    cleaned = " ".join(strip_speaker_labels(candidate).split())
    if not cleaned or cleaned == original:
        return None
    if len(cleaned) >= len(original):
        return None
    # Below a third of the original is not a shortening, it is a summary that
    # dropped a clause. Rejecting it keeps the dub honest at the cost of a
    # rushed line.
    if len(cleaned) * 3 < len(original) and len(cleaned) < limit // 2:
        return None
    return cleaned


def shorten_for_dubbing(
    items: list[dict],
    target_language: str | None = None,
    *,
    model: str | None = None,
) -> dict[int, str]:
    """Say the same thing in fewer characters, for lines that will not fit.

    `items` is `[{"id": cue index, "text": line, "max_chars": budget}]`. The
    reply maps only the lines that came back genuinely shorter — anything else
    is dropped, so the caller can treat a missing id as "this one stays as it is".

    This is the last of the three fitting strategies and the only one that costs
    provider calls, which is why `dubbing` reaches it with the handful of lines
    that a speed-up and the following silence could not absorb.
    """

    requests = [
        {
            "id": int(item["id"]),
            "text": " ".join(str(item.get("text", "")).split()),
            "max_chars": max(1, int(item.get("max_chars") or 0)),
        }
        for item in items
        if str(item.get("text", "")).strip()
    ]
    results: dict[int, str] = {}
    if not requests:
        return results

    originals = {item["id"]: item["text"] for item in requests}
    limits = {item["id"]: item["max_chars"] for item in requests}
    language = (target_language or "").strip()

    for offset in range(0, len(requests), DUB_SHORTEN_BATCH):
        batch = requests[offset : offset + DUB_SHORTEN_BATCH]
        prompt = (
            "Each line below takes too long to say out loud for the subtitle it "
            "belongs to. Rewrite every one of them so it can be spoken faster.\n"
            "- `max_chars` is that line's budget: get at or under it.\n"
            "- Keep the meaning, the tone and the form of address. Dropping a "
            "clause is better than changing who says what to whom.\n"
            "- Stay in the same language as the line you are given"
            + (f" ({language})" if language else "")
            + ".\n"
            "- Reply with the rewritten line only: no quotes, no notes, no "
            "explanation of what you cut.\n"
            "- Never add speaker labels, dashes or line breaks.\n"
            'Reply with one JSON object keyed by id as a string, e.g. {"7":"Ngắn hơn."}. '
            "Return every id you were given and no others.\n\n"
            + json.dumps({"lines": batch}, ensure_ascii=False)
        )
        content = _llm_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You compress dialogue for dubbing. You emit valid JSON "
                        "only, and you never change what a line means."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            operation=OP_DUB_SHORTEN,
            model=model,
        )
        payload = _extract_json_value(content, "err.ai.dubShortenNotJson")
        if not isinstance(payload, dict):
            raise AIResponseFormatError("err.ai.dubShortenNotJson")

        for key, value in payload.items():
            try:
                line_id = int(str(key).strip())
            except (TypeError, ValueError):
                continue
            if line_id not in originals:
                continue
            accepted = _shortened_line(originals[line_id], value, limits[line_id])
            if accepted is not None:
                results[line_id] = accepted

    logger.info("dub shortening: %s of %s lines rewritten", len(results), len(requests))
    return results
