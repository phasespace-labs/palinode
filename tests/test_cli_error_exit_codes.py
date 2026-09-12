"""CLI error paths must exit non-zero (https://github.com/phasespace-labs/palinode/issues/164)."""

import importlib
from unittest.mock import patch

import httpx
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli._api import HTTPStatusError, RequestError

# importlib: cli/__init__.py re-exports Commands that shadow same-named modules.
depends_mod = importlib.import_module("palinode.cli.depends")
git_mod = importlib.import_module("palinode.cli.git")
ingest_mod = importlib.import_module("palinode.cli.ingest")
lint_mod = importlib.import_module("palinode.cli.lint")
manage_mod = importlib.import_module("palinode.cli.manage")
prime_mod = importlib.import_module("palinode.cli.prime")
query_mod = importlib.import_module("palinode.cli.query")
trace_mod = importlib.import_module("palinode.cli.trace")


def _http_status_error(status_code: int = 500) -> HTTPStatusError:
    request = httpx.Request("GET", "http://localhost/test")
    response = httpx.Response(status_code, request=request)
    return HTTPStatusError("error", request=request, response=response)


def test_depends_unblocked_error_exits_nonzero():
    with patch.object(
        depends_mod.api_client, "depends_unblocked", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["depends", "--unblocked"])
    assert result.exit_code != 0
    assert "Error fetching unblocked items" in result.output


def test_depends_slug_error_exits_nonzero():
    with patch.object(
        depends_mod.api_client, "depends", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["depends", "milestone/M1"])
    assert result.exit_code != 0
    assert "Error:" in result.output


def test_lint_api_status_error_exits_nonzero():
    with patch.object(
        lint_mod.api_client, "lint", side_effect=_http_status_error(503)
    ):
        result = CliRunner().invoke(main, ["lint"])
    assert result.exit_code != 0
    assert "API returned 503" in result.output


def test_trace_error_exits_nonzero():
    with patch.object(
        trace_mod.api_client, "trace", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["trace", "decisions/x.md"])
    assert result.exit_code != 0
    assert "Error tracing" in result.output


def test_blame_error_exits_nonzero():
    with patch.object(
        git_mod.api_client, "blame", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["blame", "core.md"])
    assert result.exit_code != 0
    assert "Error blaming" in result.output


def test_history_error_exits_nonzero():
    with patch.object(
        git_mod.api_client, "get_history", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["history", "core.md"])
    assert result.exit_code != 0
    assert "Error showing history" in result.output


def test_push_error_exits_nonzero():
    with patch.object(
        git_mod.api_client, "push", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["push"])
    assert result.exit_code != 0
    assert "Error pushing" in result.output


def test_reindex_error_exits_nonzero():
    with patch.object(
        manage_mod.api_client, "reindex", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["reindex"])
    assert result.exit_code != 0
    assert "Error reindexing" in result.output


def test_reindex_already_running_stays_zero():
    """409 'already running' is informational, not a hard failure."""
    with patch.object(
        manage_mod.api_client, "reindex", side_effect=_http_status_error(409)
    ):
        result = CliRunner().invoke(main, ["reindex"])
    assert result.exit_code == 0
    assert "already running" in result.output


def test_ingest_unreachable_api_exits_nonzero():
    with patch.object(
        ingest_mod.api_client,
        "ingest_url",
        side_effect=RequestError("connection refused"),
    ):
        result = CliRunner().invoke(main, ["ingest", "--url", "https://example.com"])
    assert result.exit_code != 0
    assert "Cannot reach API" in result.output


def test_prime_error_exits_nonzero():
    with patch.object(
        prime_mod.api_client, "context_prime", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["prime"])
    assert result.exit_code != 0
    assert "Error priming context" in result.output


def test_entities_error_exits_nonzero():
    with patch.object(
        query_mod.api_client, "get_entities", side_effect=RuntimeError("boom")
    ):
        result = CliRunner().invoke(main, ["entities"])
    assert result.exit_code != 0
    assert "Error showing entities" in result.output


def test_config_edit_missing_file_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PALINODE_CONFIG", raising=False)
    monkeypatch.setattr(
        "palinode.core.config.config.memory_dir",
        str(tmp_path / "missing-memory"),
    )
    result = CliRunner().invoke(main, ["config", "edit"])
    assert result.exit_code != 0
    assert "Config file not found" in result.output
