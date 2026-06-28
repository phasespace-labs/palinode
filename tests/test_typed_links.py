"""Typed contradiction + evidence-backing links (#533, G4).

Covers the four acceptance criteria from the issue:

  1. Both link types (``contradicts`` / ``backed_by``) persist and round-trip
     through save → file → reload (REST surface, the choke all surfaces forward
     to). Plus a seeded conflicting pair (the reciprocal back-link).
  2. Malformed refs are rejected at the save surface with HTTP 400.
  3. ``lint`` reports open contradictions (``open_contradictions`` finding).
  4. The consolidation executor can PROPOSE a contradicts link but never
     auto-resolves a conflict (no SUPERSEDE side effects).

Real SQLite + tmp_path; the embedder is mocked so no Ollama is needed (mirrors
test_update_policy_axis.py). The executor tests use plain tempfiles — it's a
pure file-mutation layer.
"""
from __future__ import annotations

import importlib
import os
import tempfile
from unittest.mock import patch

import frontmatter
import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config

_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient on a fresh tmp memory_dir + real SQLite DB; git off."""
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with TestClient(srv.app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _save(client, **body):
    body.setdefault("type", "ActionItem")
    scan = patch("palinode.core.store.scan_memory_content", return_value=(True, "OK"))
    embed = patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR)
    with scan, embed:
        return client.post("/save", json=body)


def _meta(file_path: str) -> dict:
    return frontmatter.load(file_path).metadata


# ── (1) persist + round-trip ─────────────────────────────────────────────────

def test_contradicts_persists_to_frontmatter(client):
    res = _save(client, content="Old infra claim.", slug="claim-a",
                contradicts=["decisions/claim-b"])
    assert res.status_code == 200, res.text
    meta = _meta(res.json()["file_path"])
    assert meta["contradicts"] == ["decisions/claim-b"]


def test_backed_by_persists_to_frontmatter(client):
    res = _save(client, content="Supported finding.", slug="finding-a",
                backed_by=["research/paper", "decisions/d1"])
    assert res.status_code == 200, res.text
    meta = _meta(res.json()["file_path"])
    assert meta["backed_by"] == ["research/paper", "decisions/d1"]


def test_both_links_round_trip_via_read(client):
    res = _save(client, content="Two-link memory.", slug="both",
                contradicts=["insights/x"], backed_by=["research/y"])
    assert res.status_code == 200, res.text
    rel = res.json()["file_path"].rsplit("/", 2)[-2:]
    read = client.get("/read", params={"file_path": "/".join(rel), "meta": True})
    assert read.status_code == 200, read.text
    fm = read.json()["frontmatter"]
    assert fm["contradicts"] == ["insights/x"]
    assert fm["backed_by"] == ["research/y"]


def test_clean_frontmatter_when_links_absent(client):
    res = _save(client, content="No links here.", slug="plain")
    assert res.status_code == 200, res.text
    meta = _meta(res.json()["file_path"])
    assert "contradicts" not in meta
    assert "backed_by" not in meta


def test_duplicate_refs_deduped(client):
    res = _save(client, content="dupes.", slug="dup",
                contradicts=["decisions/x", "decisions/x"])
    assert res.status_code == 200, res.text
    assert _meta(res.json()["file_path"])["contradicts"] == ["decisions/x"]


def test_links_via_metadata_resolve_and_validate(client):
    """A metadata-tunneled link is still resolved + validated (not verbatim)."""
    res = _save(client, content="meta tunnel.", slug="mt",
                metadata={"contradicts": ["decisions/z"]})
    assert res.status_code == 200, res.text
    assert _meta(res.json()["file_path"])["contradicts"] == ["decisions/z"]


# ── (2) malformed refs rejected with HTTP 400 ────────────────────────────────

@pytest.mark.parametrize("bad", [
    ["../escape"],
    ["/abs/path"],
    [""],
    ["  "],
    [123],
    "notalist-but-coerced-ok",  # a bare string is allowed; see below
])
def test_malformed_contradicts_rejected_or_coerced(client, bad):
    res = _save(client, content="bad ref.", slug="bad", contradicts=bad)
    if bad == "notalist-but-coerced-ok":
        # A single string is coerced to a one-element list — valid ref.
        assert res.status_code == 200, res.text
    else:
        assert res.status_code == 400, res.text


def test_malformed_backed_by_rejected(client):
    res = _save(client, content="bad.", slug="bad2", backed_by=["a/../b"])
    assert res.status_code == 400, res.text


# ── (1b) seeded conflicting pair: reciprocal back-link ───────────────────────

def test_reciprocal_back_link_seeded_pair(client):
    """A declares contradicts:[B]; B (already on disk) gains contradicts:[A]."""
    # Seed B first.
    res_b = _save(client, content="Claim B: the sky is green.", slug="claim-b",
                  type="Decision")
    assert res_b.status_code == 200, res_b.text
    b_meta = _meta(res_b.json()["file_path"])
    assert "contradicts" not in b_meta  # clean before the link

    # Now A declares the conflict with B.
    res_a = _save(client, content="Claim A: the sky is blue.", slug="claim-a",
                  type="Decision", contradicts=["decisions/claim-b"])
    assert res_a.status_code == 200, res_a.text
    assert _meta(res_a.json()["file_path"])["contradicts"] == ["decisions/claim-b"]

    # B now carries the reciprocal link back to A.
    b_after = _meta(res_b.json()["file_path"])
    assert b_after.get("contradicts") == ["decisions/claim-a"]


def test_back_link_missing_target_does_not_fail_save(client):
    """A contradicts a non-existent target — save still succeeds (best-effort)."""
    res = _save(client, content="A alone.", slug="lonely", type="Decision",
                contradicts=["decisions/ghost"])
    assert res.status_code == 200, res.text
    assert _meta(res.json()["file_path"])["contradicts"] == ["decisions/ghost"]


# ── (3) lint surfaces open contradictions ────────────────────────────────────

def test_lint_reports_open_contradictions(client, tmp_path):
    _save(client, content="conflicting memory.", slug="conf",
          type="Decision", contradicts=["decisions/other"])
    from palinode.core.lint import run_lint_pass
    report = run_lint_pass()
    files = {oc["file"] for oc in report["open_contradictions"]}
    assert any(f.endswith("conf.md") for f in files), report["open_contradictions"]


def test_lint_no_false_positive_without_links(client):
    _save(client, content="clean memory.", slug="clean", type="Decision")
    from palinode.core.lint import run_lint_pass
    report = run_lint_pass()
    assert report["open_contradictions"] == []


# ── (4) executor PROPOSE_CONTRADICTS: propose, never auto-resolve ────────────

_DOC = """---
id: decisions-pick
category: decisions
type: Decision
---

