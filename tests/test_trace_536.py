"""#536 (C1) — ``palinode trace`` provenance composition command.

Covers the composition engine (:mod:`palinode.core.trace`) joining every
provenance primitive into one lineage view: source citations (G1), the
saved/changed git commits, the supersession trail, typed contradiction/evidence
links (G4), and the retrieval log — plus the honest ``not_captured`` placeholders
for the gaps not yet built (G2 extraction metadata, G3 terminal edge). Also
covers the REST endpoint, the TTY-aware CLI, and the MCP tool surface.

Real SQLite + real git in tmp_path, no DB mocking (repo rule). Saves go through
the API with the embedder + security scanner mocked so chunks index for real;
the git-fixture idiom (a real ``git init`` tmp memory_dir) mirrors
``tests/test_ui_phase1.py`` and ``tests/test_claims_508.py``.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.core import git_tools
from palinode.core.config import config
from palinode.core.trace import (
    STATUS_NONE,
    STATUS_NOT_CAPTURED,
    STATUS_PRESENT,
    compose_trace,
    format_trace_text,
)

_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with a fresh git-backed tmp memory_dir + real SQLite.

    Git auto_commit is ON so saves commit and git_tools can read a real history.
    """
    db_path = tmp_path / ".palinode.db"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.services.api, "host", "127.0.0.1")
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE", "PALINODE_API_HOST"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with TestClient(srv.app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _seed(client, *, slug, content, type="Decision", **kw) -> str:
    scan_p = patch("palinode.core.store.scan_memory_content", return_value=(True, "OK"))
    embed_p = patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR)
    with scan_p, embed_p:
        body = {"content": content, "type": type, "slug": slug}
        body.update(kw)
        res = client.post("/save", json=body)
    assert res.status_code == 200, res.text
    return os.path.relpath(res.json()["file_path"], config.memory_dir)


def _git(*args: str) -> None:
    subprocess.run(["git", "-C", config.memory_dir, *args], check=True, capture_output=True)


