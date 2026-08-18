"""Translation provider contracts and the built-in adapter registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

TRANSLATION_MOCK = "mock"
TRANSLATION_TRANSFORMERS = "transformers"
TRANSLATION_OPENAI_COMPATIBLE = "openai_compatible"


class TranslationProvider(Protocol):
    name: str

    def translate_batch(
        self,
        lines: list[dict],
        target_language: str,
        *,
        context_before: list[dict],
        context_after: list[dict],
        glossary: dict[str, str] | None,
        style: Any,
        model: str | None,
    ) -> list[str]: ...


@dataclass(frozen=True)
class MockTranslationProvider:
    name: str = TRANSLATION_MOCK

    def translate_batch(
        self,
        lines: list[dict],
        target_language: str,
        **_options,
    ) -> list[str]:
        return [f"[{target_language}] {line.get('text', '')}" for line in lines]


@dataclass(frozen=True)
class TransformersTranslationProvider:
    name: str = TRANSLATION_TRANSFORMERS

    def translate_batch(
        self,
        lines: list[dict],
        target_language: str,
        *,
        model: str | None,
        **_options,
    ) -> list[str]:
        from ...ai import llm as implementation

        texts = [str(line.get("text", "")) for line in lines]
        return implementation._translate_batch_transformers(
            texts,
            target_language,
            model_name=model,
        )


@dataclass(frozen=True)
class OpenAICompatibleTranslationProvider:
    name: str = TRANSLATION_OPENAI_COMPATIBLE

    def translate_batch(
        self,
        lines: list[dict],
        target_language: str,
        *,
        context_before: list[dict],
        context_after: list[dict],
        glossary: dict[str, str] | None,
        style: Any,
        model: str | None,
    ) -> list[str]:
        from ...ai import translation as implementation

        return implementation._translate_batch_llm(
            lines,
            target_language,
            context_before=context_before,
            context_after=context_after,
            glossary=glossary,
            style=style,
            model=model,
        )


_PROVIDERS: dict[str, TranslationProvider] = {
    TRANSLATION_MOCK: MockTranslationProvider(),
    TRANSLATION_TRANSFORMERS: TransformersTranslationProvider(),
    TRANSLATION_OPENAI_COMPATIBLE: OpenAICompatibleTranslationProvider(),
}


def resolve_translation_provider_name(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in {TRANSLATION_MOCK, TRANSLATION_TRANSFORMERS}:
        return normalized
    return TRANSLATION_OPENAI_COMPATIBLE


def get_translation_provider(name: str) -> TranslationProvider:
    return _PROVIDERS[resolve_translation_provider_name(name)]
