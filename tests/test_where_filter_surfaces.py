"""``where`` and ``include_inbox`` on every surface: parity registry, CLI and MCP.

The REST behaviour is in ``test_where_filter.py``. That each surface advertises
and forwards ``where`` and ``include_inbox`` on ``list`` (one ``where`` query
parameter per entry, ``include_inbox=true``) is generated from the list spec in
``test_operation_spec.py`` (ADR-022). This file pins what the spec cannot:

* ``search`` registers no ``include_inbox``;
* a malformed ``where`` entry is a click usage error (exit code 2) on the CLI,
  and an ``Error:`` reply on ``palinode_list``, both before any request is made.
  Search's are in ``test_search_where_surfaces.py``;
* the CLI against the real app finds an inbox ActionItem only with the flag.

Tests marked *red* fail on v0.22.0 for the reason in their docstring; the rest
pass on v0.22.0 and must keep passing. No test reaches the network: requests are
captured by ``httpx.MockTransport``, or served in-process by the real app.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from palinode import mcp
from palinode.api import server
from palinode.cli import main
from palinode.cli._api import api_client
from palinode.core import embedder, parity, store
from palinode.core.config import config
from palinode.indexer import reconcile


# ── helpers ─────────────────────────────────────────────────────────────────

class Recorder:
    """Captures every request and answers each with an empty JSON list."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=[])

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        return self.handle(request)

    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture()
def cli_recorder(monkeypatch) -> Recorder:
    recorder = Recorder()
    client = httpx.Client(
        transport=httpx.MockTransport(recorder.handle), base_url="http://testserver"
    )
    monkeypatch.setattr(api_client, "client", client)
    yield recorder
    client.close()


@pytest.fixture()
def mcp_recorder(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(mcp, "_http_transport", httpx.MockTransport(recorder.handle_async))
    monkeypatch.setattr(mcp, "_http_client", None)
    monkeypatch.setattr(mcp, "_http_client_loop", None)
    return recorder


def _call_tool(name: str, arguments: dict) -> str:
    return asyncio.run(mcp._dispatch_tool(name, arguments))[0].text


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args))


# ── parity registry ─────────────────────────────────────────────────────────

def _param(operation: str, name: str):
    return {p.name: p for p in parity.by_name(operation).canonical_params}.get(name)


def test_search_does_not_register_include_inbox():
    """Search already reaches the inbox; the flag is the listing's."""
    assert _param("search", "include_inbox") is None


# ── CLI: a malformed entry ──────────────────────────────────────────────


@pytest.mark.parametrize("entry", ["no-equals", "=value", "bad key=x", "1abc=x", "status="])
def test_cli_malformed_where_is_a_usage_error_before_any_request(cli_recorder, entry):
    """Red: the output never names the entry. On v0.22.0 click answers with
    ``No such option: --where``, which is also exit code 2."""
    result = _invoke("list", "--where", entry)

    assert result.exit_code == 2, result.output
    assert entry in result.output
    assert cli_recorder.requests == []


# ── CLI against the real app ────────────────────────────────────────────────

def _cli_http_client(test_client: TestClient) -> httpx.Client:
    """An ``httpx.Client`` whose transport dispatches to the in-process app (the
    CLI only ever talks to ``httpx``; see ``test_alias_curation.py``)."""

    def handler(request: httpx.Request) -> httpx.Response:
        reply = test_client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(
            status_code=reply.status_code,
            headers=reply.headers.multi_items(),
            content=reply.content,
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")


@pytest.fixture()
def real_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    monkeypatch.setattr(config.git, "auto_commit", False)
    no_model = Mock(side_effect=AssertionError("a model was contacted"))
    monkeypatch.setattr(embedder, "embed", no_model)
    monkeypatch.setattr(embedder, "embed_many", no_model)
    monkeypatch.setattr(reconcile, "get_ollama_client", no_model)
    store.init_db()
    server._rate_counters.clear()
    with TestClient(server.app, raise_server_exceptions=False) as test_client:
        monkeypatch.setattr(api_client, "client", _cli_http_client(test_client))
        for slug, status in (("open-item", "open"), ("resolved-item", "resolved")):
            saved = test_client.post("/save", json={
                "content": f"harborlight {slug}", "type": "ActionItem", "slug": slug,
                "metadata": {"status": status},
            })
            assert saved.status_code == 200, saved.text
        yield test_client
    server._rate_counters.clear()


def test_cli_list_finds_the_open_actionitem_end_to_end(real_app):
    """Red: ``--where`` and ``--include-inbox`` are unknown options."""
    result = _invoke(
        "list", "--where", "status=open", "--include-inbox", "--format", "json"
    )

    assert result.exit_code == 0, result.output
    assert [row["file"] for row in json.loads(result.output)] == ["inbox/open-item.md"]


def test_cli_list_without_include_inbox_shows_no_actionitem_end_to_end(real_app):
    result = _invoke("list", "--format", "json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


# ── MCP: a malformed entry ──────────────────────────────────────────────


@pytest.mark.parametrize("entry", ["no-equals", "=value", "bad key=x", "1abc=x", "status="])
def test_mcp_malformed_where_is_an_error_reply_before_any_request(mcp_recorder, entry):
    """Red: the entry is ignored and the call goes to the API."""
    text = _call_tool("palinode_list", {"where": [entry]})

    assert text.startswith("Error:")
    assert entry in text
    assert mcp_recorder.requests == []
