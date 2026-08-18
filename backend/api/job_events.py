"""Server-sent events for live job snapshots."""

from __future__ import annotations

import asyncio
import json

from fastapi import Request
from fastapi.responses import StreamingResponse

from ..jobs import store
from ..jobs.model import STATUS_PROCESSING


def format_sse_job_event(revision: int, payload: str) -> str:
    return f"id: {revision}\nevent: job\ndata: {payload}\n\n"


async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    snapshot = await asyncio.to_thread(store.get, job_id)
    try:
        last_revision = int(request.headers.get("last-event-id", "-1"))
    except ValueError:
        last_revision = -1

    async def event_stream():
        nonlocal last_revision, snapshot
        subscriber = store.subscribe(job_id)
        _, queue = subscriber
        try:
            yield "retry: 2000\n\n"
            revision = int(snapshot.get("revision", 0))
            if revision > last_revision or snapshot["status"] != STATUS_PROCESSING:
                payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                yield format_sse_job_event(revision, payload)
                last_revision = revision
            if snapshot["status"] != STATUS_PROCESSING:
                return

            while True:
                if await request.is_disconnected():
                    return
                try:
                    revision, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if revision <= last_revision:
                    continue
                last_revision = revision
                yield format_sse_job_event(revision, payload)
                if json.loads(payload)["status"] != STATUS_PROCESSING:
                    return
        finally:
            store.unsubscribe(job_id, subscriber)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