def _write(rel: str, text: str) -> None:
    abs_path = os.path.join(config.memory_dir, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(text)


# ── saved / changed (git blame + history composition) ─────────────────────────


def test_trace_saved_is_creation_commit_and_changed_lists_later(client):
    rel = _seed(client, slug="alpha", content="# Alpha\n\noriginal body")
    # A second commit that modifies the file.
    _write(rel, "---\ntype: Decision\n---\n\n# Alpha\n\nrevised body")
    _git("add", rel)
    _git("commit", "-qm", "revise alpha")

    trace = compose_trace(rel, config.memory_dir)

    assert trace["file"] == rel
    saved = trace["saved"]
    assert saved["status"] == STATUS_PRESENT
    creation = git_tools.first_commit(rel)
    assert saved["commit"] == creation["hash"]
    # The creation commit is NOT double-counted in `changed`.
    changed = trace["changed"]
    assert changed["status"] == STATUS_PRESENT
    assert saved["commit"] not in {c["hash"] for c in changed["commits"]}
    assert any("revise alpha" == c["message"] for c in changed["commits"])


def test_trace_single_commit_has_no_changes(client):
    rel = _seed(client, slug="solo", content="# Solo\n\nbody")
    trace = compose_trace(rel, config.memory_dir)
    assert trace["saved"]["status"] == STATUS_PRESENT
    assert trace["changed"]["status"] == STATUS_NONE
    assert trace["changed"]["commits"] == []


# ── supersession chain (the acceptance-criterion integration case) ────────────


def test_trace_surfaces_supersession_chain(client):
    """A seeded repo with a known supersession chain: the executor's on-disk
    output is a ``-history.md`` sibling plus an in-body ``[superseded]`` tombstone."""
    rel = _seed(client, slug="auth", content="# Auth\n\nUse JWTs")

    # Mirror what the consolidation executor writes on a SUPERSEDE op.
    base = rel[:-3]
    history_rel = f"{base}-history.md"
    _write(
        history_rel,
        "---\ncategory: history\ncore: false\nstatus: archived\n---\n\n# History\n\n"
        "- [2026-05-09 10:00] Superseded (2026-05-09): rotated to session tokens "
        "<!-- fact:auth-1 -->\n",
    )
    _write(
        rel,
        "---\ntype: Decision\n---\n\n# Auth\n\n"
        "- ~~Use JWTs~~ [superseded 2026-05-09] <!-- fact:auth-1 -->\n"
        "- Use server-side session tokens <!-- fact:supersedes-auth-1 -->\n",
    )
    _git("add", rel, history_rel)
    _git("commit", "-qm", "supersede auth")

    sup = compose_trace(rel, config.memory_dir)["supersession"]
    assert sup["status"] == STATUS_PRESENT
    assert sup["history_file"] == history_rel
    assert len(sup["entries"]) == 1
    assert "Superseded" in sup["entries"][0]
    assert sup["in_file_tombstones"] == 1


def test_trace_clean_file_has_no_supersession(client):
    rel = _seed(client, slug="clean", content="# Clean\n\nno tombstones")
    sup = compose_trace(rel, config.memory_dir)["supersession"]
    assert sup["status"] == STATUS_NONE
    assert sup["history_file"] is None
    assert sup["in_file_tombstones"] == 0


# ── source citations (G1 — landed as sources anchors) ─────────────────────────


def test_trace_surfaces_source_anchors(client):
    rel = _seed(
        client,
        slug="cited",
        content="# Cited\n\nthe two models share the card",
        sources=[{"ref": "research/contention.md", "quote": "share the card"}],
    )
    source = compose_trace(rel, config.memory_dir)["source"]
    assert source["status"] == STATUS_PRESENT
    assert any(a["ref"] == "research/contention.md" for a in source["anchors"])


def test_trace_file_without_citations_reports_none(client):
    rel = _seed(client, slug="bare", content="# Bare\n\nno sources")
    source = compose_trace(rel, config.memory_dir)["source"]
    assert source["status"] == STATUS_NONE
    assert source["anchors"] == []
    assert source["claims"] == []


# ── typed links (G4 — landed) ─────────────────────────────────────────────────


def test_trace_surfaces_typed_links(client):
    _seed(client, slug="loser", content="# Loser\n\nold claim")
    rel = _seed(
        client,
        slug="winner",
        content="# Winner\n\nnew claim",
        contradicts=["decisions/loser"],
        backed_by=["research/paper"],
    )
    trace = compose_trace(rel, config.memory_dir)
    assert trace["contradicts"]["status"] == STATUS_PRESENT
    assert "decisions/loser" in trace["contradicts"]["refs"]
    assert trace["backed_by"]["status"] == STATUS_PRESENT
    assert "research/paper" in trace["backed_by"]["refs"]


def test_trace_no_typed_links_reports_none(client):
    rel = _seed(client, slug="lonely", content="# Lonely\n\nno links")
    trace = compose_trace(rel, config.memory_dir)
    assert trace["contradicts"]["status"] == STATUS_NONE
    assert trace["backed_by"]["status"] == STATUS_NONE


# ── recall log (G3 partial — retrieval events exist) ──────────────────────────


def test_trace_aggregates_recall_events(client):
    rel = _seed(client, slug="recalled", content="# Recalled\n\nbody")
    log_dir = os.path.join(config.memory_dir, ".audit")
    os.makedirs(log_dir, exist_ok=True)
    abs_path = os.path.join(config.memory_dir, rel)
    events = [
        {"timestamp": "2026-05-08T09:00:00+00:00", "file_path": rel, "session_id": "sess-1"},
        {"timestamp": "2026-05-12T09:00:00+00:00", "file_path": rel, "session_id": "sess-2"},
        # An absolute path form is normalized to the same file.
        {"timestamp": "2026-05-12T18:00:00+00:00", "file_path": abs_path, "session_id": "sess-2"},
    ]
    with open(os.path.join(log_dir, "retrievals.jsonl"), "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    recalled = compose_trace(rel, config.memory_dir)["recalled"]
    assert recalled["status"] == STATUS_PRESENT
    assert recalled["count"] == 3
    assert recalled["sessions"] == ["sess-1", "sess-2"]
    assert set(recalled["dates"]) == {"2026-05-08", "2026-05-12"}
    assert recalled["last"].startswith("2026-05-12")


def test_trace_never_recalled_reports_none(client):
    rel = _seed(client, slug="unread", content="# Unread\n\nbody")
    recalled = compose_trace(rel, config.memory_dir)["recalled"]
    assert recalled["status"] == STATUS_NONE
    assert recalled["count"] == 0


def test_recall_matches_non_canonical_logged_paths(client):
    """Events logged as './x.md' or 'a/../x.md' must still count.

    Retrieval events are logged as given by each producer; comparing raw
    strings would silently undercount the recall the feature exists to surface.
    """
    rel = _seed(client, slug="noncanon", content="# NonCanon\n\nbody")
    log_dir = os.path.join(config.memory_dir, ".audit")
    os.makedirs(log_dir, exist_ok=True)
    spellings = [
        rel,                                             # canonical
        f"./{rel}",                                      # leading ./
        os.path.join("decisions", "..", rel),            # redundant traversal
        os.path.join(config.memory_dir, rel),            # absolute
    ]
    with open(os.path.join(log_dir, "retrievals.jsonl"), "w", encoding="utf-8") as fh:
        for i, fp in enumerate(spellings):
            fh.write(json.dumps({"timestamp": f"2026-05-0{i + 1}T09:00:00+00:00",
                                 "file_path": fp}) + "\n")

    recalled = compose_trace(rel, config.memory_dir)["recalled"]
    assert recalled["count"] == len(spellings)


# ── degraded inputs must not 500 ──────────────────────────────────────────────


def test_trace_tolerates_malformed_frontmatter(client):
    """Broken YAML degrades like parse_markdown's soft-fail, never raises."""
    rel = "decisions/broken.md"
    _write(rel, "---\nthis: [is: not: valid: yaml\n---\n\n# Broken\n\nbody text\n")
    _git("add", rel)
    _git("commit", "-qm", "add broken")

    trace = compose_trace(rel, config.memory_dir)  # must not raise
    assert trace["file"] == rel
    assert trace["fact"]["epistemic"] == "unmarked"  # empty metadata fallback
    assert client.get(f"/trace/{rel}").status_code == 200


def test_trace_directory_path_is_a_clean_404(client):
    """A directory passes os.path.exists; it must not surface IsADirectoryError."""
    _seed(client, slug="indir", content="# InDir\n\nbody")
    with pytest.raises(FileNotFoundError):
        compose_trace("decisions", config.memory_dir)
    assert client.get("/trace/decisions").status_code == 404


# ── honest placeholders for unbuilt gaps (G2 extraction, G3 terminal edge) ────


def test_trace_placeholders_for_unbuilt_gaps(client):
    rel = _seed(client, slug="gaps", content="# Gaps\n\nbody")
    trace = compose_trace(rel, config.memory_dir)
    assert trace["extracted"]["status"] == STATUS_NOT_CAPTURED
    assert trace["extracted"]["gap"] == "G2"
    assert trace["used_in"]["status"] == STATUS_NOT_CAPTURED
    assert trace["used_in"]["gap"] == "G3"


def test_trace_placeholders_carry_no_private_issue_refs(client):
    """Rendered output ships publicly, where a private issue number misleads."""
    rel = _seed(client, slug="norefs", content="# NoRefs\n\nbody")
    text = format_trace_text(compose_trace(rel, config.memory_dir))
    assert not re.search(r"#\d{2,4}\b", text), text


def test_trace_fact_identity(client):
    rel = _seed(
        client,
        slug="identity",
        content="# Session tokens\n\nbody",
        title="Use server-side session tokens",
        epistemic="fact",
    )
    fact = compose_trace(rel, config.memory_dir)["fact"]
    assert fact["title"] == "Use server-side session tokens"
    assert fact["id"] == rel[:-3]
    assert fact["epistemic"] == "fact"


# ── text rendering (shared CLI/MCP formatter) ─────────────────────────────────


def test_format_trace_text_renders_labels_and_placeholders(client):
    rel = _seed(
        client,
        slug="render",
        content="# Render\n\nbody",
        sources=[{"ref": "research/x.md", "quote": "some quote"}],
    )
    text = format_trace_text(compose_trace(rel, config.memory_dir))
    assert text.startswith(f"## Trace: {rel}")
    assert f"[fact:{rel[:-3]}]" in text  # bracketed label (Rich markup=False path)
    assert "not yet captured (G2)" in text
    assert "not yet captured (G3)" in text
    assert "Source:" in text and "Recalled:" in text


# ── REST endpoint ─────────────────────────────────────────────────────────────


def test_trace_endpoint_returns_structured_object(client):
    rel = _seed(client, slug="rest", content="# Rest\n\nbody")
    res = client.get(f"/trace/{rel}")
    assert res.status_code == 200, res.text
    data = res.json()
    for key in ("file", "fact", "source", "extracted", "saved", "changed",
                "supersession", "contradicts", "backed_by", "recalled", "used_in"):
        assert key in data
    assert data["file"] == rel


def test_trace_endpoint_404_for_missing(client):
    res = client.get("/trace/decisions/nope.md")
    assert res.status_code == 404


def test_trace_endpoint_rejects_traversal(client):
    res = client.get("/trace/../../etc/passwd")
    assert res.status_code in (400, 404)


def test_trace_endpoint_records_a_retrieval(client, tmp_path, monkeypatch):
    """Composing a trace is itself an explicit retrieval; a later trace sees it."""
    from palinode.core.retrieval_log import RetrievalLogger
    import palinode.api.routers.git_history as gh

    # The router's logger is a module-level singleton bound at import; point it
    # at this test's memory_dir so the recorded event lands where compose reads.
    monkeypatch.setattr(gh, "_retrieval_logger", RetrievalLogger(str(tmp_path)))

    rel = _seed(client, slug="logged", content="# Logged\n\nbody")
    assert client.get(f"/trace/{rel}").status_code == 200  # records 1 event
    recalled = client.get(f"/trace/{rel}").json()["recalled"]
    assert recalled["count"] >= 1
    assert recalled["dates"]


# ── CLI (TTY-aware) ───────────────────────────────────────────────────────────


class _FakeAPI:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def trace(self, file_path: str) -> dict[str, Any]:
        return self._payload


def test_cli_trace_json_and_text():
    import importlib

    from click.testing import CliRunner

    # importlib.import_module (not `import palinode.cli.trace`) — the package's
    # `trace` attribute is shadowed by the Command re-exported in cli/__init__.
    trace_mod = importlib.import_module("palinode.cli.trace")

    payload = {
        "file": "decisions/x.md",
        "fact": {"title": "T", "id": "decisions/x", "epistemic": "fact",
                 "type": "Decision", "core": False},
        "source": {"status": STATUS_NONE, "anchors": [], "claims": []},
        "extracted": {"status": STATUS_NOT_CAPTURED, "note": "n", "gap": "G2"},
        "saved": {"status": STATUS_NONE, "commit": None, "date": None, "author": None,
                  "message": None, "origin_date": None, "origin_source": None},
        "changed": {"status": STATUS_NONE, "commits": []},
        "supersession": {"status": STATUS_NONE, "history_file": None, "entries": [],
                         "in_file_tombstones": 0},
        "contradicts": {"status": STATUS_NONE, "refs": []},
        "backed_by": {"status": STATUS_NONE, "refs": []},
        "recalled": {"status": STATUS_NONE, "count": 0, "sessions": [], "dates": [], "last": None},
        "used_in": {"status": STATUS_NOT_CAPTURED, "note": "n", "gap": "G3"},
    }

    with patch.object(trace_mod, "api_client", _FakeAPI(payload)):
        # JSON mode: emits the structured object verbatim.
        res_json = CliRunner().invoke(trace_mod.trace, ["decisions/x.md", "--format", "json"])
        assert res_json.exit_code == 0, res_json.output
        assert json.loads(res_json.output)["file"] == "decisions/x.md"

        # Text mode: renders the human lineage with bracketed labels intact.
        # Strip Rich's cosmetic ANSI highlighting before asserting on content.
        res_text = CliRunner().invoke(trace_mod.trace, ["decisions/x.md", "--format", "text"])
        assert res_text.exit_code == 0, res_text.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", res_text.output)
        assert "## Trace: decisions/x.md" in plain
        assert "[fact:decisions/x]" in plain


def test_cli_trace_defaults_to_json_when_piped():
    import importlib

    from click.testing import CliRunner

    trace_mod = importlib.import_module("palinode.cli.trace")

    payload = {"file": "decisions/x.md"}
    with patch.object(trace_mod, "api_client", _FakeAPI(payload)):
        # CliRunner stdout is not a TTY → get_default_format() picks JSON.
        res = CliRunner().invoke(trace_mod.trace, ["decisions/x.md"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["file"] == "decisions/x.md"


# ── MCP tool ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_trace_tool_registered_full_not_core(monkeypatch):
    from palinode.mcp import list_tools

    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    full = {t.name: t for t in await list_tools()}
    assert "palinode_trace" in full
    assert full["palinode_trace"].inputSchema["required"] == ["file_path"]

    monkeypatch.setenv("PALINODE_MCP_SURFACE", "core")
    core = {t.name for t in await list_tools()}
    assert "palinode_trace" not in core  # full-surface only, keeps core slim


@pytest.mark.asyncio
async def test_mcp_trace_dispatch_renders_text(monkeypatch):
    import palinode.mcp as mcp

    composed = {
        "file": "decisions/x.md",
        "fact": {"title": "T", "id": "decisions/x", "epistemic": "fact",
                 "type": "Decision", "core": False},
        "source": {"status": STATUS_NONE, "anchors": [], "claims": []},
        "extracted": {"status": STATUS_NOT_CAPTURED, "note": "n", "gap": "G2"},
        "saved": {"status": STATUS_NONE, "commit": None, "date": None, "author": None,
                  "message": None, "origin_date": None, "origin_source": None},
        "changed": {"status": STATUS_NONE, "commits": []},
        "supersession": {"status": STATUS_NONE, "history_file": None, "entries": [],
                         "in_file_tombstones": 0},
        "contradicts": {"status": STATUS_NONE, "refs": []},
        "backed_by": {"status": STATUS_NONE, "refs": []},
        "recalled": {"status": STATUS_NONE, "count": 0, "sessions": [], "dates": [], "last": None},
        "used_in": {"status": STATUS_NOT_CAPTURED, "note": "n", "gap": "G3"},
    }

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return composed

    async def _fake_get(path, params=None, timeout=30.0):
        captured["path"] = path
        return _Resp()

    monkeypatch.setattr(mcp, "_get", _fake_get)
    result = await mcp._dispatch_tool("palinode_trace", {"file_path": "decisions/x.md"})
    assert captured["path"] == "/trace/decisions/x.md"
    assert "## Trace: decisions/x.md" in result[0].text


@pytest.mark.asyncio
async def test_mcp_trace_requires_file_path():
    import palinode.mcp as mcp

    result = await mcp._dispatch_tool("palinode_trace", {})
    assert "file_path is required" in result[0].text
