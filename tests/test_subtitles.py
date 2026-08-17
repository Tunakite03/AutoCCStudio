from backend.subtitles import (
    CJK_STYLE,
    LATIN_STYLE,
    balance_lines,
    enforce_cue_timing,
    format_subtitle,
    is_cjk_text,
    join_tokens,
    merge_short_cues,
    parse_subtitle,
    parse_timestamp,
    split_long_cue,
    split_long_cues,
    split_text_into_chunks,
    strip_speaker_labels,
    style_for_text,
)


def test_parse_srt_keeps_lines_and_normalizes_ids():
    content = """1
00:00:01,250 --> 00:00:03,500
Hello
world

7
00:00:04,000 --> 00:00:05,100
Second cue
"""
    cues = parse_subtitle(content, ".srt")
    assert cues == [
        {
            "id": 1,
            "start": 1.25,
            "end": 3.5,
            "text": "Hello\nworld",
            "translation": "",
        },
        {
            "id": 2,
            "start": 4.0,
            "end": 5.1,
            "text": "Second cue",
            "translation": "",
        },
    ]


def test_parse_vtt_supports_short_timestamps_and_settings():
    content = """WEBVTT

00:01.000 --> 00:03.250 align:start
Xin chào
"""
    cues = parse_subtitle(content, "vtt")
    assert cues[0]["start"] == 1.0
    assert cues[0]["end"] == 3.25
    assert cues[0]["text"] == "Xin chào"


def test_format_translation_to_srt_and_vtt():
    cues = [{"start": 0, "end": 1.5, "text": "Hello", "translation": "Xin chào"}]
    assert "00:00:00,000 --> 00:00:01,500" in format_subtitle(cues, "srt", "translated")
    vtt = format_subtitle(cues, "vtt", "translated")
    assert vtt.startswith("WEBVTT\n")
    assert "00:00:00.000 --> 00:00:01.500" in vtt
    assert "Xin chào" in vtt


def test_format_removes_legacy_speaker_labels_and_keeps_line_breaks():
    cues = [
        {
            "start": 0,
            "end": 2,
            "text": "[1] How much?\n[S2] The fare is $5.50.",
            "translation": "",
        }
    ]

    output = format_subtitle(cues, "srt")

    assert "[1]" not in output
    assert "[S2]" not in output
    assert "How much?\nThe fare is $5.50." in output
    assert strip_speaker_labels("[S1] Hello\n[2] Hi") == "Hello\nHi"


def test_timestamp_rounding_is_millisecond_safe():
    assert parse_timestamp("01:02:03.45") == 3723.45
    assert parse_timestamp("02:03.4") == 123.4


def test_balance_lines_limits_to_max_two_lines_and_balances():
    short_text = "How's everything coming along?"
    assert balance_lines(short_text, max_line_len=40) == "How's everything coming along?"

    long_text = "The Shogun is coming to visit in one week and everyone knows he is trying to decide."
    balanced = balance_lines(long_text, max_line_len=42, max_lines=2)
    lines = balanced.split("\n")
    assert len(lines) == 2
    assert len(lines[0]) <= 50
    assert len(lines[1]) <= 50


def test_split_long_cues_splits_long_monologue_into_short_cues():
    cue = {
        "id": 1,
        "start": 164.545,
        "end": 187.165,  # 22.62s
        "text": (
            "How's everything coming along? The Shogun is coming to visit in one week. "
            "Everyone knows he's trying to decide who his success will be. "
            "And if I have anything to do with it, it'll be me."
        ),
        "translation": "",
    }

    split_cues = split_long_cues([cue], max_chars=75, max_duration=6.0, max_lines=2)

    assert len(split_cues) >= 3
    assert split_cues[0]["start"] == 164.545
    assert split_cues[-1]["end"] == 187.165
    for c in split_cues:
        assert c["text"].count("\n") <= 1
        assert len(c["text"]) <= 75
        assert (c["end"] - c["start"]) <= 7.0



# ── Script awareness ─────────────────────────────────────────────────


def test_a_chinese_line_is_measured_as_chinese():
    assert is_cjk_text("不管这香炉怎么滚动") is True
    # A Chinese line carrying a Latin name is still a Chinese line.
    assert is_cjk_text("长平香炉 Level 3") is True
    assert is_cjk_text("How's everything coming along?") is False
    assert style_for_text("不管这香炉怎么滚动") is CJK_STYLE
    assert style_for_text("How's everything?") is LATIN_STYLE


def test_recognised_words_join_without_a_space_in_chinese():
    assert join_tokens(["看清楚", "了吧"]) == "看清楚了吧"
    assert join_tokens(["How's", "everything"]) == "How's everything"
    # A trailing mark stays glued to the clause it closes.
    assert join_tokens(["装香料的托盘", "，", "香料和炉灰"]) == "装香料的托盘，香料和炉灰"


