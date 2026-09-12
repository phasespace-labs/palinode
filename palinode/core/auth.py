"""Shared bearer-token auth primitives for the API and MCP HTTP servers.

Extracted so the MCP HTTP transport can share the same middleware,
token loader, and startup gate as the API server without duplicating the
security-sensitive comparison logic.

Public names
------------
load_api_token()        — read PALINODE_API_TOKEN / PALINODE_API_TOKEN_FILE
BearerAuthMiddleware    — ASGI middleware; no-op when token is None
validate_auth_config()  — SystemExit gate for BIND_INTENT=public + no-token
validate_bind_auth()    — SystemExit gate for non-loopback bind + no-token
is_loopback_host()      — classify a bind host as loopback or not
allow_unauth_opt_out()  — read the PALINODE_API_ALLOW_UNAUTH opt-out (one knob
                          per deployment; the MCP HTTP transport shares it)
API_EXEMPT_PATHS        — paths always allowed on the API server
MCP_EXEMPT_PATHS        — paths always allowed on the MCP HTTP server
"""
from __future__ import annotations

import hmac
import ipaddress
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
    ``ExecStartPost`` checks, Tailscale Funnel monitors) don't need the token.

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


def is_loopback_host(host: str) -> bool:
    """Return ``True`` when ``host`` can only be reached from this machine.

    ``localhost`` and any address in ``127.0.0.0/8`` / ``::1`` are loopback.
    Everything else — ``0.0.0.0``, ``::``, a LAN or Tailscale address, a
    hostname — is treated as network-reachable. Unparseable input is
    non-loopback: the gate fails closed.
    """
    host = host.strip().strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allow_unauth_opt_out() -> bool:
    """Return ``True`` when ``PALINODE_API_ALLOW_UNAUTH`` is set truthy.

    The one explicit opt-out for serving token-less on a non-loopback bind.
    Both the API server and the MCP HTTP transport read this same variable —
    one knob per deployment, no per-surface twin.
    """
    return os.environ.get("PALINODE_API_ALLOW_UNAUTH", "").strip().lower() in (
        "1", "true", "yes",
    )


def bind_host_phrasing(
    host_var: str, host: str, host_var_kind: str = "env"
) -> tuple[str, str]:
    """Render a bind host as the operator spelled it, plus its loopback remedy.

    Returns ``(stated, remedy)``. An env var reads ``NAME=value`` and is
    fixed with ``export NAME=127.0.0.1``; a flag reads ``--host value`` and
    is fixed with ``--host 127.0.0.1``. Callers that resolve a host from
    several sources pass the one that actually won, so refusal and warning
    text point at the knob the operator turned rather than at the canonical
    env var they may never have set.
    """
    if host_var_kind == "flag":
        return f"{host_var} {host}", f"{host_var} 127.0.0.1"
    return f"{host_var}={host}", f"export {host_var}=127.0.0.1"


def validate_bind_auth(
    host: str,
    token: str | None,
    *,
    allow_unauth: bool,
    host_var: str = "PALINODE_API_HOST",
    host_var_kind: str = "env",
    allow_unauth_var: str = "PALINODE_API_ALLOW_UNAUTH",
    exposure: str = "an unauthenticated API",
    detail: str = "",
) -> None:
    """Refuse to serve unauthenticated on a non-loopback bind.

    Keys on the *resolved bind host*, not on any stated intent: a server
    that would listen on a network-reachable address without a bearer
    token exits with ``SystemExit`` unless the operator has explicitly
    opted out via ``allow_unauth`` (``PALINODE_API_ALLOW_UNAUTH=1``).

    Parameters
    ----------
    host:
        The address the server will bind.
    token:
        The resolved bearer token, or ``None`` if unconfigured.
    allow_unauth:
        ``True`` when the operator set the explicit opt-out env var.
    host_var, allow_unauth_var:
        Names of the knobs named in the error message.
    host_var_kind:
        How ``host_var`` is spelled by the operator: ``"env"`` for an
        environment variable (``NAME=value`` / ``export NAME=...``) or
        ``"flag"`` for a command-line flag (``--host value``). The caller
        passes whichever source actually resolved ``host``, so the
        remediation names the knob the operator really turned — an
        operator who set ``--host`` must not be sent to an env var they
        never set.
    exposure:
        What the refusal would otherwise have served, for the message —
        the MCP HTTP transport names its tool surface here.
    detail:
        Optional sentence appended to the first paragraph of the message
        (the MCP HTTP transport says here that it has no token of its own).
    """
    if token is not None or allow_unauth or is_loopback_host(host):
        return
    stated, remedy = bind_host_phrasing(host_var, host, host_var_kind)
    raise SystemExit(
        f"REFUSING TO START: {stated} is not a loopback address and no "
        "PALINODE_API_TOKEN (or PALINODE_API_TOKEN_FILE) is set — this would "
        f"serve {exposure} to the network."
        + (f" {detail}" if detail else "")
        + "\n\n"
        "Either bind loopback-only:\n"
        f"  {remedy}\n\n"
        "or generate a token:\n"
        "  python -c 'import secrets; print(secrets.token_urlsafe(32))'\n"
        "  export PALINODE_API_TOKEN=<value>\n\n"
        "or, for a deliberately token-less network-isolated host (e.g. "
        "Tailscale-only), opt out explicitly:\n"
        f"  export {allow_unauth_var}=1\n"
    )
