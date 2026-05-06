"""Direct unit tests for palinode.api.middleware (#325).

These exercise the extracted middleware classes and helpers without
FastAPI — they call the functions and classes directly.
"""
from __future__ import annotations

import io
import json
import logging
import os
import time

import pytest


# ---------------------------------------------------------------------------
# SecretRedactingFilter
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[logging.Logger, io.StringIO]:
    from palinode.api.middleware import SecretRedactingFilter

    log = logging.getLogger(f"test.middleware.{time.time_ns()}")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    log.addHandler(handler)
    return log, buf


def test_secret_redaction_redacts_known_patterns():
    """SecretRedactingFilter scrubs API keys, tokens, and basic-auth URLs."""
    log, buf = _make_logger()
    log.warning("key: sk-abcdefghijklmnopqrstuvwxyz0123456789")
    out = buf.getvalue()
    assert "sk-abcdefghij" not in out
    assert "***REDACTED***" in out


def test_secret_redaction_leaves_clean_messages():
    log, buf = _make_logger()
    log.warning("nothing sensitive here")
    assert buf.getvalue().strip() == "nothing sensitive here"


# ---------------------------------------------------------------------------
# JsonlFormatter
# ---------------------------------------------------------------------------


def test_jsonl_formatter_emits_valid_json_lines():
    from palinode.api.middleware import JsonlFormatter

    log = logging.getLogger(f"test.jsonl.{time.time_ns()}")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonlFormatter())
    log.addHandler(handler)

    log.info("test message")
    line = buf.getvalue().strip()
    parsed = json.loads(line)
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "test message"
    assert "timestamp" in parsed
    assert parsed["timestamp"].endswith("Z")


# ---------------------------------------------------------------------------
# parse_cors_origins
# ---------------------------------------------------------------------------


def test_parse_cors_origins_handles_csv():
    from palinode.api.middleware import parse_cors_origins

    result = parse_cors_origins("http://localhost:3000, http://127.0.0.1:3000")
    assert result == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_parse_cors_origins_rejects_wildcard():
    from palinode.api.middleware import parse_cors_origins

    with pytest.raises(ValueError, match="wildcard"):
        parse_cors_origins("*")


def test_parse_cors_origins_rejects_empty():
    from palinode.api.middleware import parse_cors_origins

    with pytest.raises(ValueError):
        parse_cors_origins("")


# ---------------------------------------------------------------------------
# load_api_token
# ---------------------------------------------------------------------------


def test_load_api_token_reads_env(monkeypatch):
    from palinode.api.middleware import load_api_token

    monkeypatch.setenv("PALINODE_API_TOKEN", "test-tok-123")
    monkeypatch.delenv("PALINODE_API_TOKEN_FILE", raising=False)
    assert load_api_token() == "test-tok-123"


def test_load_api_token_returns_none_when_unset(monkeypatch):
    from palinode.api.middleware import load_api_token

    monkeypatch.delenv("PALINODE_API_TOKEN", raising=False)
    monkeypatch.delenv("PALINODE_API_TOKEN_FILE", raising=False)
    assert load_api_token() is None


# ---------------------------------------------------------------------------
# validate_auth_config
# ---------------------------------------------------------------------------


def test_validate_auth_config_rejects_public_bind_no_token():
    from palinode.api.middleware import validate_auth_config

    with pytest.raises(SystemExit, match="REFUSING TO START"):
        validate_auth_config(None, bind_intent_public=True)


def test_validate_auth_config_passes_with_token():
    from palinode.api.middleware import validate_auth_config

    # Should not raise.
    validate_auth_config("some-token", bind_intent_public=True)


def test_validate_auth_config_passes_loopback_no_token():
    from palinode.api.middleware import validate_auth_config

    # Should not raise.
    validate_auth_config(None, bind_intent_public=False)
