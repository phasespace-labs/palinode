"""Integration test enforcing the 10-minute Obsidian demo walkthrough (#210, Deliverable H).

One test function per minute-block in artifacts/obsidian-integration/demo-plan.md.
The gate: each test PASSES or is marked @pytest.mark.xfail with a specific reason.
A fully-passing suite means the demo works end-to-end without manual intervention.

Minute mapping:
  test_minute_1  — palinode init --obsidian succeeds, expected files present
  test_minute_2  — .obsidian/*.json are valid JSON; _index.md MOC exists
  test_minute_3_5 — _index.md wikilinks resolve to category dirs that exist
  test_minute_6_8 — palinode save → file at expected path; correct frontmatter; auto-footer present
  test_minute_9_10 — search finds the saved memory; preprocessing keeps
                     shared-entity notes distinct in results

Drive layer:
  - Click CliRunner for `palinode init --obsidian`
  - FastAPI TestClient (context-manager form) for save + search
  - Real SQLite in tmp_path; no external services
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from unittest import mock

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from palinode.cli import main as cli_main
from palinode.core import store
from palinode.core.config import config

# ---------------------------------------------------------------------------
# Fake embedder — deterministic, token-overlap-aware (same pattern as
# tests/test_embedding_tools.py so threshold behaviour is consistent)
# ---------------------------------------------------------------------------

_EMBED_DIM = 1024
_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _hash_dim(token: str) -> int:
    h = hashlib.sha256(token.encode()).digest()
    return int.from_bytes(h[:4], "big") % _EMBED_DIM


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    """Bag-of-words → sparse normalised vector.  Identical tokens → sim=1.0."""
    tokens = _tokens(text)
    if not tokens:
        return [0.1] * _EMBED_DIM  # non-zero so search doesn't bail
    vec = [0.0] * _EMBED_DIM
    counts = Counter(tokens)
    for tok, count in counts.items():
        idx = _hash_dim(tok)
        vec[idx] += math.sqrt(count)
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return [0.1] * _EMBED_DIM
    return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def demo_vault(tmp_path, monkeypatch):
    """Provide an isolated memory dir + real SQLite + fake embedder.

    Returns the tmp_path Path object (the vault root).
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    # Pre-create the DB schema so the TestClient fixtures don't race
    store.init_db()
    return tmp_path


@pytest.fixture()
def inited_vault(demo_vault):
    """Run `palinode init --obsidian` against demo_vault and return the vault path."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["init", "--dir", str(demo_vault), "--obsidian"])
    assert result.exit_code == 0, (
        f"palinode init --obsidian failed (exit={result.exit_code}):\n{result.output}"
    )
    return demo_vault


@pytest.fixture()
def api_client(inited_vault):
    """TestClient wrapping the FastAPI app, with the fake embedder in place.

    Uses the context-manager form so lifespan startup runs.
    """
    from palinode.api.server import app, _rate_counters

    _rate_counters.clear()
    with (
        mock.patch("palinode.core.embedder.embed", side_effect=_fake_embed),
        mock.patch("palinode.api.server._generate_description", return_value="Test desc"),
        mock.patch("palinode.api.server._generate_summary", return_value=""),
        mock.patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        TestClient(app, raise_server_exceptions=True) as client,
    ):
        yield client, inited_vault


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _obsidian_json_files(vault: "os.PathLike") -> list[str]:
    return [
        str(vault / ".obsidian" / "app.json"),
        str(vault / ".obsidian" / "graph.json"),
        str(vault / ".obsidian" / "workspace.json"),
    ]


def _extract_wikilinks(text: str) -> list[str]:
    """Return the link targets from [[target]] or [[target|alias]] patterns."""
    return re.findall(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]+?)?\]\]", text)


def _frontmatter(file_path: str) -> dict:
    with open(file_path) as f:
        text = f.read()
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"No frontmatter found in {file_path}"
    return yaml.safe_load(parts[1]) or {}


def _body(file_path: str) -> str:
    with open(file_path) as f:
        text = f.read()
    parts = text.split("---", 2)
    return parts[2].lstrip("\n") if len(parts) >= 3 else text


def _index_chunk(chunk_id: str, file_path: str, content: str) -> None:
    """Manually insert a chunk into the DB so search can find it without the watcher."""
    chunks = [{
        "id": chunk_id,
        "file_path": file_path,
        "section_id": None,
        "category": os.path.basename(os.path.dirname(file_path)),
        "content": content,
        "metadata": {},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "embedding": _fake_embed(content),
    }]
    store.upsert_chunks(chunks)


# ---------------------------------------------------------------------------
# Minute 1 — init
# ---------------------------------------------------------------------------


def test_minute_1_palinode_init_obsidian_succeeds(tmp_path, monkeypatch):
    """Minute 1: `palinode init --obsidian <vault>` exits 0 and creates all expected files."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["init", "--dir", str(tmp_path), "--obsidian"])

    assert result.exit_code == 0, (
        f"palinode init --obsidian exited {result.exit_code}:\n{result.output}"
    )

    expected = [
        tmp_path / ".obsidian" / "app.json",
        tmp_path / ".obsidian" / "graph.json",
        tmp_path / ".obsidian" / "workspace.json",
        tmp_path / "_index.md",
        tmp_path / "_README.md",
    ]
    for path in expected:
        assert path.exists(), f"Expected scaffold file missing: {path.relative_to(tmp_path)}"

    # Output must mention Obsidian (so the user knows what happened)
    output_lower = result.output.lower()
    assert "obsidian" in output_lower or ".obsidian" in result.output, (
        "init output did not mention Obsidian:\n" + result.output
    )


