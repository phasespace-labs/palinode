"""Executable discovery-only contract through real API handlers and clients."""
from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
from pathlib import Path

import click
import httpx
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.cli import main
from palinode.cli._api import PalinodeAPI
from palinode.core import store
from palinode.core.auth import API_EXEMPT_PATHS, BearerAuthMiddleware
from palinode.core.config import config
from tests._store_helpers import upsert_chunks

TOKEN = "disposable-contract-token"


@pytest.fixture
def contract_store(tmp_path, monkeypatch):
    """Real files, index and history; no lifespan, watcher or external models."""
    # macOS temp directories contain /private; that component is not a leak.
    root = tmp_path / "private" / "memory"
    root.mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("PALINODE_DIR", str(root))
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.delenv("PALINODE_API_TOKEN_FILE", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(root))
    monkeypatch.setattr(config, "db_path", str(root / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.context, "project_map", {})
    monkeypatch.setattr(config.context, "auto_detect", False)
    for level in ("agent", "member", "harness", "org"):
        monkeypatch.setattr(config.scope, level, None)
    store.init_db()
    records = {
        "open": {},
        "scoped": {"scope": "project/known"},
        "private": {"visibility": "private", "scope": "project/known"},
        "restricted": {"visibility": "restricted", "access": ["member/alice"]},
    }
    for name, extra in records.items():
        path = root / "decisions" / f"{name}.md"
        path.parent.mkdir(exist_ok=True)
        meta = {"category": "decisions", "type": "Decision", "core": True, **extra}
        path.write_text(f"---\n{yaml.safe_dump(meta)}---\n{name}-contract-body\n", encoding="utf-8")
        upsert_chunks([{
            "id": name, "file_path": str(path), "section_id": None,
            "category": "decisions", "content": f"{name}-contract-body",
            "metadata": meta, "embedding": None,
            "created_at": "2026-09-15T12:00:00+00:00",
            "last_updated": "2026-09-15T12:00:00+00:00",
        }])
    for args in (
        ["init", "-q"], ["add", "decisions"],
        ["-c", "user.name=Contract Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "fixture: capture privacy contract records"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


@pytest.fixture
def protected(contract_store):
    return BearerAuthMiddleware(app, TOKEN, API_EXEMPT_PATHS)


@pytest.fixture
def client(protected):
    return TestClient(protected, headers={"Authorization": f"Bearer {TOKEN}"})


def test_list_and_recall_hide_paths_bodies_and_metadata(client):
    listing = client.get("/list").json()
    assert {row["file"] for row in listing} == {"decisions/open.md", "decisions/scoped.md"}
    result = client.post("/search", json={"query": "", "limit": 20})
    assert result.status_code == 200
    assert {Path(row["file_path"]).stem for row in result.json()} == {"open", "scoped"}
    for hidden in ("private", "restricted"):
        for payload in (json.dumps(listing), result.text):
            assert f"decisions/{hidden}.md" not in payload
            assert f"{hidden}-contract-body" not in payload
        assert all(row.get("metadata", {}).get("visibility") != hidden
                   for row in result.json())


@pytest.mark.parametrize("scope,expected", [
    ({"project": "known"}, {"open", "scoped", "private"}),
    ({"member": "alice"}, {"open", "restricted"}),
    ({"project": "unknown"}, {"open"}),
    ({}, {"open"}),
])
def test_prime_selection_hints_are_not_authenticated_identities(client, scope, expected):
    response = client.post("/context/prime", json={"mode": "scoped", "scope": scope})
    assert response.status_code == 200
    assert {Path(row["file"]).stem for row in response.json()["core_memories"]} == expected


@pytest.mark.parametrize("path", ["private", "restricted"])
@pytest.mark.parametrize("surface", ["read", "blame", "history", "trace"])
def test_known_hidden_path_is_readable(client, path, surface):
    rel = f"decisions/{path}.md"
    response = (client.get("/read", params={"file_path": rel, "meta": True})
                if surface == "read" else client.get(f"/{surface}/{rel}"))
    assert response.status_code == 200
    if surface in ("read", "blame"):
        assert f"{path}-contract-body" in response.text
    elif surface == "history":
        assert response.json()["history"]
    else:
        assert response.json()["file"] == rel


@pytest.mark.parametrize("token", [None, "wrong"])
@pytest.mark.parametrize("route,method", [("/read?file_path=decisions/private.md", "get"),
                                         ("/list", "get"), ("/lint", "post")])
def test_shared_bearer_rejects_missing_or_wrong_token(protected, token, route, method):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = getattr(TestClient(protected), method)(route, headers=headers)
    assert response.status_code == 401
    assert "private-contract-body" not in response.text


@pytest.mark.parametrize("bad_path", ["../outside.md", "/etc/passwd", "escape.md", "bad\x00.md"])
def test_read_rejects_unsafe_paths(client, contract_store, bad_path):
    outside = contract_store.parent / "outside.md"
    outside.write_text("outside-contract-secret", encoding="utf-8")
    (contract_store / "escape.md").symlink_to(outside)
    response = client.get("/read", params={"file_path": bad_path})
    assert response.status_code == (400 if "\x00" in bad_path else 403)
    assert "outside-contract-secret" not in response.text


def test_unknown_worktree_without_autodetection_has_no_project(client, contract_store):
    worktree = contract_store.parent / "unmapped-worktree"
    worktree.mkdir()
    response = client.post("/context/prime", json={"cwd": str(worktree), "mode": "scoped"})
    assert response.status_code == 200
    assert response.json()["project"] is None
    assert response.json()["scope_chain"] == []
    assert {row["file"] for row in response.json()["core_memories"]} == {"decisions/open.md"}


def test_maintenance_is_full_store_for_same_token(client):
    lint = client.post("/lint")
    assert lint.status_code == 200
    assert "decisions/private.md" in lint.text
    assert "decisions/restricted.md" in lint.text
    diff = client.get("/diff")
    assert diff.status_code == 200
    assert "private-contract-body" in diff.text


@pytest.mark.parametrize("authorized", [True, False])
def test_mcp_dispatch_preserves_discovery_and_exact_path_contract(protected, monkeypatch, authorized):
    monkeypatch.setenv("PALINODE_API_TOKEN", TOKEN if authorized else "wrong")
    monkeypatch.setattr(mcp, "_http_transport", httpx.ASGITransport(app=protected))
    monkeypatch.setattr(mcp, "_http_client", None)
    monkeypatch.setattr(mcp, "_http_client_loop", None)

    async def run():
        try:
            listing = await mcp._dispatch_tool("palinode_list", {})
            read = await mcp._dispatch_tool("palinode_read", {"file_path": "decisions/private.md"})
            blame = await mcp._dispatch_tool("palinode_blame", {"file_path": "decisions/private.md"})
            trace = await mcp._dispatch_tool("palinode_trace", {"file_path": "decisions/private.md"})
            prime = await mcp._dispatch_tool("palinode_session_init", {"project": "unknown"})
            return ["\n".join(block.text for block in result) for result in (listing, read, blame, trace, prime)]
        finally:
            if mcp._http_client:
                await mcp._http_client.aclose()

    listing, read, blame, trace, prime = asyncio.run(run())
    assert "decisions/private.md" not in listing
    if authorized:
        assert "decisions/open.md" in listing
        assert "private-contract-body" in read
        assert "private-contract-body" in blame
        assert "decisions/private.md" in trace
        assert "decisions/open.md" in prime
        assert "decisions/private.md" not in prime
        assert "decisions/scoped.md" not in prime
    else:
        for text in (listing, read, blame, trace, prime):
            assert "private-contract-body" not in text
            assert "Error" in text or "error" in text


@pytest.mark.parametrize("authorized", [True, False])
def test_cli_preserves_discovery_and_exact_path_contract(protected, monkeypatch, authorized):
    monkeypatch.setenv("PALINODE_API_TOKEN", TOKEN if authorized else "wrong")
    test_client = TestClient(protected)

    def dispatch(request):
        response = test_client.request(
            request.method, request.url.raw_path.decode("ascii"),
            content=request.content, headers=dict(request.headers),
        )
        return httpx.Response(response.status_code, content=response.content,
                              headers=response.headers, request=request)

    api = PalinodeAPI(transport=httpx.MockTransport(dispatch))
    for module in ("list_cmd", "read", "git", "trace", "prime"):
        monkeypatch.setattr(importlib.import_module(f"palinode.cli.{module}"), "api_client", api)
    runner = CliRunner()
    listing = runner.invoke(main, ["list", "--format", "json"])
    assert "decisions/private.md" not in listing.output
    if authorized:
        assert listing.exit_code == 0
        assert len(json.loads(listing.output)) == 2
    else:
        assert listing.exit_code != 0
    for command in ("read", "blame", "trace"):
        result = runner.invoke(main, [command, "decisions/private.md"])
        if authorized:
            assert result.exit_code == 0, result.output
            assert ("decisions/private.md" if command == "trace" else "private-contract-body") in result.output
        else:
            assert result.exit_code != 0
            assert "private-contract-body" not in result.output
    if authorized:
        prime = runner.invoke(main, ["prime", "--project", "unknown", "--format", "json"])
        assert prime.exit_code == 0, prime.output
        assert {r["file"] for r in json.loads(prime.output)["core_memories"]} == {"decisions/open.md"}
    api.client.close()
    test_client.close()


@pytest.mark.parametrize("dry_run", [False, True])
def test_init_discloses_boundary_before_installing_hooks(tmp_path, monkeypatch, dry_run):
    init_module = importlib.import_module("palinode.cli.init")
    observed = []

    def writer():
        click.echo("CAPTURE-HOOK-INSTALLED")
        observed.append(True)
        return "created"

    monkeypatch.setattr(init_module, "build_plan", lambda *_: [
        init_module.PlannedWrite("capture hook", tmp_path / "hook", "hook", writer),
    ])
    args = ["init", "--dir", str(tmp_path), "--no-prompts"]
    if dry_run:
        args.append("--dry-run")
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "Before your first capture" in result.output
    assert "read hidden known paths" in result.output
    assert "not encryption or user/agent authentication" in result.output
    assert "full-store maintenance" in result.output
    if dry_run:
        assert observed == []
    else:
        assert observed == [True]
        assert result.output.index("Before your first capture") < result.output.index("CAPTURE-HOOK-INSTALLED")


@pytest.fixture
def trigger_records(contract_store, monkeypatch):
    from palinode.core import embedder

    vector = [1.0] + [0.0] * 1023
    monkeypatch.setattr(embedder, "embed", lambda _: vector)
    targets = {name: f"decisions/{name}.md" for name in ("open", "scoped", "private", "restricted")}
    outside = contract_store.parent / "outside.md"
    outside.write_text("outside-contract-secret", encoding="utf-8")
    (contract_store / "escape.md").symlink_to(outside)
    targets.update({"escape": "escape.md", "missing": "missing.md",
                    "absolute": str(outside), "malformed": "bad\x00.md"})
    for name, path in targets.items():
        store.add_trigger(name, f"{name}-trigger-description", path, vector)
    return targets


def test_trigger_discovery_filters_targets_before_returning_metadata(client, trigger_records):
    response = client.post("/check-triggers", json={"query": "contract", "cooldown_bypass": True})
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {"open", "scoped"}
    for hidden in ("private", "restricted", "escape", "missing", "absolute", "malformed"):
        assert hidden not in response.text
    registry = client.get("/triggers")
    assert registry.status_code == 200
    assert {row["id"] for row in registry.json()} == set(trigger_records)
    # Only a delivered trigger records a fire; a hidden candidate matches,
    # is dropped, and keeps its cooldown and its fire_count.
    fired = {row["id"]: row["fire_count"] for row in registry.json()}
    assert fired == {name: (1 if name in {"open", "scoped"} else 0) for name in trigger_records}


def test_trigger_discovery_uses_live_frontmatter_and_requires_existing_file(
    client, trigger_records, contract_store,
):
    path = contract_store / "decisions/open.md"
    path.write_text("---\nvisibility: private\nscope: agent/other\n---\nhidden now\n", encoding="utf-8")
    (contract_store / "decisions/scoped.md").unlink()
    response = client.post("/check-triggers", json={"query": "contract", "cooldown_bypass": True})
    assert response.status_code == 200
    assert response.json() == []
    assert client.get("/read", params={"file_path": "decisions/open.md"}).status_code == 200


def test_trigger_discovery_preserves_read_extension_fallback(client, contract_store, monkeypatch):
    from palinode.core import embedder

    vector = [1.0] + [0.0] * 1023
    monkeypatch.setattr(embedder, "embed", lambda _: vector)
    store.add_trigger("extensionless", "extensionless target", "decisions/open", vector)
    response = client.post("/check-triggers", json={"query": "contract"})
    assert response.status_code == 200
    assert [row["memory_file"] for row in response.json()] == ["decisions/open"]


def test_generated_hook_cannot_inject_hidden_trigger_targets(
    protected, trigger_records, tmp_path,
):
    import os
    import shutil
    import threading
    import time

    import uvicorn

    from palinode.cli.init import USER_PROMPT_SUBMIT_HOOK_SCRIPT

    if not (shutil.which("curl") and shutil.which("jq")):
        pytest.skip("generated hook requires curl and jq")
    server = uvicorn.Server(uvicorn.Config(
        protected, host="127.0.0.1", port=0, lifespan="off", log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        script = tmp_path / "generated-hook.sh"
        script.write_text(USER_PROMPT_SUBMIT_HOOK_SCRIPT, encoding="utf-8")
        env = {
            **os.environ, "PALINODE_API_URL": f"http://127.0.0.1:{port}",
            "PALINODE_API_TOKEN": TOKEN, "PALINODE_HOOK_RECALL_TRIGGERS": "1",
            "PALINODE_HOOK_RECALL_MAX_RESULTS": "0",
        }
        result = subprocess.run(
            ["/bin/bash", str(script)],
            input=json.dumps({"prompt": "recall the contract decision", "cwd": str(tmp_path)}),
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "open-contract-body" in context
        assert "scoped-contract-body" in context
        for hidden in ("private", "restricted", "outside-contract-secret", "escape.md"):
            assert hidden not in context
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
