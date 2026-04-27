"""
Tests for GET /doctor API endpoint.

Covers:
  - Basic GET /doctor returns 200 + valid JSON shape
  - ?fast=true filters to fast-tagged checks only (no deep checks in result)
  - ?canary=true is accepted without error (no canary checks exist yet)
  - JSON shape: {results: [...], summary: {total, passed, failed}, params: {...}}
  - Each result entry has the expected fields

Uses TestClient lifespan context-manager pattern (project standard: real
tmp_path DB, no SQLite mocking).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.config import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    """TestClient backed by a fresh tmp_path directory.

    Uses TestClient as a context manager so the lifespan startup fires and
    store.init_db() creates the schema.  Patches config so the doctor checks
    see the tmp_path as the configured memory_dir.
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.doctor, "search_roots", [str(tmp_path)])
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_RESULT_FIELDS = {"name", "severity", "passed", "message", "remediation"}
_SUMMARY_FIELDS = {"total", "passed", "failed"}


def _assert_valid_shape(body: dict) -> None:
    """Assert the top-level /doctor response has the expected shape."""
    assert "results" in body, "missing 'results' key"
    assert "summary" in body, "missing 'summary' key"
    assert isinstance(body["results"], list), "'results' must be a list"

    summary = body["summary"]
    for field in _SUMMARY_FIELDS:
        assert field in summary, f"summary missing '{field}'"

    assert summary["total"] == len(body["results"])
    assert summary["passed"] + summary["failed"] == summary["total"]

    for entry in body["results"]:
        for field in _RESULT_FIELDS:
            assert field in entry, f"result entry missing '{field}'"
        assert isinstance(entry["passed"], bool)
        assert entry["severity"] in ("info", "warn", "error", "critical")


# ---------------------------------------------------------------------------
# Basic / default run
# ---------------------------------------------------------------------------


class TestDoctorDefault:
    def test_returns_200(self, client) -> None:
        resp = client.get("/doctor")
        assert resp.status_code == 200

    def test_response_is_json(self, client) -> None:
        resp = client.get("/doctor")
        assert resp.headers["content-type"].startswith("application/json")

    def test_valid_json_shape(self, client) -> None:
        resp = client.get("/doctor")
        body = resp.json()
        _assert_valid_shape(body)

    def test_results_non_empty(self, client) -> None:
        resp = client.get("/doctor")
        body = resp.json()
        assert len(body["results"]) > 0

    def test_summary_counts_correct(self, client) -> None:
        resp = client.get("/doctor")
        body = resp.json()
        results = body["results"]
        expected_passed = sum(1 for r in results if r["passed"])
        expected_failed = sum(1 for r in results if not r["passed"])
        assert body["summary"]["passed"] == expected_passed
        assert body["summary"]["failed"] == expected_failed


# ---------------------------------------------------------------------------
# ?fast=true
# ---------------------------------------------------------------------------


class TestDoctorFast:
    def test_fast_returns_200(self, client) -> None:
        resp = client.get("/doctor", params={"fast": "true"})
        assert resp.status_code == 200

    def test_fast_valid_shape(self, client) -> None:
        resp = client.get("/doctor", params={"fast": "true"})
        body = resp.json()
        _assert_valid_shape(body)

    def test_fast_fewer_checks_than_full(self, client) -> None:
        """fast=true should run a subset of the full check list."""
        full = client.get("/doctor").json()
        fast = client.get("/doctor", params={"fast": "true"}).json()
        # Fast subset must be strictly smaller (we have deep checks registered)
        assert fast["summary"]["total"] < full["summary"]["total"]

    def test_fast_excludes_deep_checks(self, client) -> None:
        """Network-probe checks (api_reachable, watcher_alive, etc.) must not appear."""
        resp = client.get("/doctor", params={"fast": "true"})
        body = resp.json()
        deep_check_names = {
            "api_reachable", "api_status_consistent",
            "watcher_alive", "watcher_indexes_correct_db",
            "phantom_db_files",
        }
        result_names = {r["name"] for r in body["results"]}
        assert result_names.isdisjoint(deep_check_names), (
            f"Fast run should not contain deep checks, but found: "
            f"{result_names & deep_check_names}"
        )

    def test_fast_params_flag_set(self, client) -> None:
        resp = client.get("/doctor", params={"fast": "true"})
        body = resp.json()
        assert body["params"]["fast"] is True


# ---------------------------------------------------------------------------
# ?canary=true
# ---------------------------------------------------------------------------


class TestDoctorCanary:
    def test_canary_returns_200(self, client) -> None:
        """?canary=true must be accepted without error."""
        resp = client.get("/doctor", params={"canary": "true"})
        assert resp.status_code == 200

    def test_canary_valid_shape(self, client) -> None:
        resp = client.get("/doctor", params={"canary": "true"})
        body = resp.json()
        _assert_valid_shape(body)

    def test_canary_params_flag_set(self, client) -> None:
        resp = client.get("/doctor", params={"canary": "true"})
        body = resp.json()
        assert body["params"]["canary"] is True

    def test_canary_result_count_same_as_full(self, client) -> None:
        """No canary checks exist yet, so result count equals full run."""
        full = client.get("/doctor").json()
        canary = client.get("/doctor", params={"canary": "true"}).json()
        assert canary["summary"]["total"] == full["summary"]["total"]


# ---------------------------------------------------------------------------
# Result entry shape
# ---------------------------------------------------------------------------


class TestDoctorResultShape:
    def test_each_result_has_required_fields(self, client) -> None:
        resp = client.get("/doctor")
        for entry in resp.json()["results"]:
            for field in _RESULT_FIELDS:
                assert field in entry, f"missing field '{field}' in {entry}"

    def test_passed_is_bool(self, client) -> None:
        resp = client.get("/doctor")
        for entry in resp.json()["results"]:
            assert isinstance(entry["passed"], bool), (
                f"'passed' must be bool, got {type(entry['passed'])} in {entry}"
            )

    def test_severity_valid(self, client) -> None:
        resp = client.get("/doctor")
        for entry in resp.json()["results"]:
            assert entry["severity"] in ("info", "warn", "error", "critical"), (
                f"unexpected severity {entry['severity']!r}"
            )
