"""#664 — on-demand ARCHIVE / SUPERSEDE for one named memory.

Covers the entry point into the existing archival machinery: the frontmatter
flip shared with the TTL sweep, the executor's ``{base}-history.md`` audit
sibling, the chunk-index status push that takes the memory out of default
recall, and the one-mutation-one-commit discipline — plus the REST, CLI and MCP
surfaces and their rejection cases (traversal, missing file, already archived).

Real SQLite + real git in ``tmp_path``, no DB mocking (repo rule). Only the
embedder and the security scanner are patched.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.consolidation import archive as archive_mod
from palinode.core.config import config

EMBED_DIM = 1024
_FAKE_VECTOR = [0.05] * EMBED_DIM


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    return list(_FAKE_VECTOR)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """Git-backed tmp memory_dir with real SQLite and a fake embedder."""
    from palinode.core import store

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", os.path.join(str(tmp_path), ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    for d in ("insights", "projects", "decisions"):
        os.makedirs(os.path.join(str(tmp_path), d), exist_ok=True)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_fake_embed):
        yield str(tmp_path)


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient over the same git-backed tmp memory_dir."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=list(_FAKE_VECTOR)),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as c:
            yield c, str(tmp_path)
    srv._rate_counters.clear()


def _write_and_index(memory_dir: str, relpath: str, body: str, frontmatter: dict) -> str:
    from palinode.indexer.index_file import index_file

    path = os.path.join(memory_dir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = yaml.safe_dump(frontmatter, default_flow_style=False)
    with open(path, "w") as f:
        f.write(f"---\n{fm}---\n\n{body}\n")
    index_file(path)
    return path


def _meta(path: str) -> dict[str, Any]:
    from palinode.core import parser

    with open(path) as f:
        meta, _ = parser.parse_markdown(f.read())
    return meta


def _git(memory_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", memory_dir, *args], check=True, capture_output=True, text=True
    ).stdout


# ── core: ARCHIVE ─────────────────────────────────────────────────────────────


def test_archive_sets_status_and_writes_history_sibling(store_env):
    _write_and_index(
        store_env, "insights/claim-verification.md", "a wrongly-named synthesis",
        {"id": "insights-claim-verification", "category": "insights"},
    )

    result = archive_mod.archive_memory(
        "insights/claim-verification.md", reason="misnamed by an early promote"
    )

    assert result["status"] == "archived"
    assert result["file"] == "insights/claim-verification.md"
    assert result["history_file"] == "insights/claim-verification-history.md"

    assert _meta(os.path.join(store_env, "insights/claim-verification.md"))["status"] == "archived"

    history = os.path.join(store_env, "insights/claim-verification-history.md")
    text = open(history).read()
    # The executor's own history writer: archived frontmatter + a dated entry
    # tagged with the memory's id, so the sibling is byte-shaped like a
    # consolidation ARCHIVE and `palinode trace` reads it unchanged.
    assert _meta(history)["status"] == "archived"
    assert "misnamed by an early promote" in text
    assert "<!-- fact:insights-claim-verification -->" in text


def test_archive_preserves_the_body(store_env):
    marker = "the original body text must survive retirement"
    path = _write_and_index(
        store_env, "insights/keeper.md", marker,
        {"id": "insights-keeper", "category": "insights"},
    )
    archive_mod.archive_memory("insights/keeper.md")
    assert marker in open(path).read()


def test_archived_memory_leaves_default_recall_but_is_retained(store_env):
    from palinode.core import store

    marker = "obsolete finding about the oracle accept path"
    _write_and_index(
        store_env, "insights/stale-finding.md", marker,
        {"id": "insights-stale-finding", "category": "insights"},
    )

    q = [0.05] * EMBED_DIM
    before = store.search(q, threshold=0.0, top_k=50, record_access=False)
    assert any(marker in r["content"] for r in before)

    result = archive_mod.archive_memory("insights/stale-finding.md", reason="wrong")
    assert result["chunks_updated"] >= 1

    after = store.search(q, threshold=0.0, top_k=50, record_access=False)
    assert not any(marker in r["content"] for r in after)

    # Never hard-deleted: still retrievable when exclusion is lifted.
    retained = store.search(
        q, threshold=0.0, top_k=50, status_exclude_list=[], record_access=False
    )
    assert any(marker in r["content"] for r in retained)


def test_archive_commits_the_memory_and_its_history_together(store_env):
    _write_and_index(
        store_env, "insights/committed.md", "body",
        {"id": "insights-committed", "category": "insights"},
    )
    _git(store_env, "add", "-A")
    _git(store_env, "commit", "-q", "-m", "seed")

    result = archive_mod.archive_memory("insights/committed.md", reason="obsolete")
    assert result["committed"] is True

    files = _git(store_env, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == [
        "insights/committed-history.md",
        "insights/committed.md",
    ]
    assert "archive: insights/committed.md" in _git(store_env, "log", "-1", "--format=%s")


# ── core: SUPERSEDE ───────────────────────────────────────────────────────────


def test_supersede_records_the_successor(store_env):
    _write_and_index(
        store_env, "decisions/old-policy.md", "the superseded decision",
        {"id": "decision-old-policy", "category": "decisions"},
    )

    result = archive_mod.archive_memory(
        "decisions/old-policy.md",
        reason="replaced after the re-promote",
        superseded_by="decisions/new-policy.md",
    )

    assert result["superseded_by"] == "decisions/new-policy.md"
    meta = _meta(os.path.join(store_env, "decisions/old-policy.md"))
    # Status stays `archived`, not `superseded`: only `archived` is in
    # config.search.exclude_status, so `superseded` would leave the retired
    # memory in default recall — the bug this feature closes.
    assert meta["status"] == "archived"
    assert meta["superseded_by"] == "decisions/new-policy.md"

    history = open(os.path.join(store_env, "decisions/old-policy-history.md")).read()
    assert "Superseded by decisions/new-policy.md" in history
    assert "replaced after the re-promote" in history
    assert "supersede: decisions/old-policy.md -> decisions/new-policy.md" in _git(
        store_env, "log", "-1", "--format=%s"
    )


def test_supersede_accepts_a_bare_slug(store_env):
    _write_and_index(
        store_env, "insights/a.md", "old", {"id": "insights-a", "category": "insights"},
    )
    result = archive_mod.archive_memory("insights/a.md", superseded_by="new-finding")
    assert result["superseded_by"] == "new-finding"
    assert _meta(os.path.join(store_env, "insights/a.md"))["superseded_by"] == "new-finding"


def test_archived_file_is_visible_to_trace_as_a_supersession_trail(store_env):
    """The audit sibling is the same artifact `palinode trace` already reads."""
    from palinode.core.trace import STATUS_PRESENT, compose_trace

    _write_and_index(
        store_env, "decisions/traced.md", "a decision that got retired",
        {"id": "decision-traced", "category": "decisions"},
    )
    archive_mod.archive_memory(
        "decisions/traced.md", reason="obsolete", superseded_by="decisions/next.md"
    )

    trace = compose_trace("decisions/traced.md", store_env)
    assert trace["supersession"]["status"] == STATUS_PRESENT
    assert trace["supersession"]["history_file"] == "decisions/traced-history.md"
    assert any("Superseded by decisions/next.md" in e for e in trace["supersession"]["entries"])


# ── core: rejection + idempotence ─────────────────────────────────────────────


def test_already_archived_is_a_reported_no_op(store_env):
    _write_and_index(
        store_env, "insights/done.md", "already gone",
        {"id": "insights-done", "category": "insights", "status": "archived"},
    )
    _git(store_env, "add", "-A")
    _git(store_env, "commit", "-q", "-m", "seed")
    head = _git(store_env, "rev-parse", "HEAD").strip()

    result = archive_mod.archive_memory("insights/done.md", reason="again")

    assert result["status"] == "already_archived"
    assert result["committed"] is False
    assert result["history_file"] is None
    assert not os.path.exists(os.path.join(store_env, "insights/done-history.md"))
    assert _git(store_env, "rev-parse", "HEAD").strip() == head  # no new commit


def test_archive_is_idempotent_across_two_calls(store_env):
    _write_and_index(
        store_env, "insights/twice.md", "body",
        {"id": "insights-twice", "category": "insights"},
    )
    first = archive_mod.archive_memory("insights/twice.md", reason="one")
    second = archive_mod.archive_memory("insights/twice.md", reason="two")
    assert first["status"] == "archived"
    assert second["status"] == "already_archived"
    history = open(os.path.join(store_env, "insights/twice-history.md")).read()
    assert history.count("reason: one") == 1
    assert "reason: two" not in history


def test_missing_memory_raises_file_not_found(store_env):
    with pytest.raises(FileNotFoundError):
        archive_mod.archive_memory("insights/nope.md")


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "insights/../../../etc/passwd",
        "/etc/passwd",
        "insights/\x00evil.md",
    ],
)
def test_traversal_and_null_bytes_are_rejected(store_env, bad):
    with pytest.raises(ValueError):
        archive_mod.archive_memory(bad)


def test_symlink_pointing_outside_memory_dir_is_rejected(store_env, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.md"
    outside.write_text("---\nid: secret\n---\n\nnot yours\n")
    link = os.path.join(store_env, "insights", "escape.md")
    os.symlink(str(outside), link)

    with pytest.raises(ValueError):
        archive_mod.archive_memory("insights/escape.md")
    # The symlink target is untouched.
    assert "status: archived" not in outside.read_text()


def test_superseded_by_is_held_to_the_same_path_guard(store_env):
    _write_and_index(
        store_env, "insights/guarded.md", "body",
        {"id": "insights-guarded", "category": "insights"},
    )
    with pytest.raises(ValueError):
        archive_mod.archive_memory("insights/guarded.md", superseded_by="../../etc/passwd")
    # Rejected before any mutation.
    assert _meta(os.path.join(store_env, "insights/guarded.md")).get("status") != "archived"


def test_replace_policy_document_is_archivable(store_env):
    """The ADR-015 §2.2 replace-guard is consolidation-scoped, not a global ban.

    The memories that motivated this feature had been hand-tombstoned with
    `save(update_policy="replace")`, so refusing `replace` docs here would make
    the op unable to fix its own case. Nothing is forked into history — the
    whole file is retired in place — so the failure mode the guard prevents
    does not arise.
    """
    _write_and_index(
        store_env, "insights/living.md", "a living current-state doc",
        {"id": "insights-living", "category": "insights", "update_policy": "replace"},
    )
    result = archive_mod.archive_memory("insights/living.md", reason="hand-tombstoned earlier")
    assert result["status"] == "archived"
    assert _meta(os.path.join(store_env, "insights/living.md"))["status"] == "archived"


def test_ttl_sweep_still_uses_the_shared_frontmatter_primitive(store_env):
    """Regression: the TTL sweep delegates to the same flip, so they can't drift."""
    from datetime import UTC, datetime, timedelta

    from palinode.consolidation import ttl

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _write_and_index(
        store_env, "insights/expired.md", "ephemeral",
        {"id": "insights-expired", "category": "insights", "expires_at": past},
    )
    # ttl imports the symbol at module scope, so patch it there: if the sweep
    # ever stops delegating, call_count drops to 0 and this fails.
    with patch.object(ttl, "set_archived_frontmatter",
                      wraps=archive_mod.set_archived_frontmatter) as spy:
        result = ttl.archive_expired()
    assert result["count"] == 1
    assert spy.call_count == 1
    assert _meta(os.path.join(store_env, "insights/expired.md"))["status"] == "archived"


