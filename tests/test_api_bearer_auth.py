"""Tests for the bearer-token auth middleware (Tier A — last public-scan high).

The middleware is default-off so existing local-dev workflows keep working
without configuration. When ``PALINODE_API_TOKEN`` (or
``PALINODE_API_TOKEN_FILE``) is set, every request must carry an
``Authorization: Bearer <token>`` header — except the ``/health`` and
``/health/watcher`` probes, which stay open for uptime checks.

The startup gate is the loud-fail counterpart: with
``PALINODE_API_BIND_INTENT=public`` set and no token configured, the API
must refuse to start so an operator cannot accidentally expose an
unauthenticated service.
"""
from __future__ import annotations

import importlib
import inspect
import os

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_server(monkeypatch: pytest.MonkeyPatch, **env: str | None):
    """Re-import ``palinode.api.server`` with patched env vars.

    The module reads ``PALINODE_API_TOKEN`` / ``PALINODE_API_TOKEN_FILE`` /
    ``PALINODE_API_BIND_INTENT`` once at import time, so each test that
    cares about a different configuration needs a fresh module. Returns
    the freshly loaded module.
    """
    # Clear any previous run's values so monkeypatch can supply fresh ones.
    for key in (
        "PALINODE_API_TOKEN",
        "PALINODE_API_TOKEN_FILE",
        "PALINODE_API_BIND_INTENT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            continue
        monkeypatch.setenv(key, value)
    # Force a clean re-import. importlib.reload preserves the module
    # identity so other test modules that imported it earlier see the
    # latest config too.
    import palinode.api.server as server_mod  # noqa: PLC0415
    return importlib.reload(server_mod)


# ---------------------------------------------------------------------------
# Token loader
# ---------------------------------------------------------------------------


def test_no_token_when_env_unset(monkeypatch: pytest.MonkeyPatch):
    """No env var, no file → ``_load_api_token`` returns None."""
    server = _reload_server(monkeypatch)
    assert server._api_token is None
    assert server._load_api_token() is None


def test_token_loaded_from_env(monkeypatch: pytest.MonkeyPatch):
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="secret-abc")
    assert server._api_token == "secret-abc"


def test_token_loaded_from_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """``PALINODE_API_TOKEN_FILE`` reads token from disk and strips trailing whitespace."""
    token_file = tmp_path / "tok"
    token_file.write_text("file-token-xyz\n", encoding="utf-8")
    server = _reload_server(
        monkeypatch, PALINODE_API_TOKEN_FILE=str(token_file)
    )
    assert server._api_token == "file-token-xyz"


def test_env_token_wins_over_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """When both are set, the env var takes precedence (cheaper, no I/O)."""
    token_file = tmp_path / "tok"
    token_file.write_text("from-file", encoding="utf-8")
    server = _reload_server(
        monkeypatch,
        PALINODE_API_TOKEN="from-env",
        PALINODE_API_TOKEN_FILE=str(token_file),
    )
    assert server._api_token == "from-env"


def test_unreadable_token_file_falls_through_to_none(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
):
    """Missing file path → returns None and logs an error (no crash)."""
    missing = tmp_path / "does-not-exist"
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN_FILE=str(missing))
    assert server._api_token is None


def test_whitespace_only_token_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    """A pure-whitespace env var must not enable auth (would 401 every request)."""
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="   \n\t  ")
    assert server._api_token is None


# ---------------------------------------------------------------------------
# Startup gate
# ---------------------------------------------------------------------------


def test_validate_auth_config_raises_when_public_bind_no_token(
    monkeypatch: pytest.MonkeyPatch,
):
    """The gate fires at MODULE IMPORT, not just from main(), so it
    propagates under uvicorn-direct invocation (the canonical systemd
    ExecStart pattern). See server.py module-scope
    ``_validate_auth_config(_api_token)`` call.
    """
    # SystemExit must propagate out of the import itself, not require an
    # explicit follow-up call.
    monkeypatch.delenv("PALINODE_API_TOKEN", raising=False)
    monkeypatch.delenv("PALINODE_API_TOKEN_FILE", raising=False)
    monkeypatch.setenv("PALINODE_API_BIND_INTENT", "public")
    import palinode.api.server  # noqa: F401  (preload baseline)
    import importlib
    with pytest.raises(SystemExit) as excinfo:
        importlib.reload(palinode.api.server)
    msg = str(excinfo.value)
    assert "REFUSING TO START" in msg
    assert "PALINODE_API_TOKEN" in msg


def test_validate_auth_config_passes_when_public_bind_with_token(
    monkeypatch: pytest.MonkeyPatch,
):
    server = _reload_server(
        monkeypatch,
        PALINODE_API_BIND_INTENT="public",
        PALINODE_API_TOKEN="set-token",
    )
    # Should not raise.
    server._validate_auth_config(server._api_token)


def test_validate_auth_config_passes_when_loopback_no_token(
    monkeypatch: pytest.MonkeyPatch,
):
    """Default deployment (loopback bind, no token) must keep working —
    that's the local-first promise."""
    server = _reload_server(monkeypatch)
    assert server._bind_intent_public is False
    assert server._api_token is None
    # Should not raise.
    server._validate_auth_config(server._api_token)


