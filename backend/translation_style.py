"""House style for translation: how a film should sound, not just what it says.

A literal translation can be correct and wrong at the same time — 大哥 really
does mean "anh cả", and a viewer of a Chinese period drama still expects "đại
ca". So style is carried into the prompt as two things: a handful of rules, and
a seed glossary that is *pinned*, meaning the model may extend it during the
film but never overwrite it.

Presets are picked from the source language by default because that is where the
expectation comes from, and any of it can be overridden per job.

The rules are written in English and the terms they cite are not: an instruction
is followed more reliably in English, while the words the audience expects to
hear are the output itself and only exist in the target language. Neither half
belongs in an interface catalogue — this is what the app translates *with*, not
text it shows. `label_code` is the exception, and it is only a key.
"""

from __future__ import annotations

from dataclasses import dataclass

STYLE_AUTO = "auto"
STYLE_NEUTRAL = "neutral"
# A user's own rule outranks a preset, but neither should be able to flood the
# prompt: the glossary rides along on every single batch.
PINNED_TERM_LIMIT = 40
TERM_LENGTH_LIMIT = 60
# One separator per line, and never ":" — prose uses it far too often.
TERM_SEPARATORS = ("→", "->", "=>", "=")


@dataclass(frozen=True)
class TranslationStyle:
    key: str
    # An interface key, resolved by the client. The rules below are prompt text
    # and stay exactly as written.
    label_code: str
    rules: tuple[str, ...]
    terms: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StyleBrief:
    """The resolved style for one translation run."""

    key: str
    rules: tuple[str, ...]
    terms: tuple[tuple[str, str], ...]

    def glossary(self) -> dict[str, str]:
        return dict(self.terms)

    def pinned(self) -> frozenset[str]:
        return frozenset(term for term, _translation in self.terms)


_BASE_RULES = (
    "Write each line the way a native speaker of the target language would say "
    "it out loud, not as a word-for-word rendering of the source.",
    "Keep lines short enough to be read within the cue's own duration; when the "
    "source is wordy, cut filler rather than compress meaning.",
)

STYLES: dict[str, TranslationStyle] = {
    STYLE_NEUTRAL: TranslationStyle(
        key=STYLE_NEUTRAL,
        label_code="style.neutral",
        rules=_BASE_RULES,
    ),
    "han_viet": TranslationStyle(
        key="han_viet",
        label_code="style.hanViet",
        rules=_BASE_RULES
        + (
            "This is a Chinese-language film for a Vietnamese audience: prefer "
            "the Sino-Vietnamese (Hán Việt) wording viewers already expect, and "
            "do not nativise it into everyday modern Vietnamese.",
            "Keep the genre's forms of address exactly as the audience knows "
            "them — đại ca, bệ hạ, sư phụ, tiểu thư, công tử, cô nương, tiền "
            "bối — and never flatten them into anh cả, vua, thầy, cô gái.",
            "Read personal names, titles and sect names with their "
            "Sino-Vietnamese pronunciation, and keep each one identical for the "
            "whole film once you have chosen it.",
            "Keep wuxia and xianxia terminology in its Sino-Vietnamese form: "
            "nội lực, khinh công, tu luyện, linh khí, độ kiếp.",
            "Apply the same convention to terms that are not in the glossary "
            "yet: choose the Sino-Vietnamese reading, then reuse it.",
        ),
        terms=(
            ("大哥", "đại ca"),
            ("二哥", "nhị ca"),
            ("陛下", "bệ hạ"),
            ("皇上", "hoàng thượng"),
            ("娘娘", "nương nương"),
            ("太子", "thái tử"),
            ("王爷", "vương gia"),
            ("大人", "đại nhân"),
            ("师父", "sư phụ"),
            ("师兄", "sư huynh"),
            ("师姐", "sư tỷ"),
            ("小姐", "tiểu thư"),
            ("公子", "công tử"),
            ("姑娘", "cô nương"),
            ("前辈", "tiền bối"),
            ("掌门", "chưởng môn"),
            ("江湖", "giang hồ"),
            ("内力", "nội lực"),
            ("轻功", "khinh công"),
        ),
    ),
    "korean": TranslationStyle(
        key="korean",
        label_code="style.korean",
        rules=_BASE_RULES
        + (
            "This is a Korean drama: keep the Korean forms of address the "
            "audience already knows — oppa, unnie, noona, hyung, sunbae, "
            "hoobae, ahjussi, ahjumma — instead of replacing them with the "
            "target language's own kinship pronouns.",
            "Keep the honorific -nim attached to a title, as in giám đốc-nim or "
            "bác sĩ-nim.",
            "Fall back to an ordinary pronoun only for lines that carry no such "
            "form of address at all.",
        ),
        terms=(
            ("오빠", "oppa"),
            ("언니", "unnie"),
            ("누나", "noona"),
            ("형", "hyung"),
            ("선배", "sunbae"),
            ("후배", "hoobae"),
            ("아저씨", "ahjussi"),
            ("아줌마", "ahjumma"),
            ("막내", "maknae"),
            ("대박", "daebak"),
        ),
    ),
    "japanese": TranslationStyle(
        key="japanese",
        label_code="style.japanese",
        rules=_BASE_RULES
        + (
            "This is a Japanese film or anime: keep the honorific suffixes -san, "
            "-kun, -chan and -sama on the name rather than translating them away.",
            "Keep the forms of address the audience knows in their romanised "
            "Japanese: senpai, kouhai, sensei, onii-chan, onee-chan.",
        ),
        terms=(
            ("先輩", "senpai"),
            ("後輩", "kouhai"),
            ("先生", "sensei"),
            ("お兄ちゃん", "onii-chan"),
            ("お姉ちゃん", "onee-chan"),
            ("様", "-sama"),
            ("さん", "-san"),
            ("ちゃん", "-chan"),
            ("くん", "-kun"),
        ),
    ),
    "genz": TranslationStyle(
        key="genz",
        label_code="style.genz",
        rules=_BASE_RULES
        + (
            "Use the young, colloquial register of a fansub: relaxed forms of "
            "address, short sentences, and widely understood slang where the "
            "scene invites it — in Vietnamese that means words like xỉu, chill, "
            "cạn lời, u là trời.",
            "Drop the slang for serious scenes, and never add emoji or "
            "chat-style abbreviations.",
        ),
    ),
    "formal": TranslationStyle(
        key="formal",
        label_code="style.formal",
        rules=_BASE_RULES
        + (
            "Use a formal, standard register suited to documentaries and news "
            "broadcasts.",
            "No slang, little colloquial phrasing, and domain terminology kept "
            "precise rather than paraphrased.",
        ),
    ),
}

