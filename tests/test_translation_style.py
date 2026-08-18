import backend.domain.translation.style as style


def test_auto_picks_the_preset_the_source_language_implies():
    assert style.build_style_brief("auto", "zh").key == "han_viet"
    assert style.build_style_brief("auto", "zh-CN").key == "han_viet"
    assert style.build_style_brief("auto", "ko").key == "korean"
    assert style.build_style_brief("auto", "ja").key == "japanese"
    # Nothing special expected of a language with no house convention.
    assert style.build_style_brief("auto", "en").key == "neutral"
    assert style.build_style_brief("auto", None).key == "neutral"


def test_an_explicit_choice_beats_the_language():
    """Chinese audio, but the user wants it read like a fansub."""

    assert style.build_style_brief("genz", "zh").key == "genz"
    # An unknown key falls back to the language rather than failing a translation.
    assert style.build_style_brief("nonsense", "ko").key == "korean"


def test_the_chinese_preset_pins_the_terms_that_make_it_sound_right():
    brief = style.build_style_brief("auto", "zh")
    assert brief.glossary()["大哥"] == "đại ca"
    assert brief.glossary()["陛下"] == "bệ hạ"
    assert "大哥" in brief.pinned()


def test_notes_split_into_terms_and_rules():
    terms, rules = style.parse_style_notes(
        "大哥 → đại ca\n"
        "- 陛下 = bệ hạ\n"
        "Giọng trẻ, tránh từ Hán Việt nặng\n"
        "\n"
        "師傅 -> sư phụ"
    )
    assert terms == [("大哥", "đại ca"), ("陛下", "bệ hạ"), ("師傅", "sư phụ")]
    assert rules == ["Giọng trẻ, tránh từ Hán Việt nặng"]


def test_a_users_own_term_overrides_the_preset():
    brief = style.build_style_brief("han_viet", "zh", "大哥 → anh đại")
    assert brief.glossary()["大哥"] == "anh đại"
    # And no other preset's rules came along for the ride.
    assert "fansub" not in " ".join(brief.rules)


def test_free_text_notes_become_extra_rules():
    brief = style.build_style_brief("auto", "ko", "Xưng hô kiểu GenZ, câu ngắn")
    assert brief.rules[-1] == "Xưng hô kiểu GenZ, câu ngắn"
    assert brief.key == "korean"  # notes refine the preset, they do not replace it


def test_the_pinned_list_cannot_grow_past_the_prompt_budget():
    notes = "\n".join(f"term{index} → dịch{index}" for index in range(60))
    brief = style.build_style_brief("han_viet", "zh", notes)
    assert len(brief.terms) == style.PINNED_TERM_LIMIT
    # Trimming drops preset terms first — the user's own rules are the point.
    assert brief.glossary()["term59"] == "dịch59"
