"""Tests for bearer-token auth on the MCP HTTP transport (#289).

The MCP HTTP server (port 6341) mirrors the API server's auth:
  - Token shared via PALINODE_API_TOKEN / PALINODE_API_TOKEN_FILE
  - Startup gate via PALINODE_MCP_BIND_INTENT=public (distinct from
    PALINODE_API_BIND_INTENT — a separate opt-in for the MCP surface)
  - Gate fires inside ``main_http()`` NOT at module import time, because
    ``palinode/mcp.py`` is imported for the stdio transport too
  - ``/healthz`` is the MCP-exempt path (cf. ``/health`` on the API server)
"""
from __future__ import annotations

import importlib
import inspect

import pytest

from palinode.core.auth import (
    BearerAuthMiddleware,
    MCP_EXEMPT_PATHS,
    API_EXEMPT_PATHS,
    validate_auth_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fake_app(scope, receive, send):
    """Minimal ASGI inner app — always returns 200."""
    if scope.get("type") == "http":
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b"{}"})


async def _drive_middleware(
    token: str | None,
    path: str,
    auth_header: str | None = None,
    scope_type: str = "http",
) -> tuple[int | None, list[tuple[bytes, bytes]]]:
    """Drive ``BearerAuthMiddleware`` as a standalone ASGI app.

    Returns ``(status_code, response_headers)`` from the start message.
    ``status_code`` is ``None`` when no http.response.start was emitted
    (e.g. lifespan scope).
    """
    messages: list[dict] = []

    async def capture_send(message: dict) -> None:
        messages.append(message)

    headers: list[tuple[bytes, bytes]] = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header.encode()))

    scope = {"type": scope_type, "path": path, "headers": headers}
    mw = BearerAuthMiddleware(_fake_app, token=token, exempt_paths=MCP_EXEMPT_PATHS)
    await mw(scope, lambda: None, capture_send)

    start = next((m for m in messages if m.get("type") == "http.response.start"), None)
    resp_headers = start["headers"] if start else []
    return (start["status"] if start else None), resp_headers


# ---------------------------------------------------------------------------
# 1–3: MCP startup gate via validate_auth_config
# ---------------------------------------------------------------------------


def test_mcp_gate_public_no_token_raises():
    """validate_auth_config with PALINODE_MCP_BIND_INTENT semantics must raise
    SystemExit when no token is supplied."""
    with pytest.raises(SystemExit) as excinfo:
        validate_auth_config(
            True, None, bind_intent_var="PALINODE_MCP_BIND_INTENT"
        )
    msg = str(excinfo.value)
    assert "REFUSING TO START" in msg
    assert "PALINODE_MCP_BIND_INTENT" in msg
    assert "PALINODE_API_TOKEN" in msg  # shared token setup instructions


def test_mcp_gate_public_with_token_ok():
    """Public bind + token configured → no SystemExit."""
    # Must not raise
    validate_auth_config(True, "my-token", bind_intent_var="PALINODE_MCP_BIND_INTENT")


def test_mcp_gate_default_no_token_ok():
    """No bind intent + no token (local-dev default) → no SystemExit."""
    # Must not raise
    validate_auth_config(False, None, bind_intent_var="PALINODE_MCP_BIND_INTENT")


# ---------------------------------------------------------------------------
# 4: Gate is inside main_http(), NOT at module-import time
# ---------------------------------------------------------------------------


def test_gate_not_at_module_import_time(monkeypatch: pytest.MonkeyPatch):
    """Importing (or reloading) palinode.mcp with
    PALINODE_MCP_BIND_INTENT=public and no token must NOT raise — the gate
    lives inside main_http(), which the stdio path never calls."""
    monkeypatch.setenv("PALINODE_MCP_BIND_INTENT", "public")
    monkeypatch.delenv("PALINODE_API_TOKEN", raising=False)
    monkeypatch.delenv("PALINODE_API_TOKEN_FILE", raising=False)
    import palinode.mcp  # noqa: F401, PLC0415
    # Reload to ensure the env change is re-evaluated at import time
    importlib.reload(palinode.mcp)  # must not raise SystemExit


