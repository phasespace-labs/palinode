"""Shared bearer-token auth primitives for the API and MCP HTTP servers.

Extracted so the MCP HTTP transport (#289) can share the same middleware,
token loader, and startup gate as the API server without duplicating the
security-sensitive comparison logic.

Public names
------------
load_api_token()        — read PALINODE_API_TOKEN / PALINODE_API_TOKEN_FILE
BearerAuthMiddleware    — ASGI middleware; no-op when token is None
validate_auth_config()  — SystemExit gate for public-bind + no-token
API_EXEMPT_PATHS        — paths always allowed on the API server
MCP_EXEMPT_PATHS        — paths always allowed on the MCP HTTP server
"""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

logger = logging.getLogger("palinode.auth")

#: Paths that bypass auth on the API server (port 6340).
API_EXEMPT_PATHS: frozenset[str] = frozenset({
    "/health",
    "/health/watcher",
    "/health/auto-summary",
})

#: Paths that bypass auth on the MCP HTTP server (port 6341).
MCP_EXEMPT_PATHS: frozenset[str] = frozenset({"/healthz"})


def load_api_token() -> str | None:
    """Return the bearer token, or ``None`` if unconfigured.

    Source priority:
      1. ``PALINODE_API_TOKEN`` env var (preferred for casual setups).
      2. ``PALINODE_API_TOKEN_FILE`` — path to a file whose contents are the
         token. Supports docker-secrets / sealed-secrets / k8s-CSI patterns
         where the secret arrives on disk rather than in the env.

    Whitespace is stripped; empty values resolve to ``None`` (treated as
    "no token configured"). File-read errors are logged and fall back to
    ``None`` so a malformed deployment fails closed via the bind-intent gate
    rather than silently exposing the service.
    """
    env_tok = os.environ.get("PALINODE_API_TOKEN", "").strip()
    if env_tok:
        return env_tok
    file_path = os.environ.get("PALINODE_API_TOKEN_FILE", "").strip()
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip() or None
        except OSError:
            # Don't echo the path — it may itself be sensitive (e.g. a
            # mounted secret path that hints at the deployment topology).
            # The operator can grep the journal for this exact message.
            logger.error(
                "PALINODE_API_TOKEN_FILE set but unreadable; "
                "auth will be unconfigured"
            )
            return None
    return None


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` when a token is configured.

    No-op pass-through when ``token`` is ``None`` so local-first development
    keeps working without ceremony. Configured ``exempt_paths`` are always
    allowed so uptime probes (k8s readiness/liveness, systemd
    ``ExecStartPost`` checks, external health monitors) don't need the token.

    The comparison uses ``hmac.compare_digest`` to remove the timing
    side-channel that a naive ``==`` would expose. The expected header is
    pre-encoded once at construction time so the hot path is a single
    constant-time byte compare.
    """

    def __init__(
        self,
        app,
        token: str | None,
        exempt_paths: frozenset[str] | None = None,
    ) -> None:
        self.app = app
        self._token = token
        self._expected_header = (
            f"Bearer {token}".encode() if token else None
        )
        self._exempt_paths: frozenset[str] = (
            exempt_paths if exempt_paths is not None else frozenset()
        )

    async def __call__(self, scope, receive, send) -> None:
        if self._expected_header is None or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in self._exempt_paths:
            await self.app(scope, receive, send)
            return

        provided = b""
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                provided = value
                break

        # Both compare_digest operands must be bytes of the same type. The
        # length check is short-circuit and not timing-relevant — the
        # secret length is fixed at config-time and the constant-time
        # compare runs over equal-length inputs.
        if not provided or not hmac.compare_digest(provided, self._expected_header):
            await self._send_401(send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send) -> None:
        body = b'{"detail":"Unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="palinode"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def validate_auth_config(
    bind_intent_public: bool,
    token: str | None,
    *,
    bind_intent_var: str = "PALINODE_API_BIND_INTENT",
) -> None:
    """Refuse to start when binding publicly without a token.

    Raises ``SystemExit`` with an operator-readable message so the process
    exits loudly rather than silently serving an unauthenticated surface.

    Parameters
    ----------
    bind_intent_public:
        ``True`` when the caller's bind-intent env var is ``"public"``.
    token:
        The resolved bearer token, or ``None`` if unconfigured.
    bind_intent_var:
        Name of the env var that controls this server's bind intent;
        included in the error message so the operator knows what to set.
        Defaults to ``PALINODE_API_BIND_INTENT`` (the API server's var).
    """
    if bind_intent_public and token is None:
        raise SystemExit(
            f"REFUSING TO START: {bind_intent_var}=public requires "
            "PALINODE_API_TOKEN (or PALINODE_API_TOKEN_FILE) to be set.\n\n"
            "Generate a token:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(32))'\n\n"
            "Then set:\n"
            "  export PALINODE_API_TOKEN=<value>\n"
        )
