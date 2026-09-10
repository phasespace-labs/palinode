"""
Tests for palinode_depends — milestone dependency modeling.

Covers:
- traverse_depends: empty deps, chains, unblocked/blocked, orphans
- find_unblocked: returns only items ready to start
- CLI: palinode depends command (via CLI runner with patched api_client methods)
- API: the two routes, and that the sibling route stays a sibling

All tests use tmp_path with real markdown files — no mocking of the filesystem.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from palinode.core.depends import traverse_depends, find_unblocked


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_md(directory: Path, filename: str, frontmatter: dict, body: str = "") -> Path:
    """Write a minimal memory file with the given frontmatter."""
    import yaml

    fm_text = yaml.safe_dump(frontmatter, default_flow_style=False, allow_unicode=True)
    content = f"---\n{fm_text}---\n\n{body}\n"
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# traverse_depends — basic cases
# ─────────────────────────────────────────────────────────────────────────────


def test_traverse_no_deps(tmp_path):
    """Memory with no dependency fields → empty arrays, unblocked=True."""
    _write_md(tmp_path, "a.md", {"slug": "milestone/A", "type": "ProjectSnapshot"})
    result = traverse_depends("milestone/A", memory_dir=str(tmp_path))

    assert result["slug"] == "milestone/A"
    assert result["depends_on"] == []
    assert result["blocks"] == []
    assert result["parallel_with"] == []
    assert result["unblocked"] is True
    assert result["orphans"] == []


def test_traverse_chain_a_depends_b_depends_c(tmp_path):
    """A depends_on B, B depends_on C — traversing A returns its direct deps."""
    _write_md(tmp_path, "c.md", {"slug": "milestone/C", "status": "done"})
    _write_md(tmp_path, "b.md", {"slug": "milestone/B", "depends_on": ["milestone/C"], "status": "in_progress"})
    _write_md(tmp_path, "a.md", {"slug": "milestone/A", "depends_on": ["milestone/B"]})

    result = traverse_depends("milestone/A", memory_dir=str(tmp_path))

    assert result["slug"] == "milestone/A"
    dep_slugs = [e["slug"] for e in result["depends_on"]]
    assert "milestone/B" in dep_slugs


def test_traverse_all_done_dependencies_unblocked(tmp_path):
    """All depends_on are status=done → unblocked=True."""
    _write_md(tmp_path, "dep1.md", {"slug": "task/dep1", "status": "done"})
    _write_md(tmp_path, "dep2.md", {"slug": "task/dep2", "status": "done"})
    _write_md(
        tmp_path,
        "main.md",
        {"slug": "milestone/main", "depends_on": ["task/dep1", "task/dep2"]},
    )

    result = traverse_depends("milestone/main", memory_dir=str(tmp_path))

    assert result["unblocked"] is True
    assert all(e["found"] for e in result["depends_on"])
    assert all(e["status"] == "done" for e in result["depends_on"])


def test_traverse_one_in_progress_dep_blocked(tmp_path):
    """One in_progress dependency → unblocked=False."""
    _write_md(tmp_path, "done.md", {"slug": "task/done", "status": "done"})
    _write_md(tmp_path, "wip.md", {"slug": "task/wip", "status": "in_progress"})
    _write_md(
        tmp_path,
        "main.md",
        {"slug": "milestone/main", "depends_on": ["task/done", "task/wip"]},
    )

    result = traverse_depends("milestone/main", memory_dir=str(tmp_path))

    assert result["unblocked"] is False


def test_traverse_orphan_reference(tmp_path):
    """Reference to a slug with no matching memory → shows up in orphans."""
    _write_md(
        tmp_path,
        "a.md",
        {"slug": "milestone/A", "depends_on": ["milestone/MISSING"]},
    )

    result = traverse_depends("milestone/A", memory_dir=str(tmp_path))

    assert "milestone/MISSING" in result["orphans"]
    # The orphan entry is not found
    dep = next(e for e in result["depends_on"] if e["slug"] == "milestone/MISSING")
    assert dep["found"] is False
    assert dep["status"] is None


def test_traverse_slug_not_found_returns_orphan_self(tmp_path):
    """Traversing a slug with no file → orphans contains the slug itself."""
    result = traverse_depends("milestone/NONEXISTENT", memory_dir=str(tmp_path))

    assert result["slug"] == "milestone/NONEXISTENT"
    assert "milestone/NONEXISTENT" in result["orphans"]
    assert result["depends_on"] == []
    assert result["unblocked"] is True  # vacuously unblocked


def test_traverse_blocks_and_parallel(tmp_path):
    """blocks and parallel_with entries are correctly returned."""
    _write_md(tmp_path, "b.md", {"slug": "milestone/B", "status": "in_progress"})
    _write_md(tmp_path, "c.md", {"slug": "milestone/C", "status": "in_progress"})
    _write_md(
        tmp_path,
        "a.md",
        {
            "slug": "milestone/A",
            "blocks": ["milestone/B"],
            "parallel_with": ["milestone/C"],
        },
    )

    result = traverse_depends("milestone/A", memory_dir=str(tmp_path))

    assert any(e["slug"] == "milestone/B" for e in result["blocks"])
    assert any(e["slug"] == "milestone/C" for e in result["parallel_with"])


# ─────────────────────────────────────────────────────────────────────────────
# find_unblocked
# ─────────────────────────────────────────────────────────────────────────────


def test_find_unblocked_includes_no_deps(tmp_path):
    """Slug with no depends_on is included in unblocked (unconstrained)."""
    _write_md(tmp_path, "a.md", {"slug": "milestone/A", "status": "in_progress"})
    result = find_unblocked(memory_dir=str(tmp_path))
    slugs = [r["slug"] for r in result]
    assert "milestone/A" in slugs


def test_find_unblocked_excludes_done(tmp_path):
    """Slug with status=done is excluded from unblocked list."""
    _write_md(tmp_path, "done.md", {"slug": "milestone/done", "status": "done"})
    result = find_unblocked(memory_dir=str(tmp_path))
    slugs = [r["slug"] for r in result]
    assert "milestone/done" not in slugs


def test_find_unblocked_excludes_blocked(tmp_path):
    """Slug with an in_progress depends_on is NOT in unblocked."""
    _write_md(tmp_path, "dep.md", {"slug": "task/dep", "status": "in_progress"})
    _write_md(
        tmp_path,
        "main.md",
        {"slug": "milestone/main", "depends_on": ["task/dep"], "status": "in_progress"},
    )
    result = find_unblocked(memory_dir=str(tmp_path))
    slugs = [r["slug"] for r in result]
    assert "milestone/main" not in slugs


def test_find_unblocked_returns_when_all_deps_done(tmp_path):
    """Slug is in unblocked when every depends_on is done."""
    _write_md(tmp_path, "dep.md", {"slug": "task/dep", "status": "done"})
    _write_md(
        tmp_path,
        "main.md",
        {"slug": "milestone/main", "depends_on": ["task/dep"], "status": "in_progress"},
    )
    result = find_unblocked(memory_dir=str(tmp_path))
    slugs = [r["slug"] for r in result]
    assert "milestone/main" in slugs


def test_find_unblocked_mixed_scenario(tmp_path):
    """Multi-item scenario: only the ready items are returned."""
    # A: done — excluded
    _write_md(tmp_path, "a.md", {"slug": "milestone/A", "status": "done"})
    # B: depends on A (done) → unblocked
    _write_md(
        tmp_path,
        "b.md",
        {"slug": "milestone/B", "depends_on": ["milestone/A"], "status": "in_progress"},
    )
    # C: depends on B (in_progress) → blocked
    _write_md(
        tmp_path,
        "c.md",
        {"slug": "milestone/C", "depends_on": ["milestone/B"], "status": "in_progress"},
    )
    # D: no deps → unblocked
    _write_md(tmp_path, "d.md", {"slug": "task/D", "status": "in_progress"})

    result = find_unblocked(memory_dir=str(tmp_path))
    slugs = [r["slug"] for r in result]

    assert "milestone/A" not in slugs   # done
    assert "milestone/B" in slugs       # deps done
    assert "milestone/C" not in slugs   # dep in_progress → blocked
    assert "task/D" in slugs            # no deps


# ─────────────────────────────────────────────────────────────────────────────
# CLI: palinode depends
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_depends_json_output():
    """palinode depends <slug> --format json returns expected structure via API."""
    from palinode.cli import main as cli_main

    payload = {
        "slug": "milestone/m1",
        "depends_on": [],
        "blocks": [],
        "parallel_with": [],
        "unblocked": True,
        "orphans": [],
    }

    with patch("palinode.cli._api.api_client.depends", return_value=payload):
        runner = CliRunner()
        result = runner.invoke(cli_main, ["depends", "milestone/m1", "--format", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["slug"] == "milestone/m1"
    assert data["unblocked"] is True


def test_cli_depends_unblocked_flag():
    """palinode depends --unblocked lists ready items."""
    from palinode.cli import main as cli_main

    payload = [{"slug": "milestone/A", "status": "in_progress", "file_path": "/x/a.md"}]

    with patch("palinode.cli._api.api_client.depends_unblocked", return_value=payload):
        runner = CliRunner()
        result = runner.invoke(cli_main, ["depends", "--unblocked"])

    assert result.exit_code == 0, result.output
    assert "milestone/A" in result.output


def test_cli_depends_no_args_error():
    """palinode depends with no args and no --unblocked exits with error."""
    from palinode.cli import main as cli_main

    runner = CliRunner()
    result = runner.invoke(cli_main, ["depends"])

    assert result.exit_code != 0


# ─────────────────────────────────────────────────────────────────────────────
# API: GET /depends/{slug:path} and its sibling GET /depends/_unblocked
# ─────────────────────────────────────────────────────────────────────────────
#
# The registry declares the sibling route as how the ``api`` surface realizes
# the canonical ``unblocked`` param (``surface_realizations`` in
# ``palinode/core/parity.py``).  These pin the two response shapes so that
# declaration keeps describing something true.


def _api_client(tmp_path, monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    from palinode.core.config import config

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE", "PALINODE_API_HOST"):
        monkeypatch.delenv(_k, raising=False)
    # The memory files below are written before the API starts, so the
    # startup guard would otherwise read a populated dir with no database as a
    # misconfiguration and refuse to serve.  Here the fresh DB is the point.
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    import palinode.api.server as srv

    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    return TestClient(srv.app, raise_server_exceptions=True)


def test_api_unblocked_route_returns_a_list(tmp_path, monkeypatch):
    """``GET /depends/_unblocked`` answers with the ready list, not a slug view.

    Declared before ``GET /depends/{slug:path}`` in the router, so a reorder
    would make ``_unblocked`` match as a slug and return a dict.  That is the
    failure this pins: the assertion is on the shape, not just the status.
    """
    _write_md(tmp_path, "a.md", {"slug": "milestone/A", "status": "in_progress"})

    with _api_client(tmp_path, monkeypatch) as client:
        response = client.get("/depends/_unblocked")

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    assert [item["slug"] for item in body] == ["milestone/A"]
    assert set(body[0]) == {"slug", "status", "file_path"}


def test_api_depends_route_returns_the_neighbourhood(tmp_path, monkeypatch):
    """``GET /depends/{slug}`` answers with the dict view for one slug."""
    _write_md(tmp_path, "a.md", {"slug": "milestone/A", "status": "done"})
    _write_md(tmp_path, "b.md", {"slug": "milestone/B", "depends_on": ["milestone/A"]})

    with _api_client(tmp_path, monkeypatch) as client:
        response = client.get("/depends/milestone/B")

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert body["slug"] == "milestone/B"
    assert body["unblocked"] is True
    assert [dep["slug"] for dep in body["depends_on"]] == ["milestone/A"]
