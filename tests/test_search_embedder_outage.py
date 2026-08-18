"""Embedding-dependent search surfaces preserve typed outage diagnostics."""

from __future__ import annotations

import importlib
import logging
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.core.embedder as embedder_mod
import palinode.core.ollama_client as ollama_client_mod
import palinode.mcp as mcp
from palinode.api import server as srv
from palinode.api.server import app
from palinode.core.config import config
from palinode.core.embedder import EmbeddingUnavailable
from palinode.core.ollama_client import OllamaClient, RetryPolicy
from palinode.mcp import _dispatch_tool


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    srv._rate_counters.clear()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    srv._rate_counters.clear()


@pytest.fixture()
def dead_embedder(monkeypatch):
    """Use the real HTTP client against a loopback port with no listener."""
    dead_client = OllamaClient(retry_policy=RetryPolicy(retries=0))
    monkeypatch.setattr(config.embeddings.primary, "url", "http://127.0.0.1:1")
    monkeypatch.setattr(config.embeddings.primary, "connect_timeout_seconds", 1)
    monkeypatch.setattr(config.embeddings.primary, "timeout_seconds", 1)
    monkeypatch.setattr(ollama_client_mod, "_singleton", dead_client)
    # The context-size probe is a separate diagnostic request. Skip it so this
    # integration test exercises exactly the search embedding call.
    monkeypatch.setattr(embedder_mod, "_preflight_done", True)
    yield
    dead_client.close()


def _boom(text, backend="local"):
    raise EmbeddingUnavailable(
        backend="local",
        model="bge-m3",
        text_len=len(text),
        cause="connection refused",
    )


def test_search_dead_embedder_returns_typed_503_without_api_traceback(
    client, dead_embedder, caplog
):
    sensitive_query = "PRIVATE_QUERY_MUST_NOT_APPEAR_IN_DIAGNOSTICS"
    with caplog.at_level(logging.WARNING):
        res = client.post("/search", json={"query": sensitive_query})

    assert res.status_code == 503
    detail = res.json()["detail"]
    assert "Embedding backend unavailable" in detail
    assert "backend=local" in detail
    assert "model='bge-m3'" in detail
    assert "palinode doctor" in detail
    assert sensitive_query not in detail

    route_warnings = [
        record
        for record in caplog.records
        if record.name == "palinode.api"
        and "outcome=embedding_unavailable" in record.getMessage()
    ]
    assert len(route_warnings) == 1
    assert route_warnings[0].exc_info is None
    assert "cause=" not in route_warnings[0].getMessage()
    assert sensitive_query not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/dedup-suggest", {"content": "some draft content"}),
        ("/orphan-repair", {"broken_link": "missing target"}),
        ("/cluster-neighbors", {"file_path": "insights/source.md"}),
        ("/topic-coverage", {"query": "deployment policy"}),
    ],
)
def test_other_embedding_search_operations_preserve_typed_503(
    client, tmp_path, path, payload
):
    source = tmp_path / "insights" / "source.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Semantic source body.", encoding="utf-8")

    with patch("palinode.core.embedder.embed", side_effect=_boom):
        res = client.post(path, json=payload)

    assert res.status_code == 503
    assert "Embedding backend unavailable" in res.json()["detail"]
    assert "palinode doctor" in res.json()["detail"]


def test_create_trigger_preserves_typed_embedding_503(client):
    with patch("palinode.core.embedder.embed", side_effect=_boom):
        res = client.post(
            "/triggers",
            json={
                "description": "deploy the memory service",
                "memory_file": "projects/memory.md",
            },
        )

    assert res.status_code == 503
    assert "Embedding backend unavailable" in res.json()["detail"]
    assert "palinode doctor" in res.json()["detail"]


def test_check_triggers_preserves_typed_embedding_503(client):
    with patch("palinode.core.embedder.embed", side_effect=_boom):
        res = client.post(
            "/check-triggers",
            json={"query": "what should I remember about deployment?"},
        )

    assert res.status_code == 503
    assert "Embedding backend unavailable" in res.json()["detail"]
    assert "palinode doctor" in res.json()["detail"]


def test_save_keeps_200_and_index_error_with_same_dead_embedder(client, dead_embedder):
    res = client.post(
        "/save",
        json={
            "content": "Save succeeds even while semantic indexing is offline.",
            "type": "Insight",
            "slug": "dead-embedder-save-regression",
        },
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["indexed"] is False
    assert body["embedded"] is False
    assert "embedder unreachable" in body["index_error"]
    assert "retry" in body["index_error"]


def test_search_empty_query_bypasses_embedder_entirely(client):
    """Recency-only mode must not even reach the embedder — confirms the 500
    above is caused by the embed call, not by test-fixture DB absence."""
    with patch("palinode.core.embedder.embed", side_effect=_boom):
        res = client.post("/search", json={"query": ""})
    assert res.status_code == 200
    assert res.json() == []


def test_cli_search_relays_typed_503_message():
    search_cli = importlib.import_module("palinode.cli.search")
    detail = "Embedding backend unavailable — run `palinode doctor`"
    request = httpx.Request("POST", "http://palinode.test/search")
    response = httpx.Response(503, json={"detail": detail}, request=request)
    error = httpx.HTTPStatusError(
        "503 Service Unavailable", request=request, response=response
    )

    with patch.object(search_cli.api_client, "search", side_effect=error):
        result = CliRunner().invoke(search_cli.search, ["anything"])

    assert detail in result.output
    assert "Internal Server Error" not in result.output


@pytest.mark.asyncio
async def test_mcp_search_relays_typed_503_message(monkeypatch):
    detail = "Embedding backend unavailable — run `palinode doctor`"

    class UnavailableResponse:
        status_code = 503
        text = '{"detail":"Embedding backend unavailable — run `palinode doctor`"}'

    async def unavailable_post(*args, **kwargs):
        return UnavailableResponse()

    monkeypatch.setattr(mcp, "_post", unavailable_post)
    result = await _dispatch_tool("palinode_search", {"query": "anything"})

    assert detail in result[0].text
    assert "Internal Server Error" not in result[0].text
