"""Tests for the ollama_circuit_health doctor check (#338 Phase 5).

The check reads OllamaClient.metrics() in-process and maps circuit/latency state
to a CheckResult severity: open circuit → error, p95 > 5s → warn, else info/pass.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from palinode.diagnostics.checks.ollama_health import ollama_circuit_health
from palinode.diagnostics.types import DoctorContext
from palinode.core.config import config


def _ctx():
    return DoctorContext(config=config)


def _patch_metrics(metrics: dict):
    fake = MagicMock(name="OllamaClient")
    fake.metrics.return_value = metrics
    return patch("palinode.core.ollama_client.get_ollama_client", return_value=fake)


def test_open_circuit_is_error():
    with _patch_metrics({
        "chat": {"circuit_state": "open", "p95_ms": None, "count_5m": 0, "error_rate_5m": 1.0},
    }):
        r = ollama_circuit_health(_ctx())
    assert r.severity == "error"
    assert r.passed is False
    assert "circuit OPEN" in r.message
    assert "chat" in r.message


def test_high_p95_is_degraded_warn():
    with _patch_metrics({
        "embed": {"circuit_state": "closed", "p95_ms": 7200.0, "count_5m": 12, "error_rate_5m": 0.0},
    }):
        r = ollama_circuit_health(_ctx())
    assert r.severity == "warn"
    assert r.passed is False
    assert "degraded" in r.message.lower()
    assert "7200ms" in r.message


def test_open_circuit_takes_precedence_over_degraded():
    with _patch_metrics({
        "embed": {"circuit_state": "closed", "p95_ms": 9000.0, "count_5m": 3, "error_rate_5m": 0.1},
        "chat": {"circuit_state": "open", "p95_ms": 100.0, "count_5m": 5, "error_rate_5m": 1.0},
    }):
        r = ollama_circuit_health(_ctx())
    assert r.severity == "error"
    assert "circuit OPEN" in r.message


def test_no_traffic_is_info_pass():
    with _patch_metrics({}):
        r = ollama_circuit_health(_ctx())
    assert r.passed is True
    assert r.severity == "info"
    assert "no recent ollama traffic" in r.message.lower()


def test_healthy_traffic_is_info_pass():
    with _patch_metrics({
        "embed": {"circuit_state": "closed", "p95_ms": 140.0, "count_5m": 50, "error_rate_5m": 0.0},
        "chat": {"circuit_state": "closed", "p95_ms": 4200.0, "count_5m": 8, "error_rate_5m": 0.0},
    }):
        r = ollama_circuit_health(_ctx())
    assert r.passed is True
    assert r.severity == "info"
    assert "healthy" in r.message.lower()


def test_check_is_registered_as_fast():
    from palinode.diagnostics import checks  # noqa: F401  (trigger registration)
    from palinode.diagnostics.registry import all_checks

    match = [
        (fn, tags) for fn, tags in all_checks()
        if fn.__name__ == "ollama_circuit_health"
    ]
    assert match, "ollama_circuit_health not registered"
    assert "fast" in match[0][1], "ollama_circuit_health must be a fast check"
