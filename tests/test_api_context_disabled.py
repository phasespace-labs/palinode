"""The API's context setting decides recall scope across process boundaries."""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.core.context_prime import format_context_digest
from palinode.indexer import reconcile
from tests.test_cross_host_scope import _call_over_http


@pytest.fixture
def scoped_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    monkeypatch.setattr(config.auto_inject, "enabled", True)
    monkeypatch.setenv("PALINODE_PROJECT", "server-pin")
    monkeypatch.setattr("palinode.core.embedder.embed", lambda *a, **k: [1.0] + [0.0] * 1023)
    store.init_db()
    for project in ("home", "foreign"):
        path = tmp_path / "decisions" / f"{project}.md"
        path.parent.mkdir(exist_ok=True)
        meta = {"type": "Decision", "core": True, "entities": [f"project/{project}"],
                "cross_refs": ["decisions/home", "decisions/foreign"]}
        path.write_text(f"---\n{yaml.safe_dump(meta)}---\n\n{project} rule.\n")
        assert reconcile.reconcile(str(path), path.read_text()).committed
    return TestClient(app)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("source", ["project", "scope", "cwd", "pin"])
def test_prime_reports_and_applies_server_scope(scoped_memory, monkeypatch, enabled, source):
    monkeypatch.setattr(config.context, "enabled", enabled)
    if source == "pin":
        monkeypatch.setenv("PALINODE_PROJECT", "home")
        payload = {}
        basis = "environment"
    elif source == "cwd":
        monkeypatch.delenv("PALINODE_PROJECT")
        monkeypatch.setattr(config.context, "project_map", {"client": "home"})
        payload = {"cwd": "/client"}
        basis = "project_map"
    elif source == "scope":
        payload = {"scope": {"project": "home", "session": "session/client"}}
        basis = "explicit"
    else:
        payload = {"project": "project/home", "project_resolved_by": "environment"}
        basis = "environment"
    response = scoped_memory.post("/context/prime", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"] == ("project/home" if enabled else None)
    assert body["project_resolved_by"] == (basis if enabled else "disabled")
    expected_scope = (["session/client"] if source == "scope" else [])
    if enabled:
        expected_scope.append("project/home")
    assert body["scope_chain"] == expected_scope
    assert body["receipt"]["scope"] == expected_scope
    expected = {"decisions/home.md"} if enabled else {"decisions/home.md", "decisions/foreign.md"}
    assert {row["file"] for row in body["core_memories"]} == expected
    assert bool(body["recent_decisions"]) is enabled


@pytest.mark.parametrize("enabled", [False, True])
def test_resolve_applies_and_receipts_server_scope(scoped_memory, monkeypatch, enabled):
    monkeypatch.setattr(config.context, "enabled", enabled)
    response = scoped_memory.post("/resolve", json={
        "query": "foreign", "project": "project/home",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["receipt"]["scope"] == (["project/home"] if enabled else [])
    assert [row["ref"] for row in body["selected"]] == ([] if enabled else ["decisions/foreign"])


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
def test_read_uses_server_scope_for_links_and_isolation(
    scoped_memory, monkeypatch, enabled, automatic,
):
    monkeypatch.setattr(config.context, "enabled", enabled)
    response = scoped_memory.get("/read", params={
        "file_path": "decisions/foreign", "meta": True,
        "project": "project/home", "automatic": automatic,
    })
    if enabled and automatic:
        assert response.status_code == 404
        return
    assert response.status_code == 200, response.text
    expected = ["decisions/home"] if enabled else ["decisions/home", "decisions/foreign"]
    assert response.json()["frontmatter"]["cross_refs"] == expected


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("tool", ["palinode_session_init", "palinode_resolve", "palinode_read"])
def test_http_mcp_header_defers_to_real_api(scoped_memory, monkeypatch, enabled, tool):
    monkeypatch.setattr(config.context, "enabled", enabled)
    monkeypatch.setattr(mcp, "_session_init_client_name", lambda: None)
    deliveries = []

    async def post(path, json=None, **kwargs):
        response = scoped_memory.post(path, json=json)
        deliveries.append(response.json())
        return response

    async def get(path, params=None, **kwargs):
        response = scoped_memory.get(path, params=params)
        deliveries.append(response.json())
        return response

    monkeypatch.setattr(mcp, "_post", post)
    monkeypatch.setattr(mcp, "_get", get)
    args = {"palinode_session_init": {}, "palinode_resolve": {"query": "foreign"},
            "palinode_read": {"file_path": "decisions/foreign"}}[tool]
    reply = _call_over_http(tool, args, headers={"X-Palinode-Project": "home"})
    assert not reply["result"].get("isError"), reply
    text = reply["result"]["content"][0]["text"]
    body = deliveries[-1]
    if tool == "palinode_session_init":
        assert body["project"] == ("project/home" if enabled else None)
        assert body["project_resolved_by"] == ("environment" if enabled else "disabled")
        assert text == format_context_digest(body)
    elif tool == "palinode_resolve":
        assert body["receipt"]["scope"] == (["project/home"] if enabled else [])
        assert bool(body["selected"]) is not enabled
        assert text == body["text"]
    else:
        assert ("decisions/foreign" in body["frontmatter"]["cross_refs"]) is not enabled
        # Read from home, the foreign record says whose it is; with context off
        # the server applies no project, so there is nothing to label against.
        assert ("other_project" in body) is enabled
        notice = (
            "[other project: project/foreign — not project/home] "
            "Context only, not this project's decision.\n"
        )
        assert text == (notice if enabled else "") + body["content"]


@pytest.mark.parametrize("enabled", [False, True])
def test_trigger_delivery_uses_server_scope(scoped_memory, monkeypatch, enabled):
    """A client project can't scope trigger delivery when the server has context off."""
    monkeypatch.setattr(config.context, "enabled", enabled)
    store.add_trigger("foreign-rule", "the foreign home rule", "decisions/foreign.md",
                      [1.0] + [0.0] * 1023, threshold=0.5)
    response = scoped_memory.post("/check-triggers", json={
        "query": "the foreign home rule", "project": "project/home", "cooldown_bypass": True,
    })
    assert response.status_code == 200, response.text
    fired = [row["memory_file"] for row in response.json()]
    # With context on, the home project's isolation withholds a foreign target.
    assert fired == ([] if enabled else ["decisions/foreign.md"])
