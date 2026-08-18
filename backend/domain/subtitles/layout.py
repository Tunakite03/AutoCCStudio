"""Pure subtitle dialogue-layout normalization and validation.

This module intentionally knows nothing about LLMs or providers. Translation,
speaker analysis, imports, and future editor-side validation can share the same
rules without depending on one another's implementation modules.
"""

from __future__ import annotations

import re

from .parser import strip_speaker_labels


def clean_dialogue_layout(text: str) -> str:
    """Remove legacy labels and normalize non-empty dialogue lines."""

    without_labels = strip_speaker_labels(str(text)).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.strip() for line in without_labels.split("\n") if line.strip())


def same_dialogue_content(left: str, right: str) -> bool:
    """Whether two layouts contain the same characters modulo whitespace."""

    def collapse(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    return collapse(left) == collapse(right)


def dialogue_break_positions(text: str) -> set[int]:
    """Word offsets after which the normalized layout contains a line break."""

    positions: set[int] = set()
    word_count = 0
    lines = clean_dialogue_layout(text).splitlines()
    for line in lines[:-1]:
        word_count += len(re.findall(r"\S+", line))
        if word_count:
            positions.add(word_count)
    return positions
