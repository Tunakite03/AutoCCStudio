"""Pure-Python PCM byte manipulation for the dub pipeline.

No ffmpeg here — see `infrastructure/media/ffmpeg.py` for `decode_to_pcm` and
`retime_pcm`, which shell out despite operating on the same raw bytes.
"""

from __future__ import annotations

import wave
from array import array
from pathlib import Path

# Edge's neural voices come back at 24 kHz mono. Resampling higher would invent
# detail the source never had, so the whole dub pipeline stays at that rate.
DUB_SAMPLE_RATE = 24000
DUB_SAMPLE_WIDTH = 2

# Of 32768. Low enough to keep a breathy consonant, high enough to read encoder
# noise as the silence it is.
DUB_SILENCE_THRESHOLD = 600
DUB_SILENCE_BLOCK_MS = 10
# Padding left around the speech, so a plosive is not clipped off its own word.
DUB_SILENCE_KEEP_MS = 40


def pcm_seconds(pcm: bytes, sample_rate: int = DUB_SAMPLE_RATE) -> float:
    return len(pcm) / (DUB_SAMPLE_WIDTH * sample_rate)


def trim_silence(
    pcm: bytes,
    *,
    sample_rate: int = DUB_SAMPLE_RATE,
    threshold: int = DUB_SILENCE_THRESHOLD,
    keep_ms: int = DUB_SILENCE_KEEP_MS,
) -> bytes:
    """Cut the silence a synthesiser pads its output with.

    This is not a nicety. Edge returns roughly 0.2s of lead-in and up to 1.1s of
    tail on *every* utterance, so "Gì cơ?" — half a second of speech — arrives as
    1.8 seconds. Measured untrimmed, a two-word line looks too long for a
    two-second cue, and the fitting stage then speeds up, spills and finally
    spends an LLM call solving a problem that was never in the text.

    Scanned in 10ms blocks with `min`/`max` over an `array` slice rather than
    sample by sample: same answer, and it stays in C for the thousand segments a
    feature-length project brings.
    """

    block = max(1, sample_rate * DUB_SILENCE_BLOCK_MS // 1000)
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % DUB_SAMPLE_WIDTH)])
    count = len(samples)

    first: int | None = None
    last = 0
    for start in range(0, count, block):
        chunk = samples[start : start + block]
        if not chunk:
            break
        if max(abs(min(chunk)), abs(max(chunk))) > threshold:
            if first is None:
                first = start
            last = start + len(chunk)
    if first is None:
        # Nothing above the floor: the provider answered with silence, which the
        # caller treats as a line it failed to voice.
        return b""

    keep = sample_rate * keep_ms // 1000
    return samples[max(0, first - keep) : min(count, last + keep)].tobytes()


def write_wav(destination: Path, pcm: bytes, sample_rate: int = DUB_SAMPLE_RATE) -> Path:
    """Wrap raw PCM in a WAV header. No ffmpeg: the header is 44 known bytes."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(DUB_SAMPLE_WIDTH)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return destination
