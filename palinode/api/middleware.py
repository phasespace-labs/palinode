"""Cross-cutting middleware and logging infrastructure for the Palinode API.

Extracted from ``palinode.api.server`` (#325) to keep the server module
focused on route definitions. All classes and functions here are
self-contained — they depend on stdlib, ``palinode.core.db``, and
``palinode.core.config`` only.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from palinode.core.db import utc_now as _utc_now

__all__ = [
    "SecretRedactingFilter",
    "JsonlFormatter",
    "BearerAuthMiddleware",
    "BodySizeLimitMiddleware",
    "BodyTooLargeError",
    "parse_cors_origins",
    "load_api_token",
    "validate_auth_config",
    "SECRET_PATTERNS",
    "redact_secrets",
]

# ── Secret redaction (L4) ──────────────────────────────────────────────────
# Memory files routinely contain credentials (API keys, tokens, basic-auth
# URLs) and any error path that calls logger.exception() will surface those
# in tracebacks/locals. The patterns below are scrubbed from log messages
# and exception text before emission.

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Anthropic-style sk-ant-... (must come before generic sk- so the longer
    # prefix wins).
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***REDACTED***"),
    # OpenAI / generic sk-...
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-***REDACTED***"),
    # Slack bot/user/app tokens
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "xox*-***REDACTED***"),
    # GitHub personal-access tokens
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "gh*_***REDACTED***"),
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***REDACTED***"),
    # Basic-auth credentials embedded in URLs: scheme://user:password@host
    (
        re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://)([^/\s:@]+):([^/\s:@]+)@"),
        r"\1\2:***REDACTED***@",
    ),
    # Generic 32+ char hex/base64 tokens preceded by a header-name keyword.
    (
        re.compile(
            r"((?:api[_\-]?key|bearer|authorization|token)"
            r"(?:\s*[:=]\s*(?:bearer\s+)?|\s+)[\"']?)"
            r"([A-Za-z0-9_\-=+/.]{32,})",
            re.IGNORECASE,
        ),
        r"\1***REDACTED***",
    ),
)


def redact_secrets(text: str) -> str:
    """Apply ``SECRET_PATTERNS`` to *text*. Returns text unchanged on no match."""
    if not text:
        return text
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Strip credentials from log messages and traceback text before emission.

    Mutates ``record.msg`` (after expansion) so downstream formatters and
    handlers see scrubbed content. ``exc_text`` is rebuilt lazily by the
    formatter; we redact it here too if it has been pre-rendered, and we
    pre-render-and-redact when ``exc_info`` is present so the cached value
    handlers later rely on (``record.exc_text``) is already safe.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Render the formatted message once, then re-flow it as a literal so
        # downstream %-style formatting does not re-expand any redacted args.
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 -- never let a logging filter raise
            rendered = str(record.msg)
        scrubbed = redact_secrets(rendered)
        if scrubbed != rendered or record.args:
            record.msg = scrubbed
            record.args = None

        # Render and scrub the traceback up front so handlers see the
        # redacted version (Formatter caches via record.exc_text).
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_secrets(
                logging.Formatter().formatException(record.exc_info)
            )
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)

        return True


class JsonlFormatter(logging.Formatter):
    """Logging Formatter dictating a JSONL chronological schema format."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": _utc_now().isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        })


# ── CORS ───────────────────────────────────────────────────────────────────


def parse_cors_origins(raw: str) -> list[str]:
    """Validate and normalize ``PALINODE_CORS_ORIGINS``.

    - Reject literal ``'*'`` (with or without surrounding whitespace,
      anywhere in the comma-separated list) -- silent wildcard CORS is the
      failure mode the marketplace flagged.
    - Strip whitespace and skip empty entries.
    - Each origin must parse as ``http(s)://host[:port][/path]``; missing
      scheme or netloc raises ``ValueError``.
    """
    origins: list[str] = []
    for raw_origin in raw.split(","):
        origin = raw_origin.strip()
        if not origin:
            continue
        if origin == "*":
            raise ValueError(
                "refusing to start with CORS wildcard — set "
                "PALINODE_CORS_ORIGINS to specific origins"
            )
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"invalid CORS origin {origin!r}: must be a full http(s) URL"
            )
        origins.append(origin)
    if not origins:
        raise ValueError(
            "PALINODE_CORS_ORIGINS resolved to an empty list — set at least "
            "one valid origin or unset the variable to use the default"
        )
    return origins


# ── Bearer auth ────────────────────────────────────────────────────────────

_logger = logging.getLogger("palinode.api")


def load_api_token() -> str | None:
    """Return the API bearer token, or ``None`` if unconfigured.

    Source priority:
      1. ``PALINODE_API_TOKEN`` env var (preferred for casual setups).
      2. ``PALINODE_API_TOKEN_FILE`` -- path to a file whose contents are
         the token. Supports docker-secrets / sealed-secrets / k8s-CSI
         patterns where the secret arrives on disk.

    Whitespace is stripped; empty values resolve to ``None``.
    """
    env_tok = os.environ.get("PALINODE_API_TOKEN", "").strip()
    if env_tok:
        return env_tok
    file_path = os.environ.get("PALINODE_API_TOKEN_FILE", "").strip()
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip() or None
        except OSError:
            _logger.error(
                "PALINODE_API_TOKEN_FILE set but unreadable; "
                "auth will be unconfigured"
            )
            return None
    return None


def validate_auth_config(token: str | None, *, bind_intent_public: bool) -> None:
    """Refuse to start when binding public without a token.

    Fires at MODULE IMPORT (see call site in ``server.py``), so the
    ``SystemExit`` propagates out of any startup path -- including
    ``uvicorn`` invoked directly with ``palinode.api.server:app``.
    """
    if bind_intent_public and token is None:
        raise SystemExit(
            "REFUSING TO START: PALINODE_API_BIND_INTENT=public requires "
            "PALINODE_API_TOKEN (or PALINODE_API_TOKEN_FILE) to be set.\n\n"
            "Generate a token:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(32))'\n\n"
            "Then set:\n"
            "  export PALINODE_API_TOKEN=<value>\n"
        )


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` when a token is configured.

    No-op pass-through when ``token`` is ``None`` so local-first development
    keeps working without ceremony. Health endpoints are always exempt.

    Uses ``hmac.compare_digest`` to remove the timing side-channel.
    """

    _AUTH_EXEMPT_PATHS = frozenset({"/health", "/health/watcher"})

    def __init__(self, app, token: str | None) -> None:
        self.app = app
        self._token = token
        self._expected_header = (
            f"Bearer {token}".encode() if token else None
        )

    async def __call__(self, scope, receive, send) -> None:
        if self._expected_header is None or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in self._AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        provided = b""
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                provided = value
                break

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


# ── Body-size limit ────────────────────────────────────────────────────────


class BodyTooLargeError(Exception):
    """Internal sentinel for the body-size middleware."""


class BodySizeLimitMiddleware:
    """ASGI middleware enforcing a byte limit on the *streamed* request body.

    Header-based fast-path is preserved so well-behaved clients get rejected
    before any body is buffered. The streaming check tallies bytes during
    ``receive()`` as defence against chunked-transfer-encoding bypass.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value.decode("latin-1"))
                except (UnicodeDecodeError, ValueError):
                    await self._send_413(send)
                    return
                if declared > self.max_bytes:
                    await self._send_413(send)
                    return
                break

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"") or b""
                received += len(body)
                if received > self.max_bytes:
                    raise BodyTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLargeError:
            await self._send_413(send)

    @staticmethod
    async def _send_413(send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"connection", b"close"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"Request body too large"}',
            }
        )
