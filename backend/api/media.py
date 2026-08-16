"""Serving and rendering the media a job holds: video, thumbnail, waveform, mux."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ..jobs import store
from ..media import FFmpegError, NoAudioTrack, extract_waveform, mux_soft_subtitles, render_thumbnail
from ..subtitles import format_subtitle

router = APIRouter(prefix="/api/jobs", tags=["media"])

STREAM_CHUNK_BYTES = 1024 * 512


def _ffmpeg_http_error(exc: FFmpegError) -> HTTPException:
    """ffmpeg failures map onto three different client-visible situations."""

    if exc.missing:
        return HTTPException(status_code=503, detail=str(exc))
    if exc.timed_out:
        return HTTPException(status_code=504, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _job_video_path(job_id: str) -> Path:
    job = store.read(job_id)
    raw_path = job.get("video_path")
    if not raw_path:
        raise HTTPException(status_code=404, detail="Job này không có video")
    path = Path(raw_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File video không còn trong workspace")
    return path


@router.get("/{job_id}/thumbnail")
def job_thumbnail(job_id: str) -> FileResponse:
    cache_path = store.job_dir(job_id) / "thumb.jpg"
    if cache_path.exists():
        return FileResponse(cache_path, media_type="image/jpeg")

    video_path = _job_video_path(job_id)
    try:
        render_thumbnail(video_path, cache_path)
    except FFmpegError as exc:
        raise _ffmpeg_http_error(exc) from exc
    return FileResponse(cache_path, media_type="image/jpeg")


@router.get("/{job_id}/waveform")
def job_waveform(job_id: str) -> dict:
    path = _job_video_path(job_id)
    cache_path = store.job_dir(job_id) / "waveform.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache_path.unlink(missing_ok=True)

    try:
        waveform = extract_waveform(path)
    except NoAudioTrack as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FFmpegError as exc:
        raise _ffmpeg_http_error(exc) from exc

    cache_path.write_text(json.dumps(waveform, separators=(",", ":")), encoding="utf-8")
    return waveform


def _parse_range(header: str, file_size: int) -> tuple[int, int] | None:
    if not header.startswith("bytes="):
        return None
    first, _, last = header[len("bytes="):].partition("-")
    try:
        if first:
            start = int(first)
            end = int(last) if last else file_size - 1
        elif last:
            start = max(0, file_size - int(last))
            end = file_size - 1
        else:
            return None
    except ValueError:
        return None
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return None
    return start, end


def _iter_file_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/{job_id}/video")
def stream_job_video(job_id: str, request: Request) -> Response:
    path = _job_video_path(job_id)
    file_size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    common_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}

    range_header = request.headers.get("range")
    if not range_header:
        return StreamingResponse(
            _iter_file_range(path, 0, file_size - 1),
            media_type=media_type,
            headers={**common_headers, "Content-Length": str(file_size)},
        )

    span = _parse_range(range_header, file_size)
    if span is None:
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{file_size}"},
        )

    start, end = span
    return StreamingResponse(
        _iter_file_range(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(end - start + 1),
        },
    )


@router.post("/{job_id}/mux")
def mux_subtitle(job_id: str) -> FileResponse:
    """Burn the translated track in as a soft subtitle and return the file.

    NOTE: this holds the request open for the whole render. It is the one
    long-running operation that is not a background job, because the browser
    downloads the response body directly.
    """

    job = store.read(job_id)
    if not job.get("video_path"):
        raise HTTPException(status_code=400, detail="Job import subtitle không có video")

    job_dir = store.job_dir(job_id)
    subtitle_path = job_dir / "current.srt"
    subtitle_path.write_text(
        format_subtitle(job.get("cues", []), "srt", "translated"), encoding="utf-8-sig"
    )
    output_path = job_dir / "output_subtitled.mp4"
    try:
        mux_soft_subtitles(Path(job["video_path"]), subtitle_path, output_path)
    except FFmpegError as exc:
        raise _ffmpeg_http_error(exc) from exc

    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=f"{Path(job.get('video_name') or 'video').stem}.subtitled.mp4",
    )
