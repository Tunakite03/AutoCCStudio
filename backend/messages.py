"""Codes instead of sentences for anything the interface will show.

A backend that formats prose picks the language for every client it will ever
have. So nothing here writes a sentence: an error, a progress tick and an option
label all travel as a stable code plus the values that fill its blanks, and the
client's catalogue turns the pair into text.

The codes are the contract. Renaming one changes what a user reads and has to be
done on both sides; adding a param does not. A param may itself be a `Message`,
which is how a provider failure keeps its own specific cause while being
reported as, say, a translation failure.

`str()` of anything here renders the code, not a sentence — that form is for log
lines and tracebacks only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Wraps text that has no code of its own: an ffmpeg stderr tail, a provider's
# own error body, a parser's complaint. The catalogue renders it verbatim, so it
# stays the escape hatch rather than the habit.
RAW = "err.raw"


@dataclass(frozen=True)
class Message:
    """A catalogue key and the values its text interpolates."""

    code: str
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape a client receives, nested causes included."""

        return {
            "code": self.code,
            "params": {
                name: value.as_dict() if isinstance(value, Message) else value
                for name, value in self.params.items()
            },
        }

    def __str__(self) -> str:
        if not self.params:
            return self.code
        rendered = ", ".join(f"{name}={value}" for name, value in self.params.items())
        return f"{self.code}({rendered})"


def message(code: str | Message, **params: Any) -> Message:
    """A message from a code, or one adopted as it is.

    Adoption is what lets a layer relabel a failure without discarding it: the
    HTTP client's `err.http.rateLimited` stays intact when the translator raises
    it as its own error.
    """

    if isinstance(code, Message):
        return Message(code.code, {**code.params, **params}) if params else code
    return Message(str(code), params)


def raw(text: str) -> Message:
    """Text that is already final — a provider's own words, not ours."""

    return Message(RAW, {"text": str(text)})


def detail(code: str | Message, **params: Any) -> dict[str, Any]:
    """The body of an HTTP error response: `{"code": ..., "params": {...}}`."""

    return message(code, **params).as_dict()


class CodedError(Exception):
    """An error whose wording lives in the client's catalogue.

    Subclassed rather than used directly, so `except FFmpegError` still says
    which layer failed while every one of them carries a code.
    """

    def __init__(self, code: str | Message, **params: Any) -> None:
        self.message = message(code, **params)
        super().__init__(str(self.message))

    @property
    def code(self) -> str:
        return self.message.code

    @property
    def params(self) -> dict[str, Any]:
        return self.message.params