# What "auto" means, read off the language the transcript came back in.
LANGUAGE_STYLES = {
    "zh": "han_viet",
    "yue": "han_viet",
    "cmn": "han_viet",
    "ko": "korean",
    "ja": "japanese",
}


def style_options() -> list[dict[str, str]]:
    """The picker the UI renders, auto first — keys only, never rendered text."""

    return [{"value": STYLE_AUTO, "label_code": "style.auto"}] + [
        {"value": style.key, "label_code": style.label_code} for style in STYLES.values()
    ]


def style_for_language(source_language: str | None) -> TranslationStyle:
    code = str(source_language or "").strip().lower().replace("_", "-").split("-")[0]
    return STYLES[LANGUAGE_STYLES.get(code, STYLE_NEUTRAL)]


def parse_style_notes(notes: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split a user's notes into glossary terms and free-text rules.

    `大哥 → đại ca` is a term the model must not drift away from; anything else
    on its own line is another rule for the prompt. Writing rules as prose is
    still fine — only lines shaped like a mapping are read as terms.
    """

    terms: list[tuple[str, str]] = []
    rules: list[str] = []
    for raw_line in str(notes or "").splitlines():
        line = raw_line.strip().lstrip("-•*").strip()
        if not line:
            continue
        separator = next((sep for sep in TERM_SEPARATORS if sep in line), None)
        if separator is None:
            rules.append(line)
            continue
        source, _, translation = line.partition(separator)
        source, translation = source.strip(), translation.strip()
        if (
            source
            and translation
            and len(source) <= TERM_LENGTH_LIMIT
            and len(translation) <= TERM_LENGTH_LIMIT
        ):
            terms.append((source, translation))
        else:
            rules.append(line)
    return terms, rules


def build_style_brief(
    style_key: str | None,
    source_language: str | None = None,
    notes: str = "",
) -> StyleBrief:
    """Resolve the style for one run: preset, then the user's own rules on top."""

    key = str(style_key or STYLE_AUTO).strip().lower()
    style = style_for_language(source_language) if key in {"", STYLE_AUTO} else (
        STYLES.get(key) or style_for_language(source_language)
    )

    note_terms, note_rules = parse_style_notes(notes)
    merged: dict[str, str] = dict(style.terms)
    for term, translation in note_terms:
        # The user's own mapping wins, and moves to the end so that trimming
        # drops preset terms before it drops theirs.
        merged.pop(term, None)
        merged[term] = translation
    while len(merged) > PINNED_TERM_LIMIT:
        del merged[next(iter(merged))]

    return StyleBrief(
        key=style.key,
        rules=style.rules + tuple(note_rules),
        terms=tuple(merged.items()),
    )
