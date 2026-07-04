"""Tests for the streaming body-size limit middleware (#284 / Tier B #3).

The legacy middleware only inspected the ``Content-Length`` header. The new
implementation tallies bytes from the ASGI ``receive`` callable so it cannot
be bypassed by chunked-transfer-encoding or by a client lying about the
declared length.

These tests exercise both:
- header fast-path (rejects on declared length over the limit)
- streaming path (rejects when the actual body grows past the limit even if
  the declared length was small or absent)
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from palinode.api import server


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch):
    """Re-build the middleware with a tiny limit for faster, deterministic tests."""
    # Mutate the constant; the middleware reads it on dispatch via self.max_bytes
    # which is captured at app-build time. Easiest: patch on each instance.
    found = False
    for mw in server.app.user_middleware:
        if mw.cls is server._BodySizeLimitMiddleware:
            monkeypatch.setitem(mw.kwargs, "max_bytes", 256)
            found = True
    assert found, "BodySizeLimitMiddleware was not registered on the app"
    # Force FastAPI to rebuild its middleware stack so the patched kwargs apply.
    server.app.middleware_stack = None
    yield 256
    server.app.middleware_stack = None


# ---------------------------------------------------------------------------
# Header fast-path
# ---------------------------------------------------------------------------


def test_oversized_content_length_rejected(small_limit):
    """A request that DECLARES it will be too large is rejected with 413."""
    client = TestClient(server.app, raise_server_exceptions=False)
    big_body = b"x" * 1024  # > 256
    resp = client.post(
        "/save",
        content=big_body,
        headers={"content-type": "application/json", "content-length": "1024"},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"] == "Request body too large"


def test_undersized_request_passes_through(small_limit):
    """A small body must pass through to the route handler."""
    client = TestClient(server.app, raise_server_exceptions=False)
    # Hit a tiny endpoint that accepts JSON. We don't care about the result —
    # only that we don't get 413. /search rejects malformed bodies with 422,
    # which is fine here (it means the middleware passed us through).
    resp = client.post(
        "/search",
        content=json.dumps({"query": "x"}).encode(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code != 413


# ---------------------------------------------------------------------------
# Streaming-path enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_body_exceeds_limit_returns_413():
    """Drive the middleware directly with a faked ASGI scope and a
    chunked receive that omits Content-Length entirely.

    This is the scenario the legacy header-only check missed: chunked
    transfer encoding doesn't set Content-Length, so the legacy middleware
    let the body through unconstrained. The new streaming check tallies
    bytes during receive() and raises 413 mid-stream.
    """
    sent_messages: list[dict] = []

    async def send(msg):
        sent_messages.append(msg)

    chunks = [
        {"type": "http.request", "body": b"a" * 100, "more_body": True},
        {"type": "http.request", "body": b"b" * 100, "more_body": True},
        {"type": "http.request", "body": b"c" * 100, "more_body": False},
    ]
    chunk_iter = iter(chunks)

    async def receive():
        try:
            return next(chunk_iter)
        except StopIteration:  # pragma: no cover (we always 413 first)
            return {"type": "http.disconnect"}

    # A no-op downstream that drains the body and returns 200 — should NEVER
    # be reached because 413 short-circuits.
    downstream_called = False

    async def fake_app(scope, recv, snd):
        nonlocal downstream_called
        downstream_called = True
        # Drain until the middleware injects 413
        while True:
            msg = await recv()
            if not msg.get("more_body", False):
                break
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"ok"})

    middleware = server._BodySizeLimitMiddleware(fake_app, max_bytes=150)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/save",
        "headers": [],  # NO content-length — this is the bypass case
    }
    await middleware(scope, receive, send)

    # The middleware should have emitted a 413 response start + body
    statuses = [m for m in sent_messages if m["type"] == "http.response.start"]
    assert statuses, f"No response.start emitted; got {sent_messages}"
    assert statuses[0]["status"] == 413


@pytest.mark.asyncio
async def test_streaming_stops_reading_once_over_limit():
    """The middleware must short-circuit **mid-stream** — once the running byte
    total crosses the limit it must NOT keep reading the rest of the body (#297).

    The 2026-05-01 smoke test only proved a 413 came back; it never proved the
    413 fired *before the full body was received*. Here an instrumented receive
    records how many chunks it hands out: with a 150-byte limit and 100-byte
    chunks, the 2nd chunk crosses the threshold, so the 3rd chunk must never be
    requested and the downstream app must never run to completion.
    """
    chunks = [
        {"type": "http.request", "body": b"a" * 100, "more_body": True},
        {"type": "http.request", "body": b"b" * 100, "more_body": True},  # crosses 150
        {"type": "http.request", "body": b"c" * 100, "more_body": False},  # must NOT be read
    ]
    handed_out = 0

    async def receive():
        nonlocal handed_out
        if handed_out >= len(chunks):  # pragma: no cover — we 413 well before here
            return {"type": "http.disconnect"}
        msg = chunks[handed_out]
        handed_out += 1
        return msg

    downstream_completed = False

    async def fake_app(scope, recv, snd):
        nonlocal downstream_completed
        while True:
            msg = await recv()
            if not msg.get("more_body", False):
                break
        downstream_completed = True  # only reached if the whole body drained

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/save", "headers": []}
    await server._BodySizeLimitMiddleware(fake_app, max_bytes=150)(scope, receive, send)

    statuses = [m for m in sent if m["type"] == "http.response.start"]
    assert statuses and statuses[0]["status"] == 413
    # The crux: the oversized tail was never read, and the app never completed.
    assert handed_out == 2, (
        f"middleware read {handed_out} chunks; it must stop at the chunk that "
        "crosses the limit (2) and never request the oversized tail (#297)"
    )
    assert downstream_completed is False, (
        "downstream app must never see the full oversized body"
    )


@pytest.fixture()
def live_server(monkeypatch):
    """A real uvicorn server on an ephemeral port with a tiny body limit.

    Needed because Starlette's TestClient can't model a server that responds
    mid-upload — it raises "error parsing the body" when the app answers 413
    before the streamed request finishes. A real socket + genuine
    ``Transfer-Encoding: chunked`` is the only faithful reproduction of the
    chunked-bypass the smoke test failed to exercise (#297).
    """
    import uvicorn

    for mw in server.app.user_middleware:
        if mw.cls is server._BodySizeLimitMiddleware:
            monkeypatch.setitem(mw.kwargs, "max_bytes", 256)
    server.app.middleware_stack = None

    config = uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not srv.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert srv.started, "uvicorn did not start in time"
        port = srv.servers[0].sockets[0].getsockname()[1]
        yield port
    finally:
        srv.should_exit = True
        thread.join(timeout=10)
        server.app.middleware_stack = None


def test_real_chunked_upload_over_limit_returns_413(live_server):
    """End-to-end: a genuine ``Transfer-Encoding: chunked`` upload with no
    Content-Length and a body over the limit is rejected 413 by the real app.

    This is the exact request shape the 2026-05-01 smoke test *intended* to send
    but didn't (``curl --data-binary @file`` set Content-Length and took the
    header fast-path instead). We hand-write the chunked framing over a socket.
    """
    port = live_server
    # When the app is built with a bearer token (e.g. PALINODE_API_TOKEN set in
    # CI), auth runs BEFORE the body-size check — an unauthenticated request
    # would 401, never reaching the 413 path. Authenticate so we exercise the
    # body guard, not the auth guard.
    auth_line = b""
    if server._api_token:
        auth_line = f"Authorization: Bearer {server._api_token}\r\n".encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        sock.sendall(
            b"POST /save HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + auth_line
            + b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
        )
        # 256-byte limit; send 128-byte chunks — the 3rd (384 B) crosses it.
        chunk = b"x" * 128
        frame = f"{len(chunk):x}".encode() + b"\r\n" + chunk + b"\r\n"
        response = b""
        try:
            for _ in range(40):  # 40 * 128 = 5120 B >> 256
                sock.sendall(frame)
                sock.settimeout(0.1)
                try:
                    data = sock.recv(4096)
                    if data:
                        response += data
                        break
                except socket.timeout:
                    continue
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Server sent 413 + `connection: close` and hung up mid-send —
            # itself a valid outcome; read whatever response arrived.
            pass

        if not response:
            sock.settimeout(5)
            try:
                response = sock.recv(4096)
            except (socket.timeout, OSError):
                response = b""
        assert response.startswith(b"HTTP/1.1 413") or b" 413 " in response[:64], (
            f"expected 413 for an oversized chunked upload, got: {response[:120]!r}"
        )
    finally:
        sock.close()


def test_max_bytes_constant_reused():
    """The middleware is wired with the module-level _MAX_REQUEST_BYTES constant,
    not a hard-coded value — operators can tune it via PALINODE_MAX_REQUEST_BYTES.
    """
    for mw in server.app.user_middleware:
        if mw.cls is server._BodySizeLimitMiddleware:
            assert mw.kwargs["max_bytes"] == server._MAX_REQUEST_BYTES
            return
    pytest.fail("BodySizeLimitMiddleware not registered on the app")
