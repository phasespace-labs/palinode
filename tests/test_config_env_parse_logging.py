"""Tests for #337 — malformed env-var overrides must warn, not fall back silently.

`load_config()` parses two numeric env overrides (`PALINODE_API_PORT`,
`PALINODE_DESCRIBE_TIMEOUT_SECONDS`). Before #337 a malformed value was caught
by `except ValueError: pass`, silently leaving the operator on the default —
a "the system did the wrong thing silently" case per docs/logging.md. Each now
emits one WARNING naming the variable and the rejected value.
"""
from __future__ import annotations

import logging

import pytest

from palinode.core.config import load_config


def test_malformed_api_port_warns(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
    monkeypatch.setenv("PALINODE_API_PORT", "not-a-number")
    with caplog.at_level(logging.WARNING, logger="palinode.config"):
        load_config()
    matches = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "PALINODE_API_PORT" in r.getMessage()
    ]
    assert matches, "malformed PALINODE_API_PORT should emit a WARNING"
    assert "not-a-number" in matches[0].getMessage()


def test_malformed_describe_timeout_warns(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
    monkeypatch.setenv("PALINODE_DESCRIBE_TIMEOUT_SECONDS", "soon")
    with caplog.at_level(logging.WARNING, logger="palinode.config"):
        load_config()
    matches = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "PALINODE_DESCRIBE_TIMEOUT_SECONDS" in r.getMessage()
    ]
    assert matches, "malformed PALINODE_DESCRIBE_TIMEOUT_SECONDS should emit a WARNING"
    assert "soon" in matches[0].getMessage()


def test_valid_api_port_does_not_warn(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
    monkeypatch.setenv("PALINODE_API_PORT", "6399")
    with caplog.at_level(logging.WARNING, logger="palinode.config"):
        load_config()
    assert not any(
        "PALINODE_API_PORT" in r.getMessage() for r in caplog.records
    ), "a valid port must not warn"
