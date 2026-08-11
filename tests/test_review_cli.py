"""CLI-surface tests for ``palinode review`` (issue #114).

``review`` must follow the two CLI conventions the rest of the commands honour:

* **TTY-aware output.** Human-readable when interactive, JSON when piped —
  ``palinode/cli/_format.py`` centralises this in ``get_default_format()``.
  ``CliRunner`` gives a non-TTY stdout, so the piped default must be JSON.
* **Non-zero exit on API failure.** An ``HTTPStatusError`` from the API is a
  failure; a script running ``palinode review`` in CI must be able to tell a
  clean review from a 500.

The ``RequestError`` path is a deliberate local-in-process fallback (the API
being down is not an error), so it is intentionally not exercised here.
"""
from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from unittest.mock import patch

from palinode.cli import main as cli_main


SAMPLE_REVIEW = {
    "project": "project/alpha",
    "summary": {"scope_file_count": 3, "finding_count": 1, "proposed_op_count": 1},
    "findings": {"stale": [{"file": "insights/old.md", "days_old": 200}]},
    "proposed_ops": [{"op": "PROPOSE_ARCHIVE", "file": "insights/old.md", "reason": "stale"}],
    "hints": [],
}


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an ``HTTPStatusError`` whose ``.response.status_code`` is set,
    matching what ``review`` reads on the failure path."""
    request = httpx.Request("POST", "http://testserver/review")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"{status_code} Server Error", request=request, response=response
    )


def test_review_piped_default_is_json():
    """With no --format and a non-TTY stdout, output is machine-readable JSON."""
    with patch("palinode.cli._api.api_client.review", return_value=SAMPLE_REVIEW):
        result = CliRunner().invoke(cli_main, ["review", "alpha"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == SAMPLE_REVIEW


def test_review_explicit_json_matches_default():
    """--format json is identical to the piped default."""
    with patch("palinode.cli._api.api_client.review", return_value=SAMPLE_REVIEW):
        result = CliRunner().invoke(cli_main, ["review", "alpha", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == SAMPLE_REVIEW


def test_review_explicit_text_is_not_json():
    """--format text renders the human report, which is not valid JSON."""
    with patch("palinode.cli._api.api_client.review", return_value=SAMPLE_REVIEW):
        result = CliRunner().invoke(cli_main, ["review", "alpha", "--format", "text"])

    assert result.exit_code == 0, result.output
    assert "Palinode Memory Review" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_review_api_failure_exits_nonzero():
    """An HTTPStatusError from the API must produce a non-zero exit code."""
    with patch(
        "palinode.cli._api.api_client.review",
        side_effect=_http_status_error(500),
    ):
        result = CliRunner().invoke(cli_main, ["review", "alpha"])

    assert result.exit_code != 0
    assert "500" in result.output
