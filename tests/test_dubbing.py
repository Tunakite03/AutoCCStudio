"""The dubbing pass: fitting a line to its cue, and laying the track out.

Almost everything here runs on the `mock` voice. Fitting, caching, assembly and
drift are arithmetic over durations — none of them get more true for having gone
through a real provider, and all of them get slower and flakier for it.
"""

import wave
from dataclasses import replace

import pytest

import backend.ai as ai_module
import backend.dubbing as dubbing
from backend.dubbing import (
    FIT_EXACT,
    FIT_OVERFLOW,
    FIT_SPED_UP,
    FIT_SPILL,
    FitPolicy,
    assemble_track,
    budget_for,
    dub_cues,
    dub_text,
    fit_segment,
)
from backend.media import DUB_SAMPLE_RATE, DUB_SAMPLE_WIDTH, find_ffmpeg

POLICY = FitPolicy()


def pcm_of(seconds: float) -> bytes:
    return b"\x01\x00" * int(seconds * DUB_SAMPLE_RATE)


def wav_seconds(path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


# ── What a cue is allowed to take ────────────────────────────────────


def test_budget_stops_short_of_the_next_cue():
    cues = [{"start": 0.0, "end": 2.0}, {"start": 3.0, "end": 4.0}]
    hard, spill = budget_for(cues, 0, POLICY)
    assert hard == 2.0
    # One second of silence, minus the guard that keeps the line off the next cue.
    assert spill == pytest.approx(2.0 + (1.0 - POLICY.spill_guard))


def test_back_to_back_cues_leave_no_room_to_spill():
    cues = [{"start": 0.0, "end": 2.0}, {"start": 2.0, "end": 4.0}]
    hard, spill = budget_for(cues, 0, POLICY)
    assert hard == 2.0
    assert spill == 2.0


def test_the_last_cue_may_run_on_but_only_so_far():
    hard, spill = budget_for([{"start": 0.0, "end": 2.0}], 0, POLICY)
    assert spill == pytest.approx(hard + POLICY.max_spill)


def test_a_long_gap_is_capped_rather_than_taken_whole():
    cues = [{"start": 0.0, "end": 1.0}, {"start": 60.0, "end": 61.0}]
    _hard, spill = budget_for(cues, 0, POLICY)
    assert spill == pytest.approx(1.0 + POLICY.max_spill)


def test_a_cue_with_no_duration_still_gets_a_floor():
    hard, _spill = budget_for([{"start": 1.0, "end": 1.0}], 0, POLICY)
    assert hard == POLICY.min_budget


# ── The three strategies, in order ───────────────────────────────────


def test_a_line_that_already_fits_is_left_alone():
    decision = fit_segment(1.5, 2.0, 3.0, POLICY)
    assert decision.fit == FIT_EXACT
    assert decision.tempo == 1.0


def test_a_slightly_long_line_is_sped_up_rather_than_spilled():
    """Strategy one. Sync is worth more than the 10% of naturalness it costs."""

    decision = fit_segment(2.2, 2.0, 4.0, POLICY)
    assert decision.fit == FIT_SPED_UP
    assert decision.tempo == pytest.approx(1.1)
    assert decision.target_chars is None


def test_an_inaudible_overrun_spills_instead_of_being_re_encoded():
    decision = fit_segment(2.01, 2.0, 3.0, POLICY)
    assert decision.fit == FIT_SPILL
    assert decision.tempo == 1.0


def test_a_line_past_the_speed_limit_uses_the_silence_after_it():
    """Strategy two: too long to speed up, short enough for the gap."""

    decision = fit_segment(2.8, 2.0, 3.0, POLICY)
    assert decision.fit == FIT_SPILL
    assert decision.tempo == 1.0


def test_a_longer_line_is_sped_up_into_the_gap_before_the_llm_is_asked():
    decision = fit_segment(3.5, 2.0, 3.0, POLICY)
    assert decision.fit == FIT_SPED_UP
    assert decision.tempo == pytest.approx(3.5 / 3.0)
    assert decision.target_chars is None


def test_only_a_line_neither_trick_can_save_reaches_the_llm():
    """Strategy three, and the character budget it is given."""

    decision = fit_segment(8.0, 2.0, 3.0, POLICY, text_length=100)
    assert decision.fit == FIT_OVERFLOW
    assert decision.tempo == POLICY.max_speedup
    # Room after the speed-up is 3.0 * 1.25 of 8.0 seconds' worth of text.
    assert decision.target_chars == int(100 * (3.0 * 1.25 / 8.0) * POLICY.shorten_margin)


def test_shortening_is_not_proposed_when_it_is_turned_off():
    decision = fit_segment(8.0, 2.0, 3.0, POLICY, text_length=100, can_shorten=False)
    assert decision.fit == FIT_OVERFLOW
    assert decision.target_chars is None


def test_a_line_already_at_the_floor_is_not_asked_to_shrink_further():
    decision = fit_segment(30.0, 0.3, 0.3, POLICY, text_length=6)
    assert decision.fit == FIT_OVERFLOW
    assert decision.target_chars is None


# ── Assembling the track ─────────────────────────────────────────────


def test_segments_land_on_their_own_timestamps(tmp_path):
    track, drift = assemble_track(
        [(2.0, pcm_of(0.5)), (0.0, pcm_of(1.0))], tmp_path / "track.wav"
    )
    assert drift == 0.0
    with wave.open(str(track), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    assert len(frames) == int(2.5 * DUB_SAMPLE_RATE) * DUB_SAMPLE_WIDTH
    # One second of tone, one of silence, then half a second of tone.
    assert frames[: DUB_SAMPLE_RATE * DUB_SAMPLE_WIDTH].strip(b"\x00")
    silence = frames[DUB_SAMPLE_RATE * DUB_SAMPLE_WIDTH : 2 * DUB_SAMPLE_RATE * DUB_SAMPLE_WIDTH]
    assert silence == b"\x00" * len(silence)


def test_an_overlapping_segment_is_pushed_late_and_reported(tmp_path):
    """Two voices at once is worse than one arriving late — but say so."""

    _track, drift = assemble_track(
        [(0.0, pcm_of(1.0)), (0.5, pcm_of(0.5))], tmp_path / "track.wav"
    )
    assert drift == pytest.approx(0.5)


def test_the_track_is_padded_to_the_length_of_the_project(tmp_path):
    track, _drift = assemble_track(
        [(0.0, pcm_of(0.5))], tmp_path / "track.wav", tail_seconds=9.0
    )
    assert wav_seconds(track) == pytest.approx(9.0)


def test_a_track_is_never_trimmed_to_the_tail(tmp_path):
    """A line that runs past the last cue keeps its ending."""

    track, _drift = assemble_track(
        [(0.0, pcm_of(4.0))], tmp_path / "track.wav", tail_seconds=1.0
    )
    assert wav_seconds(track) == pytest.approx(4.0)


# ── What gets voiced ─────────────────────────────────────────────────


def test_a_cue_is_voiced_from_its_translation():
    assert dub_text({"text": "Hello there", "translation": "Xin chào"}) == "Xin chào"


def test_an_untranslated_cue_is_read_in_its_own_language():
    assert dub_text({"text": "Hello there", "translation": "  "}) == "Hello there"


def test_speaker_line_breaks_become_one_spoken_line():
    assert dub_text({"translation": "Ai đó?\nLà tôi."}) == "Ai đó? Là tôi."


def test_an_empty_cue_is_not_voiced():
    assert dub_text({"text": "", "translation": ""}) == ""


# ── End to end, on the mock voice ────────────────────────────────────

SAMPLE_CUES = [
    {"start": 0.0, "end": 4.0, "text": "", "translation": "Xin chào các bạn"},
    {"start": 6.0, "end": 10.0, "text": "", "translation": "Hôm nay trời rất đẹp"},
    {"start": 12.0, "end": 16.0, "text": "", "translation": ""},
]


def test_a_run_voices_every_cue_that_has_something_to_say(tmp_path):
    track, report = dub_cues(
        SAMPLE_CUES,
        job_dir=tmp_path,
        voice="mock",
        provider="mock",
        shorten=False,
    )
    assert track.exists()
    assert report["total_cues"] == 3
    assert report["voiced_cues"] == 2
    assert report["failed_cues"] == 0
    assert report["cached_cues"] == 0
    assert sum(report["fits"].values()) == 2
    # Padded out to the end of the last cue, empty or not.
    assert wav_seconds(track) == pytest.approx(16.0, abs=0.05)


def test_a_second_run_reuses_every_segment_it_already_rendered(tmp_path):
    dub_cues(SAMPLE_CUES, job_dir=tmp_path, voice="mock", provider="mock", shorten=False)
    _track, report = dub_cues(
        SAMPLE_CUES, job_dir=tmp_path, voice="mock", provider="mock", shorten=False
    )
    assert report["cached_cues"] == report["voiced_cues"] == 2


def test_changing_the_voice_invalidates_the_cache(tmp_path):
    dub_cues(SAMPLE_CUES, job_dir=tmp_path, voice="mock", provider="mock", shorten=False)
    _track, report = dub_cues(
        SAMPLE_CUES, job_dir=tmp_path, voice="other", provider="mock", shorten=False
    )
    assert report["cached_cues"] == 0


def test_a_project_with_nothing_to_say_is_refused(tmp_path):
    with pytest.raises(dubbing.DubbingError):
        dub_cues(
            [{"start": 0.0, "end": 2.0, "text": "", "translation": ""}],
            job_dir=tmp_path,
            voice="mock",
            provider="mock",
            shorten=False,
        )


def test_one_refused_line_does_not_cost_the_rest_of_the_project(tmp_path, monkeypatch):
    original = dubbing.tts.synthesize

    def flaky(text, voice, stem, **kwargs):
        if "đẹp" in text:
            raise dubbing.tts.TTSProviderError("err.tts.requestFailed")
        return original(text, voice, stem, **kwargs)

    monkeypatch.setattr(dubbing.tts, "synthesize", flaky)
    _track, report = dub_cues(
        SAMPLE_CUES, job_dir=tmp_path, voice="mock", provider="mock", shorten=False
    )
    assert report["voiced_cues"] == 1
    assert report["failed_cues"] == 1


def test_a_line_that_will_not_fit_is_rewritten_and_re_recorded(tmp_path, monkeypatch):
    # 0.4s of cue for a line that takes the mock voice about three seconds.
    cues = [
        {
            "start": 0.0,
            "end": 0.4,
            "text": "",
            "translation": "Đây là một câu thoại rất dài không thể nào đọc kịp",
        }
    ]
    asked: list[dict] = []

    def fake_shorten(items, target_language=None, **kwargs):
        asked.extend(items)
        return {item["id"]: "Câu ngắn" for item in items}

    monkeypatch.setattr(ai_module, "shorten_for_dubbing", fake_shorten)
    _track, report = dub_cues(
        cues, job_dir=tmp_path, voice="mock", provider="mock", shorten=True
    )
    assert report["shortened_cues"] == 1
    assert asked and asked[0]["max_chars"] < len(cues[0]["translation"])


def test_shortening_can_be_turned_off_entirely(tmp_path, monkeypatch):
    cues = [
        {
            "start": 0.0,
            "end": 0.4,
            "text": "",
            "translation": "Đây là một câu thoại rất dài không thể nào đọc kịp",
        }
    ]

    def explode(*_args, **_kwargs):
        raise AssertionError("the LLM must not be asked when shortening is off")

    monkeypatch.setattr(ai_module, "shorten_for_dubbing", explode)
    _track, report = dub_cues(
        cues, job_dir=tmp_path, voice="mock", provider="mock", shorten=False
    )
    assert report["shortened_cues"] == 0
    assert report["fits"][FIT_OVERFLOW] == 1


def test_a_failed_shortening_pass_still_produces_a_track(tmp_path, monkeypatch):
    cues = [
        {
            "start": 0.0,
            "end": 0.4,
            "text": "",
            "translation": "Đây là một câu thoại rất dài không thể nào đọc kịp",
        }
    ]

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(ai_module, "shorten_for_dubbing", unavailable)
    track, report = dub_cues(
        cues, job_dir=tmp_path, voice="mock", provider="mock", shorten=True
    )
    assert track.exists()
    assert report["shortened_cues"] == 0


def test_a_stop_request_is_honoured_inside_the_synthesis_pool(tmp_path):
    from backend.cancellation import OperationCancelled

    def stop():
        raise OperationCancelled("test")

    with pytest.raises(OperationCancelled):
        dub_cues(
            SAMPLE_CUES,
            job_dir=tmp_path,
            voice="mock",
            provider="mock",
            shorten=False,
            stop_check=stop,
        )


# ── Asking the model for fewer words ─────────────────────────────────


def test_shortening_keeps_only_the_rewrites_that_are_actually_shorter(monkeypatch):
    monkeypatch.setattr(
        ai_module,
        "_llm_completion",
        lambda *args, **kwargs: '{"1": "Ngắn", "2": "Dài hơn hẳn bản gốc rất nhiều"}',
    )
    result = ai_module.shorten_for_dubbing(
        [
            {"id": 1, "text": "Một câu khá dài", "max_chars": 8},
            {"id": 2, "text": "Ngắn thôi", "max_chars": 6},
        ],
        "Tiếng Việt",
    )
    assert result == {1: "Ngắn"}


def test_shortening_rejects_a_rewrite_that_threw_the_line_away(monkeypatch):
    monkeypatch.setattr(ai_module, "_llm_completion", lambda *args, **kwargs: '{"1": "Ừ"}')
    result = ai_module.shorten_for_dubbing(
        [{"id": 1, "text": "Một câu rất dài với nhiều mệnh đề khác nhau", "max_chars": 30}],
        "Tiếng Việt",
    )
    assert result == {}


def test_shortening_ignores_ids_it_was_never_given(monkeypatch):
    monkeypatch.setattr(
        ai_module, "_llm_completion", lambda *args, **kwargs: '{"99": "Ngắn"}'
    )
    result = ai_module.shorten_for_dubbing(
        [{"id": 1, "text": "Một câu khá dài", "max_chars": 8}], "Tiếng Việt"
    )
    assert result == {}


def test_nothing_is_asked_of_the_model_when_there_is_nothing_to_shorten(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("no call should be made")

    monkeypatch.setattr(ai_module, "_llm_completion", explode)
    assert ai_module.shorten_for_dubbing([]) == {}


# ── Which strategy goes first ────────────────────────────────────────

NATURAL = FitPolicy(prefer=dubbing.PREFER_NATURAL)


def test_preferring_natural_spends_the_silence_instead_of_the_delivery():
    """The same line the speed preference speeds up by 10%, this one lets run."""

    assert fit_segment(2.2, 2.0, 4.0, POLICY).fit == FIT_SPED_UP
    decision = fit_segment(2.2, 2.0, 4.0, NATURAL)
    assert decision.fit == FIT_SPILL
    assert decision.tempo == 1.0


def test_preferring_natural_still_speeds_up_when_the_silence_runs_out():
    decision = fit_segment(3.5, 2.0, 3.0, NATURAL)
    assert decision.fit == FIT_SPED_UP
    assert decision.tempo == pytest.approx(3.5 / 3.0)


def test_preferring_natural_with_no_gap_behaves_like_preferring_speed():
    """Back to back cues leave nothing to spend, so both preferences agree."""

    assert fit_segment(2.2, 2.0, 2.0, NATURAL) == fit_segment(2.2, 2.0, 2.0, POLICY)


def test_a_fraction_of_a_percent_over_is_never_worth_a_re_encode():
    decision = fit_segment(3.01, 2.0, 3.0, NATURAL)
    assert decision.fit == FIT_SPILL
    assert decision.tempo == 1.0


def test_an_unset_preference_follows_the_configuration(monkeypatch):
    monkeypatch.setattr(dubbing, "settings", replace(dubbing.settings, dub_prefer="natural"))
    assert dubbing.resolve_preference("") == dubbing.PREFER_NATURAL
    assert dubbing.policy_from_settings().prefer == dubbing.PREFER_NATURAL


def test_an_explicit_preference_wins_over_the_configuration(monkeypatch):
    monkeypatch.setattr(dubbing, "settings", replace(dubbing.settings, dub_prefer="natural"))
    assert dubbing.resolve_preference("speed") == dubbing.PREFER_SPEED


def test_a_preference_that_does_not_exist_is_refused():
    with pytest.raises(dubbing.DubbingError):
        dubbing.resolve_preference("loud")


def test_a_typo_in_the_configuration_costs_a_log_line_not_every_dub(monkeypatch):
    monkeypatch.setattr(dubbing, "settings", replace(dubbing.settings, dub_prefer="fastest"))
    assert dubbing.policy_from_settings().prefer == dubbing.PREFER_SPEED


# ── Knowing when a dub has been left behind ──────────────────────────


def test_rewording_a_line_changes_the_fingerprint():
    edited = [dict(SAMPLE_CUES[0], translation="Chào nhé"), *SAMPLE_CUES[1:]]
    assert dubbing.cues_fingerprint(edited) != dubbing.cues_fingerprint(SAMPLE_CUES)


def test_moving_a_cue_changes_the_fingerprint():
    """Same words, new timing — the recording is in the wrong place now."""

    moved = [dict(SAMPLE_CUES[0], start=1.0), *SAMPLE_CUES[1:]]
    assert dubbing.cues_fingerprint(moved) != dubbing.cues_fingerprint(SAMPLE_CUES)


def test_editing_something_the_voice_never_reads_leaves_it_alone():
    restyled = [dict(cue, speaker=3, id=99) for cue in SAMPLE_CUES]
    assert dubbing.cues_fingerprint(restyled) == dubbing.cues_fingerprint(SAMPLE_CUES)


def test_an_added_blank_cue_cannot_age_a_dub():
    with_blank = [*SAMPLE_CUES, {"start": 20.0, "end": 22.0, "text": "", "translation": ""}]
    assert dubbing.cues_fingerprint(with_blank) == dubbing.cues_fingerprint(SAMPLE_CUES)


def test_a_project_with_no_dub_is_not_stale():
    assert dubbing.dub_is_stale({"cues": SAMPLE_CUES}) is False


def test_a_dub_made_from_these_cues_is_current():
    job = {
        "cues": SAMPLE_CUES,
        "dub_audio_path": "somewhere/preview.m4a",
        "dubbing_fingerprint": dubbing.cues_fingerprint(SAMPLE_CUES),
    }
    assert dubbing.dub_is_stale(job) is False


def test_a_dub_made_before_an_edit_is_stale():
    job = {
        "cues": [dict(SAMPLE_CUES[0], translation="Chào nhé"), *SAMPLE_CUES[1:]],
        "dub_audio_path": "somewhere/preview.m4a",
        "dubbing_fingerprint": dubbing.cues_fingerprint(SAMPLE_CUES),
    }
    assert dubbing.dub_is_stale(job) is True


def test_a_dub_that_never_recorded_what_it_was_made_from_reports_stale():
    """Of the two ways to be wrong, this one costs a re-run, not a bad export."""

    job = {"cues": SAMPLE_CUES, "dub_audio_path": "somewhere/preview.m4a"}
    assert dubbing.dub_is_stale(job) is True


# ── The segment cache ────────────────────────────────────────────────


def segments_in(job_dir):
    return sorted((job_dir / "dub").glob("seg_*.raw"))


def test_a_rerun_drops_the_segments_it_no_longer_uses(tmp_path):
    """Editing one line must not leave the old recording of it on disk forever."""

    dub_cues(SAMPLE_CUES, job_dir=tmp_path, voice="mock", provider="mock", shorten=False)
    assert len(segments_in(tmp_path)) == 2

    edited = [dict(SAMPLE_CUES[0], translation="Chào nhé"), *SAMPLE_CUES[1:]]
    _track, report = dub_cues(
        edited, job_dir=tmp_path, voice="mock", provider="mock", shorten=False
    )

    assert report["pruned_segments"] == 1
    assert report["cached_cues"] == 1
    assert len(segments_in(tmp_path)) == 2


def test_the_render_version_is_part_of_what_identifies_a_segment():
    """Trimming changed what a rendered line is; a stale cache must not answer."""

    before = dubbing.cache_key("mock", "mock", "Xin chào")
    original = dubbing.RENDER_VERSION
    dubbing.RENDER_VERSION = original + 1
    try:
        assert dubbing.cache_key("mock", "mock", "Xin chào") != before
    finally:
        dubbing.RENDER_VERSION = original


# ── Policy ───────────────────────────────────────────────────────────


def test_the_policy_follows_the_configured_limits(monkeypatch):
    monkeypatch.setattr(
        dubbing,
        "settings",
        replace(dubbing.settings, dub_max_speedup=1.1, dub_max_spill_seconds=0.5),
    )
    policy = dubbing.policy_from_settings()
    assert policy.max_speedup == 1.1
    assert policy.max_spill == 0.5


def test_ffmpeg_is_available_for_the_decode_step():
    """Every duration in this module comes from a decode. Say so if it cannot."""

    assert find_ffmpeg()
