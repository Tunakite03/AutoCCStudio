from pathlib import Path

import pytest

from backend.infrastructure.media.ffmpeg import (
    FFmpegError,
    NoAudioTrack,
    extract_transcription_audio,
    extract_waveform,
    find_ffmpeg,
    media_duration_seconds,
    render_thumbnail,
)

SMOKE_VIDEO = Path("runtime/ai-smoke/speech.mp4")

# The sample is a local fixture, not something a clean checkout carries.
requires_sample = pytest.mark.skipif(
    not SMOKE_VIDEO.exists(), reason=f"sample media missing at {SMOKE_VIDEO}"
)


def test_ffmpeg_resolver_finds_path_or_bundled_binary():
    assert find_ffmpeg()


@requires_sample
def test_media_duration_reads_real_smoke_video():
    duration = media_duration_seconds(SMOKE_VIDEO)
    assert duration is not None, "PyAV missing? it is required by requirements.txt"
    assert 5.0 < duration < 6.5


@requires_sample
def test_extract_transcription_audio_from_real_video(tmp_path):
    extracted = extract_transcription_audio(SMOKE_VIDEO, output_dir=tmp_path)
    assert extracted is not None
    assert extracted.exists()
    assert extracted.stat().st_size > 0
    assert extracted.suffix == ".m4a"


def test_extract_transcription_audio_handles_missing_file(tmp_path):
    assert extract_transcription_audio(tmp_path / "nonexistent.mp4") is None


@requires_sample
def test_waveform_returns_peaks_for_real_audio():
    waveform = extract_waveform(SMOKE_VIDEO)
    assert waveform["resolution"] == 20
    assert waveform["peaks"]
    assert all(0 <= peak <= 255 for peak in waveform["peaks"])


def test_waveform_reports_a_broken_decode_rather_than_no_audio(tmp_path):
    """An ffmpeg failure used to surface as "video không có audio track", which
    sent people looking at the wrong thing entirely."""

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a media container")
    with pytest.raises(FFmpegError) as error:
        extract_waveform(broken)
    assert not isinstance(error.value, NoAudioTrack)


def test_thumbnail_render_fails_loudly_on_unreadable_media(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    with pytest.raises(FFmpegError):
        render_thumbnail(broken, tmp_path / "thumb.jpg")


# ── Trimming what a synthesiser pads its output with ─────────────────

from backend.domain.dubbing.audio_dsp import (  # noqa: E402  (grouped with the dubbing tests it serves)
    DUB_SAMPLE_RATE,
    DUB_SILENCE_KEEP_MS,
    trim_silence,
)


def tone(seconds, level=9000):
    return b"".join(
        int(level).to_bytes(2, "little", signed=True)
        for _ in range(int(seconds * DUB_SAMPLE_RATE))
    )


def silence(seconds):
    return bytes(2 * int(seconds * DUB_SAMPLE_RATE))


def seconds_of(pcm):
    return len(pcm) / (2 * DUB_SAMPLE_RATE)


def test_trimming_cuts_the_lead_in_and_the_tail():
    """Edge pads every utterance, so a two-word line measures as a long one."""

    trimmed = trim_silence(silence(0.2) + tone(0.5) + silence(1.1))
    pad = 2 * DUB_SILENCE_KEEP_MS / 1000
    assert seconds_of(trimmed) == pytest.approx(0.5 + pad, abs=0.02)


def test_trimming_keeps_a_pad_so_a_plosive_survives():
    trimmed = trim_silence(silence(0.5) + tone(0.3) + silence(0.5))
    assert seconds_of(trimmed) > 0.3


def test_trimming_leaves_speech_that_starts_and_ends_loud_alone():
    speech = tone(0.4)
    assert trim_silence(speech) == speech


def test_trimming_reports_an_all_silent_answer_as_nothing():
    """A provider that answered with silence voiced no line, and must say so."""

    assert trim_silence(silence(2.0)) == b""


def test_trimming_ignores_encoder_noise_below_the_floor():
    quiet = b"".join((120).to_bytes(2, "little", signed=True) for _ in range(DUB_SAMPLE_RATE))
    assert trim_silence(quiet) == b""


def test_trimming_does_not_cut_a_pause_inside_a_line():
    """Only the edges: a beat between two clauses is part of the delivery."""

    trimmed = trim_silence(tone(0.3) + silence(0.4) + tone(0.3))
    assert seconds_of(trimmed) == pytest.approx(1.0, abs=0.02)
