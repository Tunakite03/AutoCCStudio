"""Application assembly: middleware, error mapping, routers, static frontend."""

from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .api import jobs as jobs_api
from .api import media as media_api
from .api import styles as styles_api
from .api import system as system_api
from .core.config import FRONTEND_DIR, get_logger, settings
from .core.messages import detail
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


# Routes whose bytes must not pass through the compressor. The event stream has
# to reach the browser one event at a time instead of sitting in a compression
# buffer, and the media routes serve already-compressed bytes — often as Range
# requests, where re-encoding the body buys nothing and complicates the offsets.
_UNCOMPRESSED = ("/events", "/video", "/thumbnail", "/mux")


class SelectiveGZipMiddleware:
    """GZipMiddleware, restricted to the responses that gain from it.

    Starlette's version compresses whatever it is handed, which is the wrong
    default here: this app streams both SSE and video through the same stack.
    """

    def __init__(self, app, *, minimum_size: int) -> None:
        self.app = app
        self.compressing_app = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and not scope["path"].endswith(_UNCOMPRESSED):
            await self.compressing_app(scope, receive, send)
            return
        await self.app(scope, receive, send)


# Registered before CORS so that CORS ends up the outer layer: middleware added
# later wraps middleware added earlier.
app.add_middleware(SelectiveGZipMiddleware, minimum_size=settings.gzip_minimum_size)

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
    return JSONResponse(status_code=404, content={"detail": detail("err.job.notFound")})


@app.exception_handler(JobConflict)
async def _job_conflict(_request: Request, exc: JobConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": exc.message.as_dict()})


app.include_router(system_api.router)
app.include_router(jobs_api.router)
app.include_router(media_api.router)
app.include_router(styles_api.router)


class RevalidatingStaticFiles(StaticFiles):
    """Serve the frontend under an explicit cache policy.

    `no-cache` is the default because development needs it: without it the
    browser keeps ES modules from its heuristic cache and an edited file
    silently does not load on reload. Revalidation costs one 304 locally.

    `STATIC_CACHE_SECONDS` trades those round trips for real caching on a
    deployment. The entry document stays on `no-cache` regardless — asset names
    carry no content hash, so the HTML is the only file that could ever point a
    returning browser at something new.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        seconds = settings.static_cache_seconds
        is_document = response.headers.get("content-type", "").startswith("text/html")
        response.headers["Cache-Control"] = (
            f"public, max-age={seconds}" if seconds > 0 and not is_document else "no-cache"
        )
        return response


if FRONTEND_DIR.exists():
    # Windows registry often maps .js to text/plain, which browsers refuse for ES modules.
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    app.mount("/", RevalidatingStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