# Pick

- [2026-01-01] We chose Postgres <!-- fact:f1 -->
"""


def _tmp_doc(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def test_executor_proposes_contradicts_link():
    from palinode.consolidation.executor import apply_operations
    path = _tmp_doc(_DOC)
    try:
        stats = apply_operations(path, [
            {"op": "PROPOSE_CONTRADICTS", "contradicts": ["decisions/other-pick"]},
        ])
        assert stats["contradicts_proposed"] == 1
        meta = frontmatter.load(path).metadata
        assert meta["contradicts"] == ["decisions/other-pick"]
        # The fact body and original frontmatter are untouched — no winner picked.
        body = frontmatter.load(path).content
        assert "We chose Postgres" in body
        assert "~~" not in body  # no strikethrough → nothing superseded
        assert not os.path.exists(path.replace(".md", "-history.md"))
    finally:
        for p in (path, path.replace(".md", "-history.md")):
            if os.path.exists(p):
                os.remove(p)


def test_executor_propose_is_idempotent():
    from palinode.consolidation.executor import apply_operations
    path = _tmp_doc(_DOC)
    try:
        apply_operations(path, [
            {"op": "PROPOSE_CONTRADICTS", "contradicts": ["decisions/x"]},
        ])
        stats2 = apply_operations(path, [
            {"op": "PROPOSE_CONTRADICTS", "contradicts": ["decisions/x"]},
        ])
        # Second proposal of the same ref is a no-op (already present).
        assert stats2["contradicts_proposed"] == 0
        assert frontmatter.load(path).metadata["contradicts"] == ["decisions/x"]
    finally:
        for p in (path, path.replace(".md", "-history.md")):
            if os.path.exists(p):
                os.remove(p)


def test_executor_propose_rejects_malformed_refs():
    from palinode.consolidation.executor import apply_operations
    path = _tmp_doc(_DOC)
    try:
        stats = apply_operations(path, [
            {"op": "PROPOSE_CONTRADICTS", "contradicts": ["../escape"]},
        ])
        assert stats["contradicts_proposed"] == 0
        assert "contradicts" not in frontmatter.load(path).metadata
    finally:
        if os.path.exists(path):
            os.remove(path)
