from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from backend import httpclient


def serve(handler_factory):
    """Run a one-off HTTP stub and return (port, shutdown)."""

    server = HTTPServer(("127.0.0.1", 0), handler_factory)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return server.server_port, shutdown


def make_handler(statuses, seen, extra_headers=None):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            seen.append(self.rfile.read(length))
            status = statuses[min(len(seen) - 1, len(statuses) - 1)]
            body = b'{"ok":true}' if status == 200 else b'{"error":"nope"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    return Handler


@pytest.fixture(autouse=True)
def no_backoff_delay(monkeypatch):
    monkeypatch.setattr(httpclient, "BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(httpclient, "RATE_LIMIT_BASE_SECONDS", 0.0)
    monkeypatch.setattr(httpclient, "RATE_LIMIT_MIN_SECONDS", 0.0)


def test_retries_a_transient_failure_then_succeeds():
    seen = []
    port, shutdown = serve(make_handler([503, 200], seen))
    try:
        response = httpclient.post(
            f"http://127.0.0.1:{port}/v1",
            headers={"Content-Type": "application/json"},
            json_body={"hello": "world"},
            timeout=(5, 5),
            label="Stub",
            retries=2,
        )
        assert httpclient.json_body(response, "Stub") == {"ok": True}
    finally:
        shutdown()
    assert len(seen) == 2


def test_gives_up_after_the_retry_budget():
    seen = []
    port, shutdown = serve(make_handler([503], seen))
    try:
        with pytest.raises(httpclient.HTTPClientError) as error:
            httpclient.post(
                f"http://127.0.0.1:{port}/v1",
                headers={},
                json_body={},
                timeout=(5, 5),
                label="Stub",
                retries=2,
            )
        assert error.value.status_code == 503
    finally:
        shutdown()
    assert len(seen) == 3  # the first attempt plus two retries


def test_a_rate_limit_is_waited_out_past_the_ordinary_retry_budget():
    """429 means "later", not "broken": the connection-error budget is spent in
    seconds, which is never long enough for a per-minute quota."""

    seen = []
    port, shutdown = serve(make_handler([429, 429, 429, 200], seen))
    try:
        response = httpclient.post(
            f"http://127.0.0.1:{port}/v1",
            headers={},
            json_body={},
            timeout=(5, 5),
            label="Stub",
            retries=0,
        )
        assert httpclient.json_body(response, "Stub") == {"ok": True}
    finally:
        shutdown()
    assert len(seen) == 4


def test_a_rate_limit_that_never_clears_says_so(monkeypatch):
    monkeypatch.setattr(
        httpclient,
        "settings",
        replace(httpclient.settings, http_rate_limit_retries=2),
    )
    seen = []
    port, shutdown = serve(make_handler([429], seen))
    try:
        with pytest.raises(httpclient.HTTPClientError) as error:
            httpclient.post(
                f"http://127.0.0.1:{port}/v1",
                headers={},
                json_body={},
                timeout=(5, 5),
                label="Stub",
                retries=2,
            )
        assert error.value.status_code == 429
        assert "giới hạn tốc độ" in str(error.value)
    finally:
        shutdown()
    assert len(seen) == 3  # the rate-limit budget, not the connection one


def test_the_provider_decides_how_long_to_wait(monkeypatch):
    ask = lambda headers: httpclient._rate_limit_delay(
        SimpleNamespace(headers=headers), 0
    )
    assert ask({"Retry-After": "7"}) == 7.0
    assert ask({"ratelimitbysize-reset": "12"}) == 12.0
    # A provider asking for an hour still only blocks the worker for a minute.
    assert ask({"Retry-After": "3600"}) == httpclient.RATE_LIMIT_MAX_SECONDS

    # Nothing said: exponential, doubling with every wait already served.
    monkeypatch.setattr(httpclient, "RATE_LIMIT_BASE_SECONDS", 2.0)
    assert httpclient._rate_limit_delay(SimpleNamespace(headers={}), 3) == 16.0

    # "Retry immediately" is the one instruction a rate limit does not get.
    monkeypatch.setattr(httpclient, "RATE_LIMIT_MIN_SECONDS", 1.0)
    assert ask({"Retry-After": "0"}) == 1.0


def test_a_client_error_is_not_retried():
    """A 400 means the request is wrong; sending it again just wastes a quota."""

    seen = []
    port, shutdown = serve(make_handler([400], seen))
    try:
        with pytest.raises(httpclient.HTTPClientError) as error:
            httpclient.post(
                f"http://127.0.0.1:{port}/v1",
                headers={},
                json_body={},
                timeout=(5, 5),
                label="Stub",
                retries=3,
            )
        assert error.value.status_code == 400
    finally:
        shutdown()
    assert len(seen) == 1


def test_each_retry_gets_a_fresh_request_body(tmp_path):
    """A streamed upload cannot be replayed from a consumed file handle."""

    payload = tmp_path / "upload.bin"
    payload.write_bytes(b"media-bytes")
    seen = []
    port, shutdown = serve(make_handler([503, 200], seen))
    handles = []

    def open_media():
        handle = payload.open("rb")
        handles.append(handle)
        return handle

    try:
        httpclient.post(
            f"http://127.0.0.1:{port}/v1",
            headers={"Content-Type": "application/octet-stream"},
            timeout=(5, 5),
            label="Stub",
            body_factory=open_media,
            retries=1,
        )
    finally:
        for handle in handles:
            handle.close()
        shutdown()

    assert seen == [b"media-bytes", b"media-bytes"]