def test_balance_lines_wraps_chinese_that_has_no_spaces_to_break_on():
    text = "不管这香炉怎么滚动装香料的托盘和炉灰都不会撒出去"
    balanced = balance_lines(text)

    lines = balanced.split("\n")
    assert len(lines) == 2
    assert all(len(line) <= CJK_STYLE.max_line_chars for line in lines)
    assert "".join(lines) == text


def test_balance_lines_never_starts_a_chinese_line_with_a_closing_mark():
    text = "不管这香炉怎么滚动，装香料的托盘和炉灰都不会撒出去。"
    for line in balance_lines(text).split("\n"):
        assert line[0] not in "，。？！、；："


def test_split_text_into_chunks_splits_chinese_on_its_own_punctuation():
    text = "不管这香炉怎么滚动，装香料的托盘和炉灰都不会撒出去。这就是长平香炉。"
    chunks = split_text_into_chunks(text)

    assert len(chunks) >= 2
    assert all(len(chunk) <= CJK_STYLE.max_chars for chunk in chunks)
    assert "".join(chunks) == text


def test_split_text_into_chunks_can_still_cut_chinese_without_punctuation():
    text = "不" * 90
    chunks = split_text_into_chunks(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= CJK_STYLE.max_chars for chunk in chunks)


# ── Fragment repair ──────────────────────────────────────────────────


def test_merge_short_cues_joins_fragments_of_one_sentence():
    cues = [
        {"id": 1, "start": 91.3, "end": 92.1, "text": "看清楚", "speaker": 0},
        {"id": 2, "start": 92.1, "end": 92.9, "text": "了吧", "speaker": 0},
    ]

    merged = merge_short_cues(cues)

    assert len(merged) == 1
    assert merged[0]["text"] == "看清楚了吧"
    assert merged[0]["start"] == 91.3
    assert merged[0]["end"] == 92.9


def test_merge_short_cues_refuses_a_merge_that_would_not_be_readable():
    # Both sides are already at the budget, so joining them would trade two
    # readable cues for one unreadable one.
    long_line = "不管这香炉怎么滚动装香料的托盘都不会"
    cues = [
        {"id": 1, "start": 0.0, "end": 0.9, "text": long_line, "speaker": 0},
        {"id": 2, "start": 0.9, "end": 1.8, "text": long_line, "speaker": 0},
    ]

    assert len(merge_short_cues(cues)) == 2


def test_merge_short_cues_keeps_two_speakers_apart():
    cues = [
        {"id": 1, "start": 0.0, "end": 0.6, "text": "看清楚", "speaker": 0},
        {"id": 2, "start": 0.6, "end": 1.2, "text": "了吧", "speaker": 1},
    ]

    assert len(merge_short_cues(cues)) == 2


def test_merge_short_cues_leaves_a_finished_readable_sentence_alone():
    cues = [
        {"id": 1, "start": 0.0, "end": 2.5, "text": "How's everything coming along?"},
        {"id": 2, "start": 2.6, "end": 3.0, "text": "Fine."},
    ]

    assert len(merge_short_cues(cues)) == 2


def test_enforce_cue_timing_gives_a_flash_cue_a_readable_duration():
    cues = [
        {"id": 1, "start": 102.575, "end": 103.215, "text": "灰"},
        {"id": 2, "start": 106.975, "end": 107.695, "text": "什么"},
    ]

    fixed = enforce_cue_timing(cues, media_duration=200.0)

    for cue in fixed:
        assert cue["end"] - cue["start"] >= CJK_STYLE.min_duration


def test_enforce_cue_timing_borrows_from_silence_and_not_from_the_next_cue():
    cues = [
        {"id": 1, "start": 0.0, "end": 0.5, "text": "灰"},
        {"id": 2, "start": 0.9, "end": 3.0, "text": "不会撒出去长平香炉"},
    ]

    fixed = enforce_cue_timing(cues, media_duration=10.0)

    assert fixed[0]["end"] <= fixed[1]["start"]
    assert fixed[1]["start"] == 0.9


def test_chinese_cues_are_split_to_a_readable_line_length():
    cue = {
        "id": 1,
        "start": 0.0,
        "end": 12.0,
        "text": "不管这香炉怎么滚动，装香料的托盘和炉灰都不会撒出去。这就是长平香炉。",
        "translation": "",
    }

    split_cues = split_long_cues([cue])

    assert len(split_cues) >= 2
    for item in split_cues:
        assert item["text"].count("\n") <= 1
        for line in item["text"].split("\n"):
            assert len(line) <= CJK_STYLE.max_line_chars