def test_validate_auth_config_fires_under_uvicorn_direct_import():
    """Regression test: systemd-style deployments invoke uvicorn directly
    with ``palinode.api.server:app``, bypassing ``main()``. If the gate
    is only inside ``main()``, the public bind would start unauthenticated
    despite ``PALINODE_API_BIND_INTENT=public``. This test simulates the
    uvicorn-direct path: a fresh Python subprocess that imports
    ``palinode.api.server:app`` (the FastAPI object), with the failing
    config in env. The import must raise SystemExit during the import
    itself.
    """
    import subprocess
    import sys
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PALINODE_API_BIND_INTENT": "public",
        # No token; no token-file.
    }
    # Strip any inherited token vars
    for k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        env.pop(k, None)

    # Mirror what `uvicorn palinode.api.server:app` does internally:
    # it imports the module to read the `app` attribute.
    code = "from palinode.api.server import app  # noqa: F401"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0, (
        f"Import should fail under public-bind+no-token, but exit was 0.\n"
        f"stderr: {result.stderr}"
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert "REFUSING TO START" in combined, (
        f"Expected SystemExit message in output:\n{combined}"
    )
    assert "PALINODE_API_TOKEN" in combined


# ---------------------------------------------------------------------------
# Middleware behaviour — TestClient drives the full ASGI stack
# ---------------------------------------------------------------------------


def test_no_token_loopback_search_works_without_auth(monkeypatch: pytest.MonkeyPatch):
    """Local-dev default: /health AND /search must not require auth."""
    server = _reload_server(monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    h = client.get("/health")
    assert h.status_code == 200

    s = client.post("/search", json={"query": "anything"})
    # /search may 200 or 5xx depending on backend availability, but it
    # must NOT 401 — that would mean auth was wired even though no token
    # is configured.
    assert s.status_code != 401


def test_token_set_no_auth_header_yields_401(monkeypatch: pytest.MonkeyPatch):
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="t-secret")
    client = TestClient(server.app, raise_server_exceptions=False)
    r = client.post("/search", json={"query": "x"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}
    # WWW-Authenticate must indicate Bearer per RFC 6750
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_health_endpoints_skip_auth_even_with_token(
    monkeypatch: pytest.MonkeyPatch,
):
    """Probes (k8s, systemd, Tailscale Funnel) shouldn't have to know the token."""
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="t-secret")
    client = TestClient(server.app, raise_server_exceptions=False)
    r = client.get("/health")
    assert r.status_code == 200


def test_token_set_wrong_token_yields_401(monkeypatch: pytest.MonkeyPatch):
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="t-correct")
    client = TestClient(server.app, raise_server_exceptions=False)
    r = client.post(
        "/search",
        json={"query": "x"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_token_set_correct_token_authorises(monkeypatch: pytest.MonkeyPatch):
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="t-correct")
    client = TestClient(server.app, raise_server_exceptions=False)
    r = client.post(
        "/search",
        json={"query": "x"},
        headers={"Authorization": "Bearer t-correct"},
    )
    # The auth layer let it through. The route handler may return any
    # non-401 status (200 if Ollama happens to be up in CI; 500/422 if
    # not) — what matters here is that the middleware did not block.
    assert r.status_code != 401


@pytest.mark.parametrize(
    "header",
    [
        "Basic abc",       # wrong scheme
        "Bearer",          # missing token
        "bearer t-correct",  # lowercase scheme — RFC 7235 says Bearer is case-insensitive,
                             # but compare_digest is byte-exact and we MUST be strict.
        "Bearer  t-correct",  # extra space
        "Token t-correct",  # wrong scheme name
    ],
)
def test_malformed_authorization_header_yields_401(
    monkeypatch: pytest.MonkeyPatch, header: str
):
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="t-correct")
    client = TestClient(server.app, raise_server_exceptions=False)
    r = client.post(
        "/search",
        json={"query": "x"},
        headers={"Authorization": header},
    )
    assert r.status_code == 401, f"header={header!r} should have 401'd"


# ---------------------------------------------------------------------------
# Direct ASGI tests for non-HTTP scope and timing-safe compare
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_scope_passes_through(monkeypatch: pytest.MonkeyPatch):
    """ASGI ``lifespan`` (and any non-http scope) must skip auth — auth on
    a lifespan event would deadlock startup."""
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="t-secret")
    called = False

    async def fake_app(scope, recv, snd):
        nonlocal called
        called = True

    mw = server._BearerAuthMiddleware(fake_app, token="t-secret")
    await mw({"type": "lifespan"}, lambda: None, lambda msg: None)
    assert called


def test_compare_digest_is_used():
    """The middleware MUST use ``hmac.compare_digest`` rather than ``==``
    to compare the bearer token, to avoid leaking it via response timing.

    A naive ``provided == expected`` short-circuits on the first byte
    mismatch in CPython's bytes comparison, leaking timing information
    per byte. ``compare_digest`` runs in constant time over the inputs.

    This test inspects the source of ``__call__`` and asserts the
    constant-time compare is referenced verbatim — a guard against a
    future refactor accidentally introducing ``provided == self._expected_header``.
    """
    from palinode.api import server  # noqa: PLC0415

    src = inspect.getsource(server._BearerAuthMiddleware.__call__)
    assert "hmac.compare_digest" in src, (
        "_BearerAuthMiddleware.__call__ must compare with hmac.compare_digest"
    )
    # The obvious antipattern (string ``==`` on the expected header) must
    # be absent. We don't blanket-ban ``==`` because legitimate uses like
    # ``scope.get("type") != "http"`` are unrelated to the secret compare.
    assert "self._expected_header ==" not in src
    assert "== self._expected_header" not in src


# ---------------------------------------------------------------------------
# Middleware registration order
# ---------------------------------------------------------------------------


def test_bearer_middleware_registered_on_app(monkeypatch: pytest.MonkeyPatch):
    """The middleware is wired into the app — a future refactor must not
    drop the ``add_middleware`` call without also dropping these tests."""
    server = _reload_server(monkeypatch, PALINODE_API_TOKEN="t-secret")
    classes = [mw.cls for mw in server.app.user_middleware]
    assert server._BearerAuthMiddleware in classes
