"""Every ffmpeg invocation in the app, plus media probing.

Routes used to build ffmpeg argument lists inline, each with its own timeout
handling and its own way of ignoring a failure. They call this module instead,
so a failed render reports why rather than surfacing as an empty result.
"""

from __future__ import annotations

import shutil
import subprocess
from array import array
from pathlib import Path

from ...core.config import get_logger, settings
from ...core.messages import CodedError, Message
from ...domain.dubbing.audio_dsp import DUB_SAMPLE_RATE

logger = get_logger("media")

# Which render failed, as a code the client can name. Passed in rather than
# derived so the log line and the message agree on what was being attempted.
OP_THUMBNAIL = Message("op.thumbnail")
OP_WAVEFORM = Message("op.waveform")
OP_MUX = Message("op.mux")


class FFmpegError(CodedError):
    """ffmpeg is missing, timed out, or exited non-zero."""

    def __init__(self, code, *, timed_out: bool = False, missing: bool = False, **params):
        super().__init__(code, **params)
        self.timed_out = timed_out
        self.missing = missing


class NoAudioTrack(CodedError):
    """The media decoded fine but carries no audio."""


def require_ffmpeg() -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FFmpegError("err.ffmpeg.missing", missing=True)
    return ffmpeg


def run_ffmpeg(arguments: list[str], *, timeout: int, operation: Message) -> str:
    """Run ffmpeg and return its stderr, raising FFmpegError on any failure."""

    command = [require_ffmpeg(), "-y", "-v", "error", *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logger.warning("%s: ffmpeg timed out after %ss", operation, timeout)
        raise FFmpegError(
            "err.ffmpeg.timeout", timed_out=True, operation=operation, seconds=timeout
        ) from exc
    except OSError as exc:
        logger.warning("%s: ffmpeg could not be launched: %s", operation, exc)
        raise FFmpegError(
            "err.ffmpeg.launchFailed", operation=operation, cause=str(exc)
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()[-1500:]
        logger.warning("%s: ffmpeg exited %s: %s", operation, result.returncode, detail)
        raise FFmpegError("err.ffmpeg.failed", operation=operation, detail=detail)
    return result.stderr or ""


def find_ffmpeg() -> str | None:
    """Return a usable ffmpeg executable, including the bundled fallback."""

    configured = settings.ffmpeg_binary.strip()
    if configured:
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        configured_path = Path(configured)
        if configured_path.exists() and configured_path.is_file():
            return str(configured_path)

    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if bundled.exists():
            return str(bundled)
    except (ImportError, OSError, RuntimeError):
        pass
    return None


def media_duration_seconds(media_path: Path) -> float | None:
    """Return the container duration when PyAV can determine it."""

    try:
        import av
    except ImportError:
        logger.warning(
            "PyAV is not installed, so cue timings cannot be clamped to the media "
            "duration. Run: pip install -r requirements.txt"
        )
        return None

    try:
        with av.open(str(media_path)) as container:
            if container.duration is not None:
                return max(float(container.duration / av.time_base), 0.0)

            stream_durations = [
                float(stream.duration * stream.time_base)
                for stream in container.streams
                if stream.duration is not None and stream.time_base is not None
            ]
            return max(stream_durations, default=None)
    except Exception as exc:
        # Duration probing is a safety guard. Let Whisper report the real media
        # error if the container cannot be inspected here.
        logger.info("could not probe duration of %s: %s", media_path.name, exc)
        return None


def extract_transcription_audio(media_path: Path, output_dir: Path | None = None) -> Path | None:
    """Extract a lightweight audio file (.m4a) from video for fast STT upload.

    Returns the path to the extracted temporary audio file if successful, or None
    if extraction failed or ffmpeg is unavailable.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not media_path.exists() or not media_path.is_file():
        return None

    try:
        import tempfile
        import uuid

        target_dir = output_dir or Path(tempfile.gettempdir())
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_audio = target_dir / f"autocc_stt_{media_path.stem}_{uuid.uuid4().hex[:8]}.m4a"

        command = [
            ffmpeg,
            "-y",
            "-v", "error",
            "-i", str(media_path),
            "-vn",
            "-ac", "1",
            "-c:a", "aac",
            "-b:a", "64k",
            str(temp_audio),
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if result.returncode == 0 and temp_audio.exists() and temp_audio.stat().st_size > 0:
            return temp_audio
        logger.warning(
            "audio extraction from %s exited %s: %s",
            media_path.name,
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace").strip()[-500:],
        )
        temp_audio.unlink(missing_ok=True)
        return None
    except Exception as exc:
        # The caller falls back to uploading the original file, so this is a
        # slow path rather than a failure.
        logger.warning("audio extraction from %s failed: %s", media_path.name, exc)
        return None


WAVEFORM_RESOLUTION = 20
WAVEFORM_SAMPLE_RATE = 4000
THUMBNAIL_TIMEOUT_SECONDS = 60
WAVEFORM_TIMEOUT_SECONDS = 300
MUX_TIMEOUT_SECONDS = 1800


def render_thumbnail(video_path: Path, destination: Path) -> Path:
    """Grab one frame from a tenth of the way in."""

    duration = media_duration_seconds(video_path) or 0.0
    seek_to = max(0.0, min(duration * 0.1, max(duration - 0.5, 0.0)))
    run_ffmpeg(
        [
            "-ss", f"{seek_to:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "6",
            str(destination),
        ],
        timeout=THUMBNAIL_TIMEOUT_SECONDS,
        operation=OP_THUMBNAIL,
    )
    if not destination.exists():
        raise FFmpegError("err.ffmpeg.noThumbnail")
    return destination


def extract_waveform(video_path: Path) -> dict:
    """Decode audio to mono PCM, keeping one peak per 1/WAVEFORM_RESOLUTION second.

    Distinguishes a silent-by-design file from a broken decode: ffmpeg's exit
    code decides, not the emptiness of the result. Reporting "no audio track"
    for what was really a decode failure sent people looking in the wrong place.
    """

    ffmpeg = require_ffmpeg()
    block_bytes = (WAVEFORM_SAMPLE_RATE // WAVEFORM_RESOLUTION) * 2
    command = [
        ffmpeg, "-v", "error", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(WAVEFORM_SAMPLE_RATE), "-f", "s16le", "-",
    ]
    peaks: list[int] = []
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        buffer = b""
        while True:
            chunk = process.stdout.read(1 << 16)
            if not chunk:
                break
            buffer += chunk
            usable = len(buffer) - (len(buffer) % block_bytes)
            for offset in range(0, usable, block_bytes):
                samples = array("h")
                samples.frombytes(buffer[offset:offset + block_bytes])
                loudest = max(abs(min(samples)), abs(max(samples)))
                peaks.append(min(255, loudest * 255 // 32768))
            buffer = buffer[usable:]
        try:
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            process.wait(timeout=WAVEFORM_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise FFmpegError(
                "err.ffmpeg.timeout",
                timed_out=True,
                operation=OP_WAVEFORM,
                seconds=WAVEFORM_TIMEOUT_SECONDS,
            ) from exc

    if process.returncode != 0:
        detail = stderr.strip()[-500:]
        logger.warning("waveform decode exited %s: %s", process.returncode, detail)
        raise FFmpegError("err.ffmpeg.failed", operation=OP_WAVEFORM, detail=detail)
    if not peaks:
        raise NoAudioTrack("err.media.noAudioTrack")
    return {"resolution": WAVEFORM_RESOLUTION, "peaks": peaks}


def mux_soft_subtitles(video_path: Path, subtitle_path: Path, destination: Path) -> Path:
    """Copy the streams and attach the subtitle as a selectable mov_text track."""

    run_ffmpeg(
        [
            "-i", str(video_path),
            "-i", str(subtitle_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map", "1:0",
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "mov_text",
            str(destination),
        ],
        timeout=MUX_TIMEOUT_SECONDS,
        operation=OP_MUX,
    )
    if not destination.exists():
        raise FFmpegError("err.ffmpeg.noMuxedVideo")
    return destination


# ── Dubbing ──────────────────────────────────────────────────────────

OP_DUB_DECODE = Message("op.dubDecode")
OP_DUB_MIX = Message("op.dubMix")

DUB_DECODE_TIMEOUT_SECONDS = 180
DUB_MIX_TIMEOUT_SECONDS = 1800


def run_ffmpeg_binary(arguments: list[str], *, timeout: int, operation: Message,
                      stdin_bytes: bytes | None = None) -> bytes:
    """Run ffmpeg for its stdout. Same failure handling as `run_ffmpeg`.

    Separate because `run_ffmpeg` decodes output as text, which mangles PCM.
    """

    command = [require_ffmpeg(), "-y", "-v", "error", *arguments]
    try:
        result = subprocess.run(
            command,
            input=stdin_bytes,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("%s: ffmpeg timed out after %ss", operation, timeout)
        raise FFmpegError(
            "err.ffmpeg.timeout", timed_out=True, operation=operation, seconds=timeout
        ) from exc
    except OSError as exc:
        logger.warning("%s: ffmpeg could not be launched: %s", operation, exc)
        raise FFmpegError(
            "err.ffmpeg.launchFailed", operation=operation, cause=str(exc)
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()[-1500:]
        logger.warning("%s: ffmpeg exited %s: %s", operation, result.returncode, detail)
        raise FFmpegError("err.ffmpeg.failed", operation=operation, detail=detail)
    return result.stdout or b""


def atempo_chain(tempo: float) -> list[str]:
    """`atempo` only accepts 0.5–2.0, so anything outside is a chain of stages."""

    factor = max(0.05, float(tempo))
    stages: list[float] = []
    while factor > 2.0:
        stages.append(2.0)
        factor /= 2.0
    while factor < 0.5:
        stages.append(0.5)
        factor /= 0.5
    stages.append(factor)
    return [f"atempo={stage:.6f}" for stage in stages]


def decode_to_pcm(source: Path) -> bytes:
    """Decode any audio file to raw mono s16le at DUB_SAMPLE_RATE.

    Raw PCM rather than a container because the assembler places segments by
    sample offset: a length in bytes is a length in samples, with no header to
    account for and no second decode to find out how long a clip really is.

    Always at natural speed. A line is measured before anything is decided about
    it, and `retime_pcm` applies the decision afterwards — to the cached PCM, so
    a re-run that lands on a different tempo does not re-synthesise the line.
    """

    return run_ffmpeg_binary(
        [
            "-i", str(source),
            "-vn", "-ac", "1", "-ar", str(DUB_SAMPLE_RATE), "-f", "s16le", "-",
        ],
        timeout=DUB_DECODE_TIMEOUT_SECONDS,
        operation=OP_DUB_DECODE,
    )


def retime_pcm(pcm: bytes, *, tempo: float) -> bytes:
    """Speed raw PCM up (or down) without re-synthesising or re-decoding a file."""

    if abs(tempo - 1.0) <= 1e-3 or not pcm:
        return pcm
    arguments = [
        "-f", "s16le", "-ar", str(DUB_SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        "-filter:a", ",".join(atempo_chain(tempo)),
        "-f", "s16le", "-",
    ]
    return run_ffmpeg_binary(
        arguments,
        timeout=DUB_DECODE_TIMEOUT_SECONDS,
        operation=OP_DUB_DECODE,
        stdin_bytes=pcm,
    )


def encode_audio(source: Path, destination: Path, *, bitrate: str = "192k") -> Path:
    """Re-encode audio to AAC — a track a browser will play and mp4 will hold.

    The assembled dub is a WAV, which is right for sample-exact assembly and
    wrong for everything after it: a feature-length one is hundreds of megabytes
    to stream to a preview player, and mp4 will not carry it at all.
    """

    run_ffmpeg(
        ["-i", str(source), "-vn", "-c:a", "aac", "-b:a", bitrate, str(destination)],
        timeout=DUB_MIX_TIMEOUT_SECONDS,
        operation=OP_DUB_MIX,
    )
    if not destination.exists():
        raise FFmpegError("err.ffmpeg.noDubTrack")
    return destination


def has_audio_stream(media_path: Path) -> bool:
    """Whether the container carries audio at all.

    Asked before mixing: `amix` on a video with no audio track fails with an
    ffmpeg stream-mapping error, which reads to a user as "the dub is broken"
    rather than "this video is silent".
    """

    try:
        import av
    except ImportError:
        # Without PyAV, assume there is audio and let ffmpeg be the judge.
        return True

    try:
        with av.open(str(media_path)) as container:
            return any(stream.type == "audio" for stream in container.streams)
    except Exception as exc:
        logger.info("could not probe audio streams of %s: %s", media_path.name, exc)
        return True


def mix_dub_over_original(
    video_path: Path,
    dub_path: Path,
    destination: Path,
    *,
    original_gain: float,
) -> Path:
    """Lay the dub over the original audio, ducked to `original_gain`.

    A fixed gain rather than sidechain ducking on purpose: the result is the
    same on every run, and `sidechaincompress` is not in every ffmpeg build —
    including some of the ones `imageio-ffmpeg` ships as the fallback binary.
    """

    gain = min(max(float(original_gain), 0.0), 1.0)
    if not has_audio_stream(video_path):
        # A silent video is a legitimate input — a slideshow, a screen capture.
        # There is nothing to duck, so the dub simply becomes the audio.
        return encode_audio(dub_path, destination)

    run_ffmpeg(
        [
            "-i", str(video_path),
            "-i", str(dub_path),
            "-filter_complex",
            f"[0:a]volume={gain:.3f}[bed];"
            "[bed][1:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]",
            "-map", "[out]",
            "-c:a", "aac", "-b:a", "192k",
            str(destination),
        ],
        timeout=DUB_MIX_TIMEOUT_SECONDS,
        operation=OP_DUB_MIX,
    )
    if not destination.exists():
        raise FFmpegError("err.ffmpeg.noDubTrack")
    return destination


def mux_dubbed_video(
    video_path: Path,
    dub_audio_path: Path,
    subtitle_path: Path | None,
    destination: Path,
    *,
    keep_original_audio: bool,
) -> Path:
    """Rebuild the MP4 around the dubbed audio, copying every stream.

    The dub is mapped first so a player that just takes the default track plays
    the dub; the original follows it when the caller wants both.
    """

    arguments = ["-i", str(video_path), "-i", str(dub_audio_path)]
    if subtitle_path is not None:
        arguments += ["-i", str(subtitle_path)]
    arguments += ["-map", "0:v:0", "-map", "1:a:0"]
    if keep_original_audio:
        arguments += ["-map", "0:a:0?"]
    if subtitle_path is not None:
        arguments += ["-map", "2:0"]
    arguments += ["-c:v", "copy", "-c:a", "copy"]
    if subtitle_path is not None:
        arguments += ["-c:s", "mov_text"]
    arguments += [
        "-disposition:a:0", "default",
        "-metadata:s:a:0", "title=Dub",
    ]
    if keep_original_audio:
        arguments += ["-disposition:a:1", "0", "-metadata:s:a:1", "title=Original"]
    arguments.append(str(destination))

    run_ffmpeg(arguments, timeout=MUX_TIMEOUT_SECONDS, operation=OP_MUX)
    if not destination.exists():
        raise FFmpegError("err.ffmpeg.noMuxedVideo")
    return destination