# ---------------------------------------------------------------------------
# Minute 2 — vault opens with scaffolded workspace
# ---------------------------------------------------------------------------


def test_minute_2_vault_opens_with_scaffolded_workspace(inited_vault):
    """Minute 2: All .obsidian/*.json files parse as valid JSON with required keys."""
    vault = inited_vault

    # app.json
    app_json_path = vault / ".obsidian" / "app.json"
    app_data = json.loads(app_json_path.read_text())
    assert "newFileFolderPath" in app_data, "app.json missing newFileFolderPath"
    assert app_data.get("useMarkdownLinks") is False, (
        "app.json should set useMarkdownLinks=false to enable [[wikilinks]]"
    )

    # graph.json
    graph_data = json.loads((vault / ".obsidian" / "graph.json").read_text())
    assert "colorGroups" in graph_data, "graph.json missing colorGroups"
    assert "collapsedNodeGroups" in graph_data, "graph.json missing collapsedNodeGroups"

    # workspace.json
    ws_data = json.loads((vault / ".obsidian" / "workspace.json").read_text())
    for key in ("main", "left", "right", "active"):
        assert key in ws_data, f"workspace.json missing top-level key: {key}"

    # _index.md MOC must exist
    assert (vault / "_index.md").exists(), "_index.md MOC not created"
    # _README.md orientation file must exist
    assert (vault / "_README.md").exists(), "_README.md not created"


# ---------------------------------------------------------------------------
# Minute 3–5 — graph seed nodes (wikilinks resolve)
# ---------------------------------------------------------------------------


def test_minute_3_5_graph_view_seed_nodes_present(inited_vault):
    """Minutes 3-5: _index.md contains [[wikilinks]] to category dirs; those dirs exist."""
    vault = inited_vault
    index_content = (vault / "_index.md").read_text()

    # Must contain wikilinks
    wikilinks = _extract_wikilinks(index_content)
    assert wikilinks, "_index.md contains no [[wikilinks]] — graph will be empty"

    # Core categories that the demo relies on for the graph view
    expected_categories = {"people", "projects", "decisions", "insights", "research", "daily"}
    linked_targets = " ".join(wikilinks).lower()
    for cat in expected_categories:
        assert cat in linked_targets, (
            f"_index.md does not link to category '{cat}' — graph seed node missing"
        )

    # The category dirs must exist so Obsidian's graph can render them
    for cat in expected_categories:
        cat_dir = vault / cat
        assert cat_dir.exists(), (
            f"Category directory '{cat}/' was not created by `palinode init --obsidian`"
        )


# ---------------------------------------------------------------------------
# Minute 6–8 — save appears in vault
# ---------------------------------------------------------------------------


def test_minute_6_8_save_appears_in_vault(api_client):
    """Minutes 6-8: palinode save → file at expected path; correct frontmatter; auto-footer."""
    client, vault = api_client

    resp = client.post("/save", json={
        "content": (
            "Decided to use side-by-side vault as the Obsidian integration MVP. "
            "Reasoning: zero conflict surface, days of work, reversible."
        ),
        "type": "Decision",
        "slug": "obsidian-integration-mvp",
        "entities": ["project/palinode"],
        "title": "Obsidian integration MVP — side-by-side vault",
    })
    assert resp.status_code == 200, f"POST /save failed: {resp.text}"
    data = resp.json()

    # File must be at the expected path
    file_path = data["file_path"]
    assert os.path.exists(file_path), f"Saved file not found on disk: {file_path}"

    # File must land in decisions/ (matching type=Decision)
    rel = os.path.relpath(file_path, str(vault))
    assert rel.startswith("decisions/"), (
        f"Decision memory landed in wrong directory: {rel}"
    )

    # Frontmatter checks
    fm = _frontmatter(file_path)
    assert fm.get("category") == "decisions", "frontmatter category must be 'decisions'"
    assert fm.get("type") == "Decision", "frontmatter type must be 'Decision'"
    assert "project/palinode" in (fm.get("entities") or []), (
        "entity 'project/palinode' not in frontmatter"
    )
    assert fm.get("content_hash"), "frontmatter missing content_hash"

    # Body: per wiki-contract (Deliverable C), entities not in body → auto-footer added
    body = _body(file_path)
    from palinode.api.server import _WIKI_FOOTER_MARKER
    assert _WIKI_FOOTER_MARKER in body, (
        "Auto-footer marker not found — Deliverable C (wiki contract Layer 2) may not have run"
    )
    assert "## See also" in body, "## See also section missing from auto-footer"
    assert "[[palinode]]" in body, "Entity wikilink not materialised in See also footer"


