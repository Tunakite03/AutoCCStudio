"""House style for translation: how a film should sound, not just what it says.

A literal translation can be correct and wrong at the same time — 大哥 really
does mean "anh cả", and a viewer of a Chinese period drama still expects "đại
ca". So style is carried into the prompt as two things: a handful of rules, and
a seed glossary that is *pinned*, meaning the model may extend it during the
film but never overwrite it.

Presets are picked from the source language by default because that is where the
expectation comes from, and any of it can be overridden per job.
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
    label: str
    rules: tuple[str, ...]
    terms: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StyleBrief:
    """The resolved style for one translation run."""

    key: str
    label: str
    rules: tuple[str, ...]
    terms: tuple[tuple[str, str], ...]

    def glossary(self) -> dict[str, str]:
        return dict(self.terms)

    def pinned(self) -> frozenset[str]:
        return frozenset(term for term, _translation in self.terms)


_BASE_RULES = (
    "Giữ văn phong tự nhiên như người Việt nói, không dịch bám từng chữ.",
    "Câu ngắn gọn để kịp đọc trong thời lượng của cue.",
)

STYLES: dict[str, TranslationStyle] = {
    STYLE_NEUTRAL: TranslationStyle(
        key=STYLE_NEUTRAL,
        label="Trung tính",
        rules=_BASE_RULES,
    ),
    "han_viet": TranslationStyle(
        key="han_viet",
        label="Hán Việt (phim Trung)",
        rules=_BASE_RULES
        + (
            "Phim Hoa ngữ: ưu tiên từ Hán Việt mà khán giả Việt đã quen, không "
            "Việt hóa hoàn toàn.",
            "Giữ nguyên cách xưng hô đặc trưng — đại ca, bệ hạ, sư phụ, tiểu thư, "
            "công tử, cô nương, tiền bối — không đổi thành anh cả, vua, thầy, cô gái.",
            "Tên người và tên môn phái đọc theo âm Hán Việt, nhất quán suốt phim.",
            "Thuật ngữ võ hiệp/tiên hiệp giữ dạng Hán Việt: nội lực, khinh công, "
            "tu luyện, linh khí, độ kiếp.",
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
        label="Giữ oppa, sunbae (phim Hàn)",
        rules=_BASE_RULES
        + (
            "Phim Hàn: giữ nguyên cách gọi tiếng Hàn mà khán giả Việt đã quen "
            "(oppa, unnie, noona, hyung, sunbae, hoobae, ahjussi, ahjumma), không "
            "đổi thành anh/chị/cô/chú.",
            "Kính ngữ -nim giữ nguyên khi đi kèm chức danh: giám đốc-nim, bác sĩ-nim.",
            "Chỉ dùng đại từ tiếng Việt khi câu không có cách gọi đặc trưng nào.",
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
        label="Giữ senpai, -san (phim Nhật)",
        rules=_BASE_RULES
        + (
            "Phim/anime Nhật: giữ hậu tố kính ngữ -san, -kun, -chan, -sama thay vì "
            "dịch sang tiếng Việt.",
            "Giữ các cách gọi quen thuộc: senpai, kouhai, sensei, onii-chan, "
            "onee-chan.",
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
        label="GenZ, khẩu ngữ",
        rules=_BASE_RULES
        + (
            "Giọng trẻ, khẩu ngữ như phụ đề fansub: xưng hô thoải mái, câu ngắn, "
            "dùng được tiếng lóng phổ biến (xỉu, chill, cạn lời, u là trời) khi "
            "đúng ngữ cảnh.",
            "Không lạm dụng tiếng lóng ở cảnh nghiêm túc, không chèn emoji, không "
            "viết tắt kiểu chat.",
        ),
    ),
    "formal": TranslationStyle(
        key="formal",
        label="Trang trọng (tài liệu, tin tức)",
        rules=_BASE_RULES
        + (
            "Văn phong trang trọng, chuẩn mực, hợp phim tài liệu và bản tin.",
            "Không dùng tiếng lóng, hạn chế khẩu ngữ, giữ thuật ngữ chuyên ngành "
            "chính xác.",
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
    """The picker the UI renders, auto first."""

    return [{"value": STYLE_AUTO, "label": "Tự động theo ngôn ngữ nguồn"}] + [
        {"value": style.key, "label": style.label} for style in STYLES.values()
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
        label=style.label,
        rules=style.rules + tuple(note_rules),
        terms=tuple(merged.items()),
    )
