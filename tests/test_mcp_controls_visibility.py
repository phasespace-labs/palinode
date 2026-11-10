"""Read-only MCP visibility for the persisted capture/recall policy."""
from __future__ import annotations

import json
from typing import Any
from unittest import mock

import httpx
import pytest

import palinode.mcp as mcp


def _response(status_code: int, payload: object) -> mock.MagicMock:
    response = mock.MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


async def _status_text() -> str:
    result = await mcp._dispatch_tool("palinode_status", {})
    return result[0].text


def _healthy_status() -> dict[str, object]:
    return {"version": "test", "total_files": 1, "total_chunks": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capture_paused", "recall_paused", "expected"),
    [
        (False, False, ("Capture:        active", "Recall:         active")),
        (True, False, ("Capture:        paused", "Recall:         active")),
        (True, True, ("Capture:        paused", "Recall:         paused")),
    ],
)
async def test_status_discloses_actual_pause_flags_and_resolved_project(
    monkeypatch: pytest.MonkeyPatch,
    capture_paused: bool,
    recall_paused: bool,
    expected: tuple[str, str],
) -> None:
    policy = {
        "capture_paused": capture_paused,
        "recall_paused": recall_paused,
        "policy_version": 1,
        "provenance": "api_controls",
    }

    async def fake_get(path: str, **_: Any) -> mock.MagicMock:
        return _response(200, _healthy_status() if path == "/status" else policy)

    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setattr(mcp, "_status_project", lambda: "project/fixture-project")

    text = await _status_text()

    assert all(line in text for line in expected)
    assert "Policy:         v1; provenance: api_controls" in text
    assert "Client project: project/fixture-project (API decides recall scope)" in text
    assert "enrichment and consolidation" in text
    assert "capture sources only" in text
    assert "do not delete stored history" in text
    assert "history reads remain available" in text


@pytest.mark.asyncio
async def test_status_hides_malformed_policy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "token=must-not-appear"

    async def fake_get(path: str, **_: Any) -> mock.MagicMock:
        return _response(200, _healthy_status() if path == "/status" else {
            "capture_paused": "yes", "recall_paused": False,
            "policy_version": 1, "provenance": secret,
        })

    monkeypatch.setattr(mcp, "_get", fake_get)
    text = await _status_text()

    assert "policy response was invalid" in text
    assert secret not in text


@pytest.mark.asyncio
async def test_status_handles_unavailable_policy_without_echoing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "https://user:secret@example.test/controls?token=leak"

    async def fake_get(path: str, **_: Any) -> mock.MagicMock:
        if path == "/status":
            return _response(200, _healthy_status())
        return _response(503, {"detail": secret})

    monkeypatch.setattr(mcp, "_get", fake_get)
    text = await _status_text()

    assert "policy could not be read" in text
    assert secret not in text


@pytest.mark.asyncio
async def test_status_tool_remains_available_when_both_controls_are_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(path: str, **_: Any) -> mock.MagicMock:
        return _response(200, _healthy_status() if path == "/status" else {
            "capture_paused": True, "recall_paused": True,
            "policy_version": 1, "provenance": None,
        })

    monkeypatch.setattr(mcp, "_get", fake_get)

    text = await _status_text()

    assert "Capture controls (read-only)" in text
    assert "This status tool and history reads remain available" in text


@pytest.mark.asyncio
async def test_both_paused_uses_configured_mcp_project_not_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(path: str, **_: Any) -> mock.MagicMock:
        return _response(200, _healthy_status() if path == "/status" else {
            "capture_paused": True, "recall_paused": True,
            "policy_version": 1, "provenance": "api_controls",
        })

    monkeypatch.setattr(mcp, "_get", fake_get)
    monkeypatch.setenv("PALINODE_PROJECT", "configured-project")
    monkeypatch.setenv("CWD", "/fixture/ignored-by-configured-project")

    text = await _status_text()

    assert "Client project: project/configured-project (API decides recall scope)" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("controls_status", [200, 503])
async def test_status_states_the_servers_unverifiable_path_rule(
    monkeypatch: pytest.MonkeyPatch, controls_status: int,
) -> None:
    """The same server-owned rule the CLI prints, or "not reported"."""
    from palinode.core.disclosure import UNVERIFIABLE_PATH_POLICY, UNVERIFIABLE_PATH_POLICY_UNREPORTED

    policy = {"capture_paused": False, "recall_paused": False, "policy_version": 1, "provenance": "api_controls"}
    for status, expected in (
        ({**_healthy_status(), "unverifiable_path_policy": UNVERIFIABLE_PATH_POLICY}, UNVERIFIABLE_PATH_POLICY),
        (_healthy_status(), UNVERIFIABLE_PATH_POLICY_UNREPORTED),
    ):
        async def fake_get(path: str, _status: dict = status, **_: Any) -> mock.MagicMock:
            if path == "/status":
                return _response(200, _status)
            return _response(controls_status, policy)

        monkeypatch.setattr(mcp, "_get", fake_get)
        assert f"Unverifiable paths: {expected}." in await _status_text()