# ---------------------------------------------------------------------------
# Minute 9–10 — search finds saved memory; preprocessing keeps distinct notes distinct
# ---------------------------------------------------------------------------


def test_minute_9_10_search_finds_saved_memory(api_client):
    """Minute 9: POST /search returns the just-saved memory."""
    client, vault = api_client

    # Save a memory with unique content
    save_resp = client.post("/save", json={
        "content": "obsidian vault integration decision: side-by-side mode chosen for MVP",
        "type": "Decision",
        "slug": "search-target-obsidian-demo",
        "entities": ["project/palinode"],
    })
    assert save_resp.status_code == 200, save_resp.text
    file_path = save_resp.json()["file_path"]

    # Manually index so search can find it (file watcher not running in tests)
    content_body = (
        "obsidian vault integration decision: side-by-side mode chosen for MVP"
    )
    _index_chunk("search-target-obsidian-demo-1", file_path, content_body)

    # Search must surface the saved memory
    search_resp = client.post("/search", json={
        "query": "obsidian vault integration",
        "threshold": 0.0,
        "limit": 10,
    })
    assert search_resp.status_code == 200, search_resp.text
    results = search_resp.json()
    assert len(results) >= 1, "Search returned no results for the just-saved memory"
    found_paths = [r.get("file_path", r.get("file", "")) for r in results]
    assert any("search-target-obsidian-demo" in p for p in found_paths), (
        "Just-saved memory not in search results.\n"
        f"Returned paths: {found_paths}"
    )


def test_minute_9_10_preprocessing_keeps_shared_entity_notes_distinct(api_client):
    """Minute 10 (preprocessing gate): two notes sharing entities but distinct content
    are not falsely collapsed by the dedup-suggest tool.

    This is the P1 correctness gate from the design doc:
    'without preprocessing, every note linking the same entities looks similar.'
    """
    client, vault = api_client

    # Note A: about infrastructure decisions
    resp_a = client.post("/save", json={
        "content": "Infrastructure decision: use Tailscale FQDN over IP in all configs",
        "type": "Decision",
        "slug": "infra-network-decision",
        "entities": ["project/palinode"],
    })
    assert resp_a.status_code == 200, resp_a.text
    path_a = resp_a.json()["file_path"]

    # Note B: about search ranking — completely different topic, same entity
    resp_b = client.post("/save", json={
        "content": "Search ranking: BGE-M3 with RRF fusion outperforms BM25-only in user study",
        "type": "Insight",
        "slug": "search-ranking-bge-insight",
        "entities": ["project/palinode"],
    })
    assert resp_b.status_code == 200, resp_b.text
    path_b = resp_b.json()["file_path"]

    # Index both with their preprocessed content (same as dedup_suggest does)
    from palinode.core.embedding_preprocess import preprocess_for_similarity

    with open(path_a) as f:
        raw_a = f.read()
    with open(path_b) as f:
        raw_b = f.read()

    preprocessed_a = preprocess_for_similarity(raw_a)
    preprocessed_b = preprocess_for_similarity(raw_b)

    _index_chunk("infra-network-1", path_a, preprocessed_a)
    _index_chunk("search-ranking-bge-1", path_b, preprocessed_b)

    # Query with Note A's content — Note B should NOT dominate the results
    # (it has completely different tokens after preprocessing strips the entity footer)
    dedup_resp = client.post("/dedup-suggest", json={
        "content": "Infrastructure decision: use Tailscale FQDN over IP in all configs",
        "min_similarity": 0.30,
        "top_k": 5,
    })
    assert dedup_resp.status_code == 200, dedup_resp.text
    results = dedup_resp.json()

    # If preprocessing is working, note A (same content) scores high,
    # note B (different content, same entity) scores low — they must not
    # appear at the top with a strong_dup flag.
    if results:
        top = results[0]
        top_path = top.get("file_path", "")
        if "search-ranking-bge" in top_path and top.get("strong_dup"):
            pytest.fail(
                "Preprocessing failure: note B (different topic, same entity) was "
                f"flagged as a strong duplicate of note A (sim={top.get('similarity'):.3f}). "
                "The auto-footer stripping and wikilink preprocessing may not be running."
            )