# ---------------------------------------------------------------------------
# 5–9: Middleware behaviour (direct ASGI, MCP-specific exempt paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_token_auth_disabled():
    """No token configured → all requests pass through without auth."""
    status, _ = await _drive_middleware(None, "/mcp")
    assert status == 200


@pytest.mark.asyncio
async def test_token_required_no_header_yields_401():
    """Token configured, no Authorization header → 401 Unauthorized."""
    status, _ = await _drive_middleware("t-secret", "/mcp")
    assert status == 401


@pytest.mark.asyncio
async def test_token_required_correct_bearer_passes():
    """Correct Bearer token → request passes through middleware."""
    status, _ = await _drive_middleware(
        "t-secret", "/mcp", auth_header="Bearer t-secret"
    )
    assert status == 200


@pytest.mark.asyncio
async def test_token_required_wrong_bearer_yields_401():
    """Wrong token value → 401."""
    status, _ = await _drive_middleware(
        "t-correct", "/mcp", auth_header="Bearer wrong-token"
    )
    assert status == 401


@pytest.mark.asyncio
async def test_healthz_always_exempt():
    """/healthz is the MCP exempt path — must never require a token."""
    # Token is set but /healthz should still pass through
    status, _ = await _drive_middleware("t-secret", "/healthz")
    assert status == 200


# ---------------------------------------------------------------------------
# 10–14: Malformed Authorization headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        "Basic abc",          # wrong scheme
        "Bearer",             # missing token value
        "bearer t-correct",   # lowercase scheme — compare_digest is byte-exact
        "Bearer  t-correct",  # extra space before token
        "Token t-correct",    # wrong scheme name
    ],
)
async def test_malformed_auth_yields_401(header: str):
    """Malformed or wrong-scheme Authorization headers must be rejected."""
    status, _ = await _drive_middleware(
        "t-correct", "/mcp", auth_header=header
    )
    assert status == 401, f"header={header!r} should have yielded 401"


# ---------------------------------------------------------------------------
# 15: Non-HTTP ASGI scopes pass through without auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_scope_passes_through():
    """ASGI lifespan events must bypass auth entirely — auth on a lifespan
    scope would deadlock the MCP server startup."""
    called = False

    async def fake_app(scope, recv, snd):
        nonlocal called
        called = True

    mw = BearerAuthMiddleware(fake_app, token="t-secret", exempt_paths=MCP_EXEMPT_PATHS)
    await mw({"type": "lifespan"}, lambda: None, lambda msg: None)
    assert called


# ---------------------------------------------------------------------------
# 16: Security properties
# ---------------------------------------------------------------------------


def test_compare_digest_used_in_middleware():
    """BearerAuthMiddleware.__call__ MUST use hmac.compare_digest, not ==,
    to prevent timing-oracle attacks on the token."""
    src = inspect.getsource(BearerAuthMiddleware.__call__)
    assert "hmac.compare_digest" in src, (
        "BearerAuthMiddleware.__call__ must use hmac.compare_digest"
    )
    # Guard against a future refactor introducing a naive == comparison
    assert "self._expected_header ==" not in src
    assert "== self._expected_header" not in src


# ---------------------------------------------------------------------------
# Bonus: MCP and API servers use different exempt paths
# ---------------------------------------------------------------------------


def test_mcp_exempt_path_is_healthz_not_health():
    """MCP uses /healthz as its health endpoint (not /health like the API)."""
    assert "/healthz" in MCP_EXEMPT_PATHS
    assert "/health" not in MCP_EXEMPT_PATHS


def test_api_exempt_paths_cover_health_variants():
    """API exempt paths cover all three health endpoints."""
    assert "/health" in API_EXEMPT_PATHS
    assert "/health/watcher" in API_EXEMPT_PATHS
    assert "/health/auto-summary" in API_EXEMPT_PATHS
    assert "/healthz" not in API_EXEMPT_PATHS


def test_401_response_includes_www_authenticate_header():
    """RFC 6750 §3 requires WWW-Authenticate: Bearer on 401 responses."""
    import asyncio

    async def _check():
        _, headers = await _drive_middleware("t-secret", "/mcp")
        header_map = {k.lower(): v for k, v in headers}
        assert b"www-authenticate" in header_map
        assert header_map[b"www-authenticate"].lower().startswith(b"bearer")

    asyncio.run(_check())
