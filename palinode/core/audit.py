"""
MCP Audit Logger

Structured JSONL logging for every MCP tool call.
Writes to {PALINODE_DIR}/.audit/mcp-calls.jsonl by default.

Each entry records timestamp, tool name, sanitized arguments,
duration, status, and client identity for compliance and debugging.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from palinode.core.config import AuditConfig

logger = logging.getLogger("palinode.audit")

# Fields whose values are truncated in log entries for privacy
_TRUNCATE_FIELDS = {"content", "query", "summary", "prompt", "description", "text"}
_TRUNCATE_MAX = 200


def _sanitize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of arguments with long content fields truncated."""
    sanitized: dict[str, Any] = {}
    for key, value in arguments.items():
        if key in _TRUNCATE_FIELDS and isinstance(value, str) and len(value) > _TRUNCATE_MAX:
            sanitized[key] = value[:_TRUNCATE_MAX] + "..."
        else:
            sanitized[key] = value
    return sanitized


def _resolve_client_info() -> dict[str, str | None]:
    """Gather available client identity from environment."""
    return {
        "harness": os.environ.get("MCP_CLIENT_NAME") or os.environ.get("CLAUDE_CODE") or None,
        "project": os.environ.get("PALINODE_PROJECT") or None,
        "cwd": os.environ.get("CWD") or None,
    }


class AuditLogger:
    """Append-only JSONL audit logger for MCP tool calls."""

    def __init__(self, memory_dir: str, audit_config: AuditConfig):
        self._enabled = audit_config.enabled
        if not self._enabled:
            self._path: Path | None = None
            return

        log_path = audit_config.log_path
        if os.path.isabs(log_path):
            self._path = Path(log_path)
        else:
            self._path = Path(memory_dir) / log_path

        # Create parent directory if needed
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Cannot create audit directory %s: %s", self._path.parent, e)
            self._enabled = False
            self._path = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def log_path(self) -> Path | None:
        return self._path

    def log_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        duration_ms: float,
        status: str,
        error: str | None = None,
    ) -> None:
        """Write a single audit entry. Never raises — errors are logged and swallowed."""
        if not self._enabled or self._path is None:
            return

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "arguments": _sanitize_arguments(arguments),
            "duration_ms": round(duration_ms, 1),
            "status": status,
            "error": error,
            "client_info": _resolve_client_info(),
        }

        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
        except OSError as e:
            logger.warning("Audit write failed: %s", e)
