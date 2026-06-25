"""Tests for the Phase 1 provenance-UI views.

One test per new view: each returns 200 and renders seeded data.
  - Memory list (`/ui/memory`): lists a seeded fact + a type filter.
  - Search (`/ui/memory?q=`): returns hits for a seeded fact.
  - Diffs (`/ui/diffs`): renders a git commit touching a memory.
  - Compaction (`/ui/compaction`): renders a consolidation-marked commit.
  - Quality (`/ui/quality`): lists a seeded stale + orphaned file.

Plus unit checks for the store-agnostic shaping helpers in views.py.

Real SQLite + tmp_path, no DB mocking (repo rule). Saves go through the API
with the embedder + security scanner mocked so chunks index for real; the
embedder returns a constant vector, so any search query matches a seeded fact
(cosine == 1.0).
"""
from __future__ import annotations

import importlib
import os
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config

_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with a fresh git-backed tmp memory_dir + real SQLite.

    Git auto_commit is ON here (unlike the P0 fixture) so the diffs/compaction
    views have real commits to read. Loopback host forced; no bearer token.
    """
    db_path = tmp_path / ".palinode.db"
    # Real git repo so saves commit and git_tools can read history.
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


def _write_file(rel: str, text: str) -> str:
    """Write a memory file directly to disk (bypasses indexing); returns rel path."""
    abs_path = os.path.join(config.memory_dir, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(text)
    return rel


# ── 1. Memory list ───────────────────────────────────────────────────────────
def test_memory_list_renders_and_lists_seeded(client):
    _seed(client, slug="alpha", content="# Alpha\n\nbody", title="Alpha decision")
    res = client.get("/ui/memory")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    html = res.text
    assert "decisions/alpha.md" in html
    assert "/ui/memory/decisions/alpha" in html  # links to fact detail
    assert 'class="searchbar"' in html  # search box present
    assert "/ui/static/palinode.css" in html  # reuses P0 design system


def test_memory_list_type_filter(client):
    _seed(client, slug="dec1", content="# D\n\nx", type="Decision")
    _seed(client, slug="ins1", content="# I\n\ny", type="Insight")
    res = client.get("/ui/memory?type=Insight")
    assert res.status_code == 200
    assert "insights/ins1.md" in res.text
    assert "decisions/dec1.md" not in res.text


# ── 2. Search ─────────────────────────────────────────────────────────────────
def test_memory_search_returns_hits(client):
    _seed(client, slug="searchme", content="# Searchme\n\nServer-side session tokens.")
    # The /ui/memory?q= route runs search_api in-process, which embeds the
    # query via embedder.embed — mock it so the test is deterministic and does
    # not reach Ollama. With every chunk + the query at the same vector, cosine
    # is 1.0 and the seeded fact matches.
    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
        res = client.get("/ui/memory?q=session+tokens")
    assert res.status_code == 200
    html = res.text
    assert "Search results" in html
    assert "decisions/searchme.md" in html
    assert "/ui/memory/decisions/searchme" in html


def test_memory_search_degrades_when_embedder_down(client):
    """A search backend failure renders a soft banner, never a 500."""
    _seed(client, slug="x", content="# X\n\nbody")
    with patch("palinode.core.embedder.embed", side_effect=RuntimeError("embedder down")):
        res = client.get("/ui/memory?q=anything")
    assert res.status_code == 200
    assert "search unavailable" in res.text


# ── 3. Diffs ──────────────────────────────────────────────────────────────────
def test_diffs_renders_recent_commit(client):
    _seed(client, slug="changed", content="# Changed\n\nbody")  # auto_commit on
    res = client.get("/ui/diffs")
    assert res.status_code == 200
    html = res.text
    assert "Recent changes" in html
    # The save's commit touched the file and is grouped under a day.
    assert "decisions/changed.md" in html


# ── 4. Compaction ─────────────────────────────────────────────────────────────
def test_compaction_renders_consolidation_commit(client):
    # A consolidation pass is identified by its subject prefix; craft one
    # directly so the read-only view has a pass to surface (we never trigger
    # consolidation from the UI).
    rel = _write_file(
        "decisions/c.md", "---\ncategory: decisions\n---\nbody"
    )
    md = config.memory_dir
    subprocess.run(["git", "-C", md, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", md, "commit", "-q", "-m", "palinode: compaction 2026-06-16 — merged 2 facts"],
        check=True,
    )
    res = client.get("/ui/compaction")
    assert res.status_code == 200
    html = res.text
    assert "Compaction review" in html
    assert "merged 2 facts" in html
    assert rel in html


def test_compaction_lists_history_files(client):
    _write_file("decisions/x-history.md", "---\ncategory: decisions\n---\narchived\n")
    res = client.get("/ui/compaction")
    assert res.status_code == 200
    assert "decisions/x-history.md" in res.text


# ── 5. Quality ────────────────────────────────────────────────────────────────
def test_quality_lists_stale_and_orphaned(client):
    # Orphaned: a categorized file with no entities, unreferenced.
    _write_file("decisions/orphan.md", "---\ncategory: decisions\ntype: Decision\n---\nlonely")
    # Stale: active + last_updated > 90d ago.
    _write_file(
        "decisions/old.md",
        "---\ncategory: decisions\ntype: Decision\nstatus: active\n"
        "last_updated: 2020-01-01T00:00:00Z\n---\nancient",
    )
    res = client.get("/ui/quality")
    assert res.status_code == 200
    html = res.text
    assert "Quality queues" in html
    assert "Stale" in html and "Orphaned" in html
    assert "decisions/old.md" in html  # stale, links out
    assert "decisions/orphan.md" in html  # orphaned
    # New bucket present.
    assert "No extraction metadata" in html


def test_history_sibling_not_counted_and_not_flagged(client):
    """A -history.md sibling is not a browsable memory: it is excluded from the
    memory count (sidebar badge == dashboard MEMORIES card == list) and never
    appears in any Quality queue (finding 1).
    """
    # Two real memories...
    _write_file("decisions/a.md", "---\ncategory: decisions\ntype: Decision\ndescription: a\n---\nbody")
    _write_file("decisions/b.md", "---\ncategory: decisions\ntype: Decision\ndescription: b\n---\nbody")
    # ...plus a -history.md sibling (no description → lint would flag it).
    _write_file("decisions/a-history.md", "---\ncategory: decisions\n---\narchived\n")

    import re

    def _memories_card_num(html: str) -> str:
        # <div class="num">N</div>\n ... <div class="lbl">memories</div>
        m = re.search(r'<div class="num">([\d,]+)</div>\s*<div class="lbl">memories</div>', html)
        return m.group(1) if m else "??"

    def _sidebar_memory_badge(html: str) -> str:
        # <span>Memory</span><span class="n">N</span>
        m = re.search(r'<span>Memory</span><span class="n">([\d,]+)</span>', html)
        return m.group(1) if m else "??"

    # Dashboard MEMORIES card + sidebar badge reflect 2, not 3.
    dash = client.get("/ui").text
    assert _memories_card_num(dash) == "2"
    assert _sidebar_memory_badge(dash) == "2"
    assert "decisions/a-history.md" not in dash

    # Memory list also shows 2 and excludes the sibling.
    mem = client.get("/ui/memory").text
    assert "decisions/a.md" in mem and "decisions/b.md" in mem
    assert "decisions/a-history.md" not in mem
    assert _sidebar_memory_badge(mem) == "2"

    # Quality: the -history.md sibling is NOT in the No-description queue even
    # though it lacks a description (the bug this fixes).
    qual = client.get("/ui/quality").text
    assert "decisions/a-history.md" not in qual


def test_nav_links_wired_on_dashboard(client):
    """The previously-inert sidebar nav items now have hrefs.

    url_for renders absolute URLs (http://testserver/ui/...), so match the path
    with its closing quote rather than a leading-slash href.
    """
    res = client.get("/ui")
    assert res.status_code == 200
    html = res.text
    for path in ("/ui/memory", "/ui/diffs", "/ui/compaction", "/ui/quality"):
        assert f'{path}"' in html


# ── views.py unit checks (store-agnostic, weir-reusable) ──────────────────────
def test_build_memory_list_filters():
    from palinode.api.ui.views import build_memory_list

    rows = [
        {"path": "a.md", "id": "a", "name": "A", "type": "Decision", "category": "decisions",
         "core": True, "last_updated": "2026-06-01", "days_old": 1, "freshness": "fresh"},
        {"path": "b.md", "id": "b", "name": "B", "type": "Insight", "category": "insights",
         "core": False, "last_updated": "2020-01-01", "days_old": 999, "freshness": "stale"},
    ]
    all_ = build_memory_list(rows)
    assert all_["total"] == 2
    assert all_["types"] == ["Decision", "Insight"]

    decisions = build_memory_list(rows, type_filter="Decision")
    assert decisions["total"] == 1 and decisions["rows"][0]["id"] == "a"

    core = build_memory_list(rows, core_only=True)
    assert core["total"] == 1 and core["rows"][0]["id"] == "a"

    stale = build_memory_list(rows, freshness="stale")
    assert stale["total"] == 1 and stale["rows"][0]["id"] == "b"


def test_run_search_degrades_gracefully():
    from palinode.api.ui.views import run_search

    def boom(_q):
        raise RuntimeError("embedder down")

    out = run_search("anything", boom, lambda p: p)
    assert out["results"] == [] and out["count"] == 0
    assert out["error"] and "search unavailable" in out["error"]

    # Empty query → no search, no error.
    empty = run_search("  ", boom, lambda p: p)
    assert empty["error"] is None and empty["count"] == 0


def test_build_diffs_groups_by_day():
    from palinode.api.ui.views import build_diffs_view

    commits = [
        {"hash": "aaa", "date": "2026-06-16T10:00:00Z", "message": "m1", "files": ["x.md"]},
        {"hash": "bbb", "date": "2026-06-16T09:00:00Z", "message": "m2", "files": ["y.md", "z.md"]},
        # A commit that touched no memory files (only the index) is dropped.
        {"hash": "ccc", "date": "2026-06-15T08:00:00Z", "message": "m3", "files": [".palinode.db"]},
    ]
    view = build_diffs_view(commits, "stat", 14)
    assert view["commit_count"] == 2  # ccc dropped — no memory files
    assert [g["day"] for g in view["groups"]] == ["2026-06-16"]  # 06-15 empty → gone
    assert view["groups"][0]["commits"][1]["file_count"] == 2
    assert view["groups"][0]["commits"][0]["files"][0]["id"] == "x"


def test_build_diffs_scrubs_db_and_log_noise():
    """A commit's DB/journal/log files are scrubbed; .md changes survive (finding 2)."""
    from palinode.api.ui.views import build_diffs_view

    commits = [
        {
            "hash": "aaa", "date": "2026-06-16T10:00:00Z", "message": "save",
            "files": [
                "decisions/auth.md", ".palinode.db", ".palinode.db-journal",
                ".palinode.db-wal", "logs/operations.jsonl",
            ],
        },
    ]
    view = build_diffs_view(commits, "stat", 14)
    assert view["commit_count"] == 1
    files = [f["path"] for g in view["groups"] for c in g["commits"] for f in c["files"]]
    assert files == ["decisions/auth.md"]
    assert view["groups"][0]["commits"][0]["file_count"] == 1


def test_build_compaction_scrubs_db_and_log_noise():
    """Compaction pass file chips exclude DB/log noise (finding 2)."""
    from palinode.api.ui.views import build_compaction_view

    commits = [
        {
            "hash": "ccc", "date": "2026-06-16T03:00:00Z",
            "message": "palinode: compaction 2026-06-16 — merged",
            "files": ["decisions/a.md", ".palinode.db", "logs/operations.jsonl"],
        },
    ]
    view = build_compaction_view(commits, [], 90)
    assert view["pass_count"] == 1  # the pass itself is still listed
    files = [f["path"] for p in view["passes"] for f in p["files"]]
    assert files == ["decisions/a.md"]
    assert view["passes"][0]["file_count"] == 1


def test_is_browsable_memory_and_is_memory_file_predicates():
    from palinode.api.ui.views import is_browsable_memory, is_memory_file

    # browsable: a real .md memory, not a -history sibling, not a skip-dir
    assert is_browsable_memory("decisions/auth.md")
    assert not is_browsable_memory("decisions/jwt-auth-history.md")
    assert not is_browsable_memory("daily/2026-06-16.md")
    assert not is_browsable_memory("logs/x.md")
    assert not is_browsable_memory("archive/old.md")
    assert not is_browsable_memory(".palinode.db")

    # memory-file (for commit chips): keep any .md (incl. -history), drop infra
    assert is_memory_file("decisions/auth.md")
    assert is_memory_file("decisions/jwt-auth-history.md")
    assert not is_memory_file(".palinode.db")
    assert not is_memory_file(".palinode.db-journal")
    assert not is_memory_file(".palinode.db-wal")
    assert not is_memory_file(".palinode.db-shm")
    assert not is_memory_file("logs/operations.jsonl")


def test_build_quality_view_buckets():
    from palinode.api.ui.views import build_quality_view

    lint = {
        "stale_files": [{"file": "s.md", "days_old": 120}],
        "orphaned_files": [{"file": "o.md"}],
        "missing_descriptions": ["d.md"],
        "contradictions": [{"entity": "projects/p", "issue": "two active"}],
        "no_extraction_meta": [{"file": "s.md"}, {"file": "o.md"}],
    }
    view = build_quality_view(lint)
    keys = {q["key"]: q for q in view["queues"]}
    assert keys["stale"]["rows"][0]["id"] == "s"
    assert keys["stale"]["rows"][0]["detail"] == "120d old"
    assert keys["orphaned"]["rows"][0]["id"] == "o"
    assert keys["missing_description"]["rows"][0]["id"] == "d"
    assert keys["contradictions"]["rows"][0]["entity"] == "projects/p"
    assert keys["no_extraction_meta"]["rows"][0]["id"] == "s"


def test_recent_commits_repo_wide(tmp_path):
    """git_tools.recent_commits reads repo-wide history (read-only)."""
    import subprocess as sp
    from palinode.core.config import config as cfg
    from palinode.core import git_tools

    sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    sp.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    sp.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "a.md").write_text("one\n")
    sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    sp.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "palinode: compaction X — merged"], check=True)
    (tmp_path / "b.md").write_text("two\n")
    sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    sp.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "regular save"], check=True)

    old = cfg.memory_dir
    cfg.memory_dir = str(tmp_path)
    try:
        allc = git_tools.recent_commits(days=3650, limit=10)
        assert len(allc) == 2
        assert allc[0]["message"] == "regular save"  # newest first
        assert allc[0]["files"] == ["b.md"]
        only_compaction = git_tools.recent_commits(
            days=3650, limit=10, message_prefix="palinode: compaction"
        )
        assert len(only_compaction) == 1
        assert only_compaction[0]["files"] == ["a.md"]
    finally:
        cfg.memory_dir = old
