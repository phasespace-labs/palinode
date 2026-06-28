"""Tests for #366 — advisory project-memory review.

`run_review` composes the deterministic lint signals scoped to a project and
proposes corrective ops. Read-only. No embedder needed — pure filesystem + lint.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from palinode.core import review as review_mod
from palinode.core.config import config


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _write(path, *, entities=None, epistemic=None, status=None,
           contradicts=None, days_old=200, body="Some content."):
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = [
        "---",
        f"id: {path.parent.name}-{path.stem}",
        f"category: {path.parent.name}",
        f"created_at: {_iso(days_old)}",
        f"last_updated: {_iso(days_old)}",
        "description: has a description",
    ]
    if entities is not None:
        fm.append(f"entities: [{', '.join(entities)}]")
    if epistemic is not None:
        fm.append(f"epistemic: {epistemic}")
    if status is not None:
        fm.append(f"status: {status}")
    if contradicts is not None:
        fm.append(f"contradicts: [{', '.join(contradicts)}]")
    fm.append("---")
    fm.append("")
    fm.append(body)
    path.write_text("\n".join(fm) + "\n")


@pytest.fixture()
def memdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    return tmp_path


# ── whole-store review ───────────────────────────────────────────────────────

def test_review_whole_store_surfaces_findings(memdir):
    _write(memdir / "insights" / "stale.md", entities=["project/alpha"], status="active")
    _write(memdir / "insights" / "oq.md", entities=["project/alpha"], epistemic="open_question")
    _write(memdir / "insights" / "conflict.md", entities=["project/alpha"], contradicts=["insights/oq"])

    out = review_mod.run_review()
    assert out["project"] is None
    f = out["findings"]
    assert any("stale.md" in review_mod._finding_file(x) for x in f["stale"])
    assert any("oq.md" in review_mod._finding_file(x) for x in f["open_questions"])
    assert any("conflict.md" in review_mod._finding_file(x) for x in f["contradictions"])
    # proposed ops mirror findings, all advisory PROPOSE_*
    ops = out["proposed_ops"]
    assert ops and all(o["op"].startswith("PROPOSE_") for o in ops)
    kinds = {o["op"] for o in ops}
    assert {"PROPOSE_ARCHIVE", "PROPOSE_UPDATE", "PROPOSE_SUPERSEDE"} <= kinds


# ── project scoping ──────────────────────────────────────────────────────────

def test_review_scopes_to_project(memdir):
    _write(memdir / "insights" / "alpha-stale.md", entities=["project/alpha"], status="active")
    _write(memdir / "insights" / "beta-stale.md", entities=["project/beta"], status="active")

    out = review_mod.run_review("alpha")
    assert out["project"] == "project/alpha"
    stale_files = [review_mod._finding_file(x) for x in out["findings"]["stale"]]
    assert any("alpha-stale.md" in f for f in stale_files)
    assert not any("beta-stale.md" in f for f in stale_files)
    # only alpha's one file is in scope
    assert out["scope_file_count"] == 1


def test_review_accepts_typed_project_ref(memdir):
    _write(memdir / "insights" / "a.md", entities=["project/alpha"], status="active")
    out = review_mod.run_review("project/alpha")
    assert out["project"] == "project/alpha"
    assert out["scope_file_count"] == 1


def test_review_empty_project_has_no_findings(memdir):
    _write(memdir / "insights" / "a.md", entities=["project/alpha"], status="active")
    out = review_mod.run_review("ghost")  # no files tagged project/ghost
    assert out["scope_file_count"] == 0
    assert out["summary"]["finding_count"] == 0
    assert out["proposed_ops"] == []


# ── read-only guarantee ──────────────────────────────────────────────────────

def test_review_is_read_only(memdir):
    p = memdir / "insights" / "stale.md"
    _write(p, entities=["project/alpha"], status="active")
    before = p.read_text()
    review_mod.run_review("alpha")
    assert p.read_text() == before  # review never mutates


# ── API surface ──────────────────────────────────────────────────────────────

def test_review_api_endpoint(memdir, monkeypatch):
    import importlib
    monkeypatch.setattr(config, "db_path", str(memdir / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    # Files exist on disk but no index DB yet — review reads frontmatter directly,
    # so allow the fresh-DB startup rather than tripping the misconfig guard.
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    _write(memdir / "insights" / "stale.md", entities=["project/alpha"], status="active")

    import palinode.api.server as srv
    srv = importlib.reload(srv)
    from fastapi.testclient import TestClient
    with TestClient(srv.app, raise_server_exceptions=True) as c:
        res = c.post("/review", json={"project": "alpha"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["project"] == "project/alpha"
    assert data["summary"]["scope_file_count"] == 1
