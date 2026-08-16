from pathlib import Path

import pytest

from backend.media import (
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
