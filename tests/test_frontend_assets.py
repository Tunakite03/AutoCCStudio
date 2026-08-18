"""Guards for how the frontend is delivered, not for what it does.

These break on the mistakes that are invisible in the browser during
development and only cost something in production: a module nobody preloads, a
stylesheet shipped unminified, a response that must not be compressed.
"""

import dataclasses
import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend import app as app_module
from backend.app import app


FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


def module_paths() -> set[str]:
    """Every ES module the app serves, as the URL the browser would request."""

    return {
        "/" + path.relative_to(FRONTEND_DIR).as_posix()
        for path in FRONTEND_DIR.rglob("*.js")
    }


def test_every_module_except_the_entry_point_is_preloaded():
    """app.js pulls in features, which pull in core. Left to discover that on its
    own the browser spends three round trips on a graph it could fetch at once."""

    markup = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    expected = module_paths() - {"/app.js"}
    missing = sorted(path for path in expected if f'modulepreload" href="{path}"' not in markup)
    assert not missing, f"add <link rel=modulepreload> for: {missing}"


def test_entry_point_is_a_module_script_and_not_preloaded_twice():
    markup = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    assert '<script type="module" src="/app.js">' in markup
    assert 'modulepreload" href="/app.js"' not in markup


def test_no_preload_points_at_a_file_that_no_longer_exists():
    markup = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    served = module_paths()
    referenced = {
        line.split('href="', 1)[1].split('"', 1)[0]
        for line in markup.splitlines()
        if 'rel="modulepreload"' in line
    }
    assert referenced <= served, f"preload points at missing files: {sorted(referenced - served)}"


def test_stylesheet_is_shipped_minified():
    """build-css.ps1 minifies; --watch does not. Committing the watch output is
    the easy mistake, and it costs every visitor the difference."""

    css = FRONTEND_DIR / "styles.css"
    text = css.read_text(encoding="utf-8")
    assert text.count("\n") < 20, "styles.css looks unminified — run build-css.ps1"


def test_text_responses_are_compressed():
    with TestClient(app) as client:
        response = client.get("/api/capabilities", headers={"accept-encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers.get("content-encoding") == "gzip"


def test_the_event_stream_is_never_compressed():
    """gzip would hold each event in the compressor's buffer instead of handing
    it to the browser, which is the whole point of the stream."""

    from backend.app import _UNCOMPRESSED

    assert "/events" in _UNCOMPRESSED
    assert "/video" in _UNCOMPRESSED


def test_frontend_files_revalidate_by_default():
    """Asset names carry no content hash, so the default has to be `no-cache`;
    anything else makes an edit invisible until the browser feels like asking."""

    with TestClient(app) as client:
        response = client.get("/app.js")
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-cache"


def test_static_cache_seconds_caches_assets_but_never_the_document(monkeypatch):
    """The opt-in for deployments. index.html has to keep revalidating: it is the
    only file that could point a returning browser at anything new."""

    monkeypatch.setattr(
        app_module,
        "settings",
        dataclasses.replace(app_module.settings, static_cache_seconds=3600),
    )
    with TestClient(app) as client:
        assert client.get("/app.js").headers["cache-control"] == "public, max-age=3600"
        assert client.get("/styles.css").headers["cache-control"] == "public, max-age=3600"
        assert client.get("/").headers["cache-control"] == "no-cache"


# ── The timeline canvases ────────────────────────────────────────────

CANVAS_LANE = re.compile(r'<div class="(lane[^"]*)"[^>]*>\s*(?:<!--.*?-->\s*)*<canvas', re.S)


def test_a_lane_holding_a_canvas_never_clips():
    """Each canvas is only as wide as the scrollport and is repainted with the
    scroll offset baked in, so `position: sticky` is what keeps it under the
    viewport. Give its lane `overflow-hidden` and that lane becomes the sticky
    element's scroll container — a box that never scrolls, so nothing sticks and
    the canvas slides away, leaving the right of the lane blank."""

    markup = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    lanes = CANVAS_LANE.findall(markup)
    assert lanes, "no canvas lanes found — the markup moved, so this guard is blind"
    clipping = [classes for classes in lanes if "overflow-hidden" in classes]
    assert not clipping, f"a lane with a sticky canvas must not clip: {clipping}"


CSS_VAR_CALL = re.compile(r'cssVar\(\s*"([^"]+)"')


def test_every_token_the_canvases_read_survives_the_css_build():
    """A canvas is painted from JS, so its colours arrive through `cssVar()` —
    and an unknown custom property returns "", which `fillStyle` *ignores*
    rather than rejecting. The context then keeps its default black and the
    waveform paints black on a black lane with nothing raised anywhere.

    Two ways to land there, both caught here: naming the token wrong, and
    leaving a JS-only token in `@theme`, which Tailwind compiles away because no
    utility class references it."""

    css = (FRONTEND_DIR / "styles.css").read_text(encoding="utf-8")
    asked = {
        name
        for path in FRONTEND_DIR.rglob("*.js")
        for name in CSS_VAR_CALL.findall(path.read_text(encoding="utf-8"))
    }
    assert asked, "no cssVar() calls found — the scan is looking in the wrong place"
    missing = sorted(name for name in asked if f"{name}:" not in css)
    assert not missing, f"tokens read from JS but absent from the built CSS: {missing}"