# ── REST ──────────────────────────────────────────────────────────────────────


def _seed(client, *, slug, content, type="Insight", **kw) -> str:
    body = {"content": content, "type": type, "slug": slug}
    body.update(kw)
    res = client.post("/save", json=body)
    assert res.status_code == 200, res.text
    return os.path.relpath(res.json()["file_path"], config.memory_dir)


def test_archive_endpoint_returns_the_result_object(api_client):
    client, memory_dir = api_client
    rel = _seed(client, slug="endpoint-target", content="obsolete endpoint finding")

    res = client.post("/archive", json={"file_path": rel, "reason": "obsolete"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["file"] == rel
    assert data["status"] == "archived"
    assert data["history_file"].endswith("-history.md")
    assert _meta(os.path.join(memory_dir, rel))["status"] == "archived"


def test_archive_endpoint_supersede(api_client):
    client, memory_dir = api_client
    rel = _seed(client, slug="endpoint-old", content="the old one")

    res = client.post(
        "/archive",
        json={"file_path": rel, "superseded_by": "insights/endpoint-new.md"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["superseded_by"] == "insights/endpoint-new.md"
    assert _meta(os.path.join(memory_dir, rel))["superseded_by"] == "insights/endpoint-new.md"


def test_archive_endpoint_404_for_missing(api_client):
    client, _ = api_client
    res = client.post("/archive", json={"file_path": "insights/absent.md"})
    assert res.status_code == 404


def test_archive_endpoint_rejects_traversal(api_client):
    client, _ = api_client
    res = client.post("/archive", json={"file_path": "../../etc/passwd"})
    assert res.status_code == 400


def test_archive_endpoint_requires_file_path(api_client):
    client, _ = api_client
    assert client.post("/archive", json={}).status_code == 422


def test_archive_endpoint_second_call_reports_already_archived(api_client):
    client, _ = api_client
    rel = _seed(client, slug="endpoint-twice", content="retire me")
    assert client.post("/archive", json={"file_path": rel}).status_code == 200
    again = client.post("/archive", json={"file_path": rel})
    assert again.status_code == 200
    assert again.json()["status"] == "already_archived"


# ── CLI ───────────────────────────────────────────────────────────────────────


class _FakeAPI:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    def archive(self, file_path, reason=None, superseded_by=None):
        self.calls.append(
            {"file_path": file_path, "reason": reason, "superseded_by": superseded_by}
        )
        return self._payload


def _cli_module():
    # importlib.import_module: the package's `archive` attribute is shadowed by
    # the Command re-exported in cli/__init__ (same idiom as cli/trace).
    return importlib.import_module("palinode.cli.archive")


def test_cli_archive_text_and_json():
    from click.testing import CliRunner

    mod = _cli_module()
    payload = {
        "file": "insights/x.md",
        "status": "archived",
        "superseded_by": "insights/y.md",
        "reason": "obsolete",
        "history_file": "insights/x-history.md",
        "chunks_updated": 3,
        "committed": True,
    }
    fake = _FakeAPI(payload)
    with patch.object(mod, "api_client", fake):
        res_json = CliRunner().invoke(
            mod.archive,
            ["insights/x.md", "--reason", "obsolete",
             "--superseded-by", "insights/y.md", "--format", "json"],
        )
        assert res_json.exit_code == 0, res_json.output
        assert json.loads(res_json.output)["file"] == "insights/x.md"

        res_text = CliRunner().invoke(mod.archive, ["insights/x.md", "--format", "text"])
        assert res_text.exit_code == 0, res_text.output
        assert "Superseded by insights/y.md" in res_text.output

    assert fake.calls[0] == {
        "file_path": "insights/x.md",
        "reason": "obsolete",
        "superseded_by": "insights/y.md",
    }


def test_cli_archive_reports_already_archived():
    from click.testing import CliRunner

    mod = _cli_module()
    payload = {"file": "insights/x.md", "status": "already_archived",
               "superseded_by": None, "history_file": None, "chunks_updated": 0}
    with patch.object(mod, "api_client", _FakeAPI(payload)):
        res = CliRunner().invoke(mod.archive, ["insights/x.md", "--format", "text"])
    assert res.exit_code == 0, res.output
    assert "already archived" in res.output


def test_cli_archive_defaults_to_json_when_piped():
    from click.testing import CliRunner

    mod = _cli_module()
    payload = {"file": "insights/x.md", "status": "archived"}
    with patch.object(mod, "api_client", _FakeAPI(payload)):
        # CliRunner stdout is not a TTY → get_default_format() picks JSON.
        res = CliRunner().invoke(mod.archive, ["insights/x.md"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["file"] == "insights/x.md"


# ── MCP ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_archive_tool_registered_full_not_core(monkeypatch):
    from palinode.mcp import list_tools

    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    full = {t.name: t for t in await list_tools()}
    assert "palinode_archive" in full
    schema = full["palinode_archive"].inputSchema
    assert schema["required"] == ["file_path"]
    assert set(schema["properties"]) == {"file_path", "reason", "superseded_by"}

    monkeypatch.setenv("PALINODE_MCP_SURFACE", "core")
    core = {t.name for t in await list_tools()}
    assert "palinode_archive" not in core  # full-surface only, keeps core slim


@pytest.mark.asyncio
async def test_mcp_archive_dispatch_forwards_and_renders(monkeypatch):
    import palinode.mcp as mcp

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "file": "insights/x.md",
                "status": "archived",
                "superseded_by": "insights/y.md",
                "history_file": "insights/x-history.md",
                "chunks_updated": 2,
                "committed": True,
            }

    async def _fake_post(path, json=None, timeout=30.0):
        captured["path"] = path
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(mcp, "_post", _fake_post)
    result = await mcp._dispatch_tool(
        "palinode_archive",
        {"file_path": "insights/x.md", "reason": "obsolete",
         "superseded_by": "insights/y.md"},
    )
    assert captured["path"] == "/archive"
    assert captured["json"] == {
        "file_path": "insights/x.md",
        "reason": "obsolete",
        "superseded_by": "insights/y.md",
    }
    assert "Superseded by insights/y.md" in result[0].text


@pytest.mark.asyncio
async def test_mcp_archive_requires_file_path():
    import palinode.mcp as mcp

    result = await mcp._dispatch_tool("palinode_archive", {})
    assert "file_path is required" in result[0].text


@pytest.mark.asyncio
async def test_mcp_archive_surfaces_api_errors(monkeypatch):
    import palinode.mcp as mcp

    class _Resp:
        status_code = 404
        text = "File not found"

    async def _fake_post(path, json=None, timeout=30.0):
        return _Resp()

    monkeypatch.setattr(mcp, "_post", _fake_post)
    result = await mcp._dispatch_tool("palinode_archive", {"file_path": "insights/nope.md"})
    assert result[0].text.startswith("Archive failed:")
