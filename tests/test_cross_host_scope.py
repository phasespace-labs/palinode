"""Cross-host recall carries the client's scope, never the server's checkout.

The recommended remote install puts the client and the server on different
machines. The server process then runs from a directory of its own, often a
git checkout of Palinode itself, and nothing about that directory describes
the client. These tests stand the server in exactly that position: its process
directory (and the ``CWD`` hint) is a real git repository named differently
from every client project, and they assert that:

- a hook-shaped ``POST /resolve`` carrying the client's ``cwd`` or ``project``
  is scoped to the client's project;
- the same request carrying neither is unscoped, and labelled so, never the
  server's repository;
- the MCP server over streamable HTTP ignores its own directory, and honours
  the ``X-Palinode-Project`` header an HTTP client carries its project in;
- the shipped hooks send the client's scope on the requests that need it.

Real SQLite, real git, real files under ``tmp_path``; the memory dir, database
and log paths are all redirected there by the suite's autouse fixtures.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.cli.init import SESSION_START_HOOK_SCRIPT, USER_PROMPT_SUBMIT_HOOK_SCRIPT
from palinode.core import store
from palinode.core.config import config

SERVER_REPO = "palinode-server-checkout"
CLIENT_PROJECT = "quillon-client"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def server_in_its_own_repo(tmp_path, monkeypatch):
    """The server process's directory: a git checkout that is no client's project."""
    repo = tmp_path / "srv" / SERVER_REPO
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", f"git@example.com:team/{SERVER_REPO}.git")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CWD", str(repo))
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config.context, "enabled", True)
    monkeypatch.setattr(config.context, "auto_detect", True)
    monkeypatch.setattr(config.context, "project_map", {})
    # Precondition: the server's own directory WOULD resolve to its checkout if
    # anything consulted it. That is the failure being guarded against.
    from palinode.core.context_prime import resolve_context

    assert resolve_context(cwd=str(repo)).project == f"project/{SERVER_REPO}"
    return repo


@pytest.fixture
def memory(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    mem.mkdir()
    _git(mem, "init", "-q")
    monkeypatch.setattr(config, "memory_dir", str(mem))
    monkeypatch.setattr(config, "db_path", str(mem / ".palinode.db"))
    store.init_db()
    # No network embedder in a unit test: a fixed unit vector keeps query
    # seeding deterministic and offline. Scope, not ranking, is under test.
    monkeypatch.setattr("palinode.core.embedder.embed",
                        lambda text, *a, **k: [1.0] + [0.0] * 1023)
    return mem


def _write(mem: Path, rel: str, body: str, **meta) -> None:
    path = mem / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.safe_dump(meta)}---\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def client_dir(tmp_path):
    """The client's working directory, on the client's machine."""
    path = tmp_path / "client-host" / CLIENT_PROJECT
    path.mkdir(parents=True)
    return path


def _scope_of(body: dict) -> list[str]:
    return list(body["receipt"]["scope"])


# ── POST /resolve, hook-shaped ─────────────────────────────────────────────


def test_hook_resolve_with_client_cwd_scopes_to_the_client(
    server_in_its_own_repo, memory, client_dir,
):
    response = TestClient(app).post("/resolve", json={
        "query": "Which cache does quillon use?", "cwd": str(client_dir),
        "max_items": 3, "max_chars": 2800,
    })
    assert response.status_code == 200, response.text
    assert _scope_of(response.json()) == [f"project/{CLIENT_PROJECT}"]
    assert SERVER_REPO not in response.text


def test_hook_resolve_explicit_project_beats_client_cwd(
    server_in_its_own_repo, memory, client_dir,
):
    response = TestClient(app).post("/resolve", json={
        "query": "Which cache?", "cwd": str(client_dir), "project": "harbor-notes",
    })
    assert response.status_code == 200, response.text
    assert _scope_of(response.json()) == ["project/harbor-notes"]


def test_hook_resolve_with_no_client_scope_is_unscoped_not_the_servers_repo(
    server_in_its_own_repo, memory,
):
    response = TestClient(app).post("/resolve", json={"query": "Which cache?"})
    assert response.status_code == 200, response.text
    assert _scope_of(response.json()) == [], "no client scope is labelled unscoped"
    assert SERVER_REPO not in response.text


def test_operator_pinned_project_still_applies_to_a_scopeless_request(
    server_in_its_own_repo, memory, monkeypatch,
):
    """PALINODE_PROJECT on the server is a deliberate operator choice."""
    monkeypatch.setenv("PALINODE_PROJECT", "pinned-by-operator")
    response = TestClient(app).post("/resolve", json={"query": "Which cache?"})
    assert _scope_of(response.json()) == ["project/pinned-by-operator"]


def test_client_scope_gates_explicitly_scoped_records(
    server_in_its_own_repo, memory, client_dir,
):
    """The scope carried is the scope applied: another project's record stays out."""
    _write(memory, "decisions/client-cache.md", "The client uses Redis.",
           type="Decision", scope=f"project/{CLIENT_PROJECT}")
    _write(memory, "decisions/other-cache.md", "The other project uses Memcached.",
           type="Decision", scope="project/quillon-8b4286")
    api = TestClient(app)

    def refs(ref: str) -> list[str]:
        body = api.post("/resolve", json={"ref": ref, "cwd": str(client_dir)}).json()
        return [s["ref"] for s in body["selected"]]

    assert refs("decisions/client-cache") == ["decisions/client-cache"]
    assert refs("decisions/other-cache") == []


