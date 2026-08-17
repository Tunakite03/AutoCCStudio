import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from backend import cancellation, httpclient
from backend.apikeys import CredentialPool


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


def make_key_handler(seen, decide):
    """Stub that answers per credential. `decide(key, request_number) -> status`."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            key = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            seen.append(key)
            status = decide(key, len(seen))
            body = b'{"ok":true}' if status == 200 else b'{"error":"rate limit"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
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


# ── Key rotation ─────────────────────────────────────────────────────


def test_a_rate_limited_key_is_swapped_for_the_next_one_without_waiting(monkeypatch):
    """With a pool, a 429 costs a rotation instead of a backoff. Waiting out a
    per-minute quota on key 1 while keys 2..8 sit idle is the bug."""

    waits = []
    monkeypatch.setattr(httpclient, "_wait", waits.append)
    seen = []
    port, shutdown = serve(
        make_key_handler(seen, lambda key, _n: 429 if key == "key-a" else 200)
    )
    try:
        response = httpclient.post(
            f"http://127.0.0.1:{port}/v1",
            headers={},
            json_body={},
            timeout=(5, 5),
            label="Stub",
            retries=0,
            credentials=CredentialPool(["key-a", "key-b"]),
        )
        assert httpclient.json_body(response, "Stub") == {"ok": True}
    finally:
        shutdown()
    assert seen == ["key-a", "key-b"]
    assert waits == [], "a rotation must not cost a backoff"


def test_the_first_key_is_tried_again_once_its_window_reopens(monkeypatch):
    """The failure this exists to prevent: rotating to the last key, declaring
    the pool spent, and never noticing that key 1 recovered while we worked
    through the others."""

    monkeypatch.setattr(httpclient, "RATE_LIMIT_BASE_SECONDS", 0.05)
    monkeypatch.setattr(httpclient, "RATE_LIMIT_MIN_SECONDS", 0.05)
    waits = []

    def record_and_sleep(seconds):
        waits.append(seconds)
        # Overshoot: time.monotonic() advances in ~16 ms steps on Windows, and
        # waking a tick early would send the pool round a second, flaky wait.
        time.sleep(seconds + 0.05)

    monkeypatch.setattr(httpclient, "_wait", record_and_sleep)
    seen = []
    # Both keys are limited on the first pass; by the third request key-a is
    # through its window again.
    port, shutdown = serve(
        make_key_handler(seen, lambda key, n: 429 if n <= 2 else 200)
    )
    try:
        response = httpclient.post(
            f"http://127.0.0.1:{port}/v1",
            headers={},
            json_body={},
            timeout=(5, 5),
            label="Stub",
            retries=0,
            credentials=CredentialPool(["key-a", "key-b"]),
        )
        assert httpclient.json_body(response, "Stub") == {"ok": True}
    finally:
        shutdown()
    assert seen == ["key-a", "key-b", "key-a"]
    # Exactly one wait, and only once no key at all was free.
    assert len(waits) == 1 and waits[0] > 0


def test_when_every_key_is_limited_the_call_stops_and_names_the_reason(monkeypatch):
    monkeypatch.setattr(
        httpclient,
        "settings",
        replace(httpclient.settings, http_rate_limit_retries=0),
    )
    seen = []
    port, shutdown = serve(make_key_handler(seen, lambda *_args: 429))
    try:
        with pytest.raises(httpclient.HTTPClientError) as error:
            httpclient.post(
                f"http://127.0.0.1:{port}/v1",
                headers={},
                json_body={},
                timeout=(5, 5),
                label="Stub",
                retries=0,
                credentials=CredentialPool(["key-a", "key-b", "key-c"]),
            )
        assert error.value.status_code == 429
        assert "Cả 3 API key" in str(error.value)
    finally:
        shutdown()
    # Every key was given its turn before giving up, and none was tried twice.
    assert sorted(seen) == ["key-a", "key-b", "key-c"]


def test_every_key_keeps_its_own_backoff(monkeypatch):
    """A key that has failed three times must not drag a healthy key's cooldown
    up with it — they are separate quotas."""

    monkeypatch.setattr(httpclient, "RATE_LIMIT_BASE_SECONDS", 1.0)
    monkeypatch.setattr(httpclient, "RATE_LIMIT_MIN_SECONDS", 0.0)
    monkeypatch.setattr(httpclient, "_wait", lambda _seconds: None)
    pool = CredentialPool(["tired", "fresh"])
    pool.penalise("tired", 0)
    pool.penalise("tired", 0)

    seen = []
    port, shutdown = serve(make_key_handler(seen, lambda key, _n: 429))
    try:
        with pytest.raises(httpclient.HTTPClientError):
            httpclient.post(
                f"http://127.0.0.1:{port}/v1",
                headers={},
                json_body={},
                timeout=(5, 5),
                label="Stub",
                retries=0,
                credentials=pool,
            )
    finally:
        shutdown()
    # 2 strikes carried in, one more here: 2^3 vs the fresh key's first 2^1.
    assert pool.strikes("tired") == 3
    assert pool.strikes("fresh") == 1


def test_one_rejected_key_does_not_take_the_job_down_with_it():
    """A typo in the third of eight keys used to kill roughly every eighth call
    while the other seven worked perfectly."""

    seen = []
    port, shutdown = serve(
        make_key_handler(seen, lambda key, _n: 401 if key == "typo" else 200)
    )
    pool = CredentialPool(["typo", "good"])
    try:
        for _ in range(2):
            response = httpclient.post(
                f"http://127.0.0.1:{port}/v1",
                headers={},
                json_body={},
                timeout=(5, 5),
                label="Stub",
                retries=0,
                credentials=pool,
            )
            assert httpclient.json_body(response, "Stub") == {"ok": True}
    finally:
        shutdown()
    # Rejected once, then left out of rotation — not retried on the second call.
    assert seen == ["typo", "good", "good"]
    assert pool.strikes("typo") == 0, "a refused key is not a rate-limited one"


def test_a_pool_of_nothing_but_bad_keys_says_which_setting_is_wrong():
    seen = []
    port, shutdown = serve(make_key_handler(seen, lambda *_args: 401))
    try:
        with pytest.raises(httpclient.HTTPClientError) as error:
            httpclient.post(
                f"http://127.0.0.1:{port}/v1",
                headers={},
                json_body={},
                timeout=(5, 5),
                label="Stub",
                retries=0,
                credentials=CredentialPool(["bad-a", "bad-b"]),
            )
        assert error.value.status_code == 401
        assert "LLM_API_KEY" in str(error.value)
    finally:
        shutdown()
    assert seen == ["bad-a", "bad-b"]


def test_a_stopped_job_does_not_sit_out_the_rest_of_a_backoff(monkeypatch):
    """A minute-long rate-limit wait must not make the Hủy button look dead."""

    monkeypatch.setattr(httpclient, "STOP_CHECK_INTERVAL_SECONDS", 0.01)
    checks = {"n": 0}

    class Stopped(RuntimeError):
        pass

    def check():
        checks["n"] += 1
        if checks["n"] > 2:
            raise Stopped()

    cancellation.set_stop_check(check)
    try:
        with pytest.raises(Stopped):
            httpclient._wait(30.0)
    finally:
        cancellation.clear_stop_check()
    # It stopped inside the wait, not after serving all thirty seconds.
    assert checks["n"] == 3


def test_a_wait_without_a_job_behind_it_just_waits():
    cancellation.clear_stop_check()
    started = time.monotonic()
    httpclient._wait(0.05)
    assert time.monotonic() - started >= 0.05


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
