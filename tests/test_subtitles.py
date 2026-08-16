from backend.subtitles import (
    balance_lines,
    format_subtitle,
    parse_subtitle,
    parse_timestamp,
    split_long_cue,
    split_long_cues,
    split_text_into_chunks,
    strip_speaker_labels,
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