def test_an_unusable_client_project_is_refused_not_replaced(server_in_its_own_repo, memory):
    response = TestClient(app).post("/resolve", json={"query": "q", "project": "../escape"})
    assert response.status_code == 400
    assert "../escape" not in response.text


# ── MCP over streamable HTTP ───────────────────────────────────────────────


class _RecordingAPI:
    status_code = 200

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post(self, path, json=None, **kwargs):
        self.calls.append((path, json))
        return self

    def json(self):
        return {"results": [], "receipt": {"bundle_id": "b1", "evaluated_at": "t1"},
                "text": "", "project": None, "project_resolved_by": "none"}


def _sse_payload(response) -> dict:
    """The JSON-RPC message of a streamable-HTTP response (JSON or one SSE event)."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1])


def _call_over_http(tool: str, arguments: dict, headers: dict[str, str] | None = None) -> dict:
    """One real initialize + tools/call through the HTTP app's transport."""
    base = {"Accept": "application/json, text/event-stream",
            "Content-Type": "application/json", **(headers or {})}
    with TestClient(mcp._build_mcp_http_app(None)) as http:
        init = http.post("/mcp/", headers=base, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "cross-host-test", "version": "0"}},
        })
        assert init.status_code == 200, init.text
        session = {**base, "Mcp-Protocol-Version": "2025-06-18"}
        if init.headers.get("mcp-session-id"):
            session["Mcp-Session-Id"] = init.headers["mcp-session-id"]
        http.post("/mcp/", headers=session,
                  json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        called = http.post("/mcp/", headers=session, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        assert called.status_code == 200, called.text
        return _sse_payload(called)


def test_http_mcp_search_never_scopes_to_the_servers_own_checkout(
    server_in_its_own_repo, monkeypatch,
):
    api = _RecordingAPI()
    monkeypatch.setattr(mcp, "_post", api.post)
    reply = _call_over_http("palinode_search", {"query": "Which cache?"})
    text = reply["result"]["content"][0]["text"]
    assert api.calls[0][1]["context"] == [], "stated unscoped, not left to the server"
    assert text.startswith("Scope: none (none)")
    assert SERVER_REPO not in text


def test_http_mcp_carries_the_client_project_header(server_in_its_own_repo, monkeypatch):
    api = _RecordingAPI()
    monkeypatch.setattr(mcp, "_post", api.post)
    reply = _call_over_http("palinode_search", {"query": "Which cache?"},
                            headers={"X-Palinode-Project": CLIENT_PROJECT})
    assert api.calls[0][1]["context"] == [f"project/{CLIENT_PROJECT}"]
    assert reply["result"]["content"][0]["text"].startswith(
        f"Scope: project/{CLIENT_PROJECT} (environment)")

    _call_over_http("palinode_resolve", {"query": "Which cache?"},
                    headers={"X-Palinode-Project": CLIENT_PROJECT})
    assert api.calls[-1] == ("/resolve", {"query": "Which cache?",
                                          "project": f"project/{CLIENT_PROJECT}"})


def test_http_mcp_rejects_an_unusable_project_header(server_in_its_own_repo, monkeypatch):
    api = _RecordingAPI()
    monkeypatch.setattr(mcp, "_post", api.post)
    reply = _call_over_http("palinode_search", {"query": "q"},
                            headers={"X-Palinode-Project": "../escape"})
    assert reply["result"]["isError"] is True
    assert "X-Palinode-Project" in reply["result"]["content"][0]["text"]
    assert api.calls == []


def test_http_mcp_session_init_sends_no_server_directory(server_in_its_own_repo, monkeypatch):
    api = _RecordingAPI()
    monkeypatch.setattr(mcp, "_post", api.post)
    monkeypatch.setattr(config.auto_inject, "enabled", True)
    monkeypatch.setattr(config.auto_inject, "harnesses_disabled", [])
    _call_over_http("palinode_session_init", {})
    assert api.calls[0] == ("/context/prime", {})

    _call_over_http("palinode_session_init", {},
                    headers={"X-Palinode-Project": CLIENT_PROJECT})
    assert api.calls[-1] == ("/context/prime", {
        "project": f"project/{CLIENT_PROJECT}", "project_resolved_by": "environment",
    })


@pytest.mark.asyncio
async def test_stdio_mcp_still_resolves_the_local_client_directory(
    server_in_its_own_repo, monkeypatch,
):
    """Over stdio the process runs on the client's machine: its directory counts."""
    api = _RecordingAPI()
    monkeypatch.setattr(mcp, "_post", api.post)
    await mcp._dispatch_tool("palinode_search", {"query": "Which cache?"})
    assert api.calls[0][1]["context"] == [f"project/{SERVER_REPO}"]


# ── the shipped hooks send the client's scope ──────────────────────────────


def _run_hook(script: str, tmp_path: Path, payload: dict, env: dict[str, str]) -> list[dict]:
    """Run a hook under bash with a stub curl; return every JSON body it sent."""
    hook = tmp_path / "hook.sh"
    hook.write_text(script)
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "curl").write_text(
        '#!/bin/bash\n'
        'body=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-d" ] && body="$a"; prev="$a"; done\n'
        'url="${@: -1}"; for a in "$@"; do case "$a" in http*) url="$a";; esac; done\n'
        'printf "%s\\t%s\\n" "$url" "$(printf %s "$body" | tr -d "\\n")" >> "$STUB_DIR/sent"\n'
        'case "$url" in\n'
        '  */controls/check) echo \'{"allowed":true}\' ;;\n'
        '  */resolve) echo \'{"selected":[],"conflicts":[],"replaced":[],'
        '"insufficient":[],"text":""}\' ;;\n'
        '  *) echo \'[]\' ;;\n'
        'esac\n'
    )
    (stub / "curl").chmod(0o755)
    subprocess.run(
        ["/bin/bash", str(hook)], input=json.dumps(payload), capture_output=True, text=True,
        env={"PATH": f"{stub}:/usr/bin:/bin", "STUB_DIR": str(stub), "HOME": str(tmp_path),
             **env},
        check=True,
    )
    sent = []
    for line in (stub / "sent").read_text().splitlines():
        url, _, body = line.partition("\t")
        sent.append({"url": url, "body": json.loads(body) if body else None})
    return sent


def _bodies(sent: list[dict], suffix: str) -> list[dict]:
    return [s["body"] for s in sent if s["url"].endswith(suffix)]


@pytest.mark.parametrize("env,expected", [
    ({}, {}),
    ({"PALINODE_PROJECT": CLIENT_PROJECT}, {"project": CLIENT_PROJECT}),
    ({"PALINODE_PROJECT": f"project/{CLIENT_PROJECT}"}, {"project": CLIENT_PROJECT}),
])
def test_prompt_hook_sends_the_client_scope_to_resolve(tmp_path, env, expected):
    cwd = str(tmp_path / CLIENT_PROJECT)
    sent = _run_hook(USER_PROMPT_SUBMIT_HOOK_SCRIPT, tmp_path,
                     {"prompt": "Which cache does quillon use?", "cwd": cwd}, env)
    scope = {"cwd": cwd, **expected}
    (resolve,) = _bodies(sent, "/resolve")
    assert {k: resolve[k] for k in ("cwd", "project") if k in resolve} == scope
    assert all({k: c[k] for k in ("cwd", "project") if k in c} == scope
               for c in _bodies(sent, "/controls/check"))


@pytest.mark.parametrize("env,expected", [
    ({}, {}),
    ({"PALINODE_PROJECT": CLIENT_PROJECT}, {"project": CLIENT_PROJECT}),
])
def test_session_start_hook_primes_the_client_scope(tmp_path, env, expected):
    cwd = str(tmp_path / CLIENT_PROJECT)
    sent = _run_hook(SESSION_START_HOOK_SCRIPT, tmp_path,
                     {"session_id": "s1", "cwd": cwd, "source": "startup"}, env)
    (prime,) = _bodies(sent, "/context/prime")
    assert prime == {"cwd": cwd, "session_id": "s1", **expected}
