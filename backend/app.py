"""Application assembly: middleware, error mapping, routers, static frontend."""

from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .api import jobs as jobs_api
from .api import media as media_api
from .api import system as system_api
from .config import FRONTEND_DIR, get_logger, settings
from .jobs import JobConflict, JobNotFound, runner

logger = get_logger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "AutoCC starting (transcription=%s, translation=%s, max_concurrent_jobs=%s)",
        settings.transcription_provider,
        settings.translation_provider,
        settings.max_concurrent_jobs,
    )
    yield
    runner.shutdown()
    logger.info("AutoCC stopped")


app = FastAPI(title="AutoCC", version="0.1.0", lifespan=lifespan)

# The bundled frontend is served from this same origin, so CORS only needs to
# cover a separately hosted dev frontend. A wildcard would let any website the
# user has open read their projects and videos from 127.0.0.1, and there is no
# auth layer to stop it.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


# Domain errors are raised where they are detected and become status codes here,
# so the store and the workers never import HTTPException.
@app.exception_handler(JobNotFound)
async def _job_not_found(_request: Request, _exc: JobNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Không tìm thấy job"})


@app.exception_handler(JobConflict)
async def _job_conflict(_request: Request, exc: JobConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc) or "Job đang xử lý"})


app.include_router(system_api.router)
app.include_router(jobs_api.router)
app.include_router(media_api.router)


class RevalidatingStaticFiles(StaticFiles):
    """Serve the frontend with `no-cache`.

    Without it the browser keeps ES modules from its heuristic cache and an edited
    file silently does not load on reload. Revalidation costs one 304 locally.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if FRONTEND_DIR.exists():
    # Windows registry often maps .js to text/plain, which browsers refuse for ES modules.
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    app.mount("/", RevalidatingStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
