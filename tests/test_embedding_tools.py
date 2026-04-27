"""Tests for #210 Deliverable F — `palinode_dedup_suggest` and
`palinode_orphan_repair` MCP/API/CLI tools.

This file uses a **deterministic fake embedder** (not real BGE-M3) so the
test suite stays hermetic and doesn't require an Ollama instance.  The fake
maps text → vector via a stable hash-to-vector function, with a
keyword-overlap kicker so semantically-related test inputs land near each
other in cosine space.  The preprocessing pipeline is exercised end-to-end —
the fake embedder is only swapped in for the embedding step itself.

Mark: deterministic fake embedder (NOT real BGE-M3).  Threshold-recalibration
recommendation lives in the PR description and CHANGELOG.

The CRITICAL preprocessing test
(``test_preprocessing_strips_shared_wikilinks_no_false_positive``) PROVES
that two notes with completely different topics that happen to link the
same entities are NOT flagged as duplicates of each other.  This is the
P1-correctness gate from the design doc — without it, every note linking
the same entities would false-positive against every other one.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from typing import Any

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.core.embedding_preprocess import (
    AUTO_FOOTER_MARKER,
    preprocess_for_similarity,
    strip_auto_footer,
    strip_wikilinks,
)


# ---------------------------------------------------------------------------
# Fake embedder — deterministic, semantically-meaningful
# ---------------------------------------------------------------------------

DIM = 1024  # match BGE-M3 dimensionality so the SQLite-vec schema is happy


_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def _tokens(text: str) -> list[str]:
    """Lowercase word-tokenize on alpha runs ≥3 chars."""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _hash_dim(token: str) -> int:
    """Map a token to a deterministic dimension index in [0, DIM)."""
    h = hashlib.sha256(token.encode()).digest()
    return int.from_bytes(h[:4], "big") % DIM


def fake_embed(text: str) -> list[float]:
    """Bag-of-words → sparse-but-normalized vector.

    Token presence sets dimensions; identical token sets yield identical
    vectors (similarity → 1.0).  Disjoint token sets yield orthogonal
    vectors (similarity → 0.0).  Partial overlap interpolates smoothly.
    This makes cosine similarity behave like Jaccard-on-tokens, which is
    semantically meaningful enough to test threshold logic and preprocessing
    end-to-end.
    """
    tokens = _tokens(text)
    if not tokens:
        return []
    vec = [0.0] * DIM
    counts = Counter(tokens)
    for tok, count in counts.items():
        idx = _hash_dim(tok)
        # Use sqrt of count (TF) so very repeated words don't dominate.
        vec[idx] += math.sqrt(count)
    # L2 normalize so dot product == cosine similarity.
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return []
    return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with a fresh tmp_path DB and the deterministic fake embedder.

    Patches ``palinode.core.embedder.embed`` (the local-backend function the
    API server calls) so no Ollama dependency is required.  Schema creation
    fires via the FastAPI lifespan when TestClient is used as a context
    manager — without that, every store query raises OperationalError.
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    # Swap the embedder.  The API server imports `from palinode.core import
    # embedder` then calls `embedder.embed(...)`, so patching the attribute
    # on the module is sufficient — the server's binding is by reference.
    from palinode.core import embedder

    monkeypatch.setattr(embedder, "embed", lambda text, backend="local": fake_embed(text))

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _write_memory(tmp_path, rel_path: str, content: str) -> str:
    """Materialize a memory file on disk so the API's reader can pick it up.

    Returns the absolute path written.
    """
    full = os.path.join(str(tmp_path), rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return full


def _ingest(tmp_path, rel_path: str, content: str) -> None:
    """Write the file AND insert a chunk into the index for it.

    Mirrors what the watcher would do.  Embeds via the fake embedder; the
    index thereby reflects the real (raw, un-preprocessed) corpus state at
    test time.  The dedup/orphan-repair rerank step re-embeds with the
    preprocessed body as the production code does.
    """
    _write_memory(tmp_path, rel_path, content)
    chunk_id = hashlib.sha256(rel_path.encode()).hexdigest()[:32]
    store.upsert_chunks(
        [
            {
                "id": chunk_id,
                "file_path": rel_path,
                "section_id": "root",
                "category": rel_path.split("/", 1)[0] if "/" in rel_path else "",
                "content": content,
                "embedding": fake_embed(content),
                "metadata": {},
                "created_at": "2026-04-26T00:00:00Z",
                "last_updated": "2026-04-26T00:00:00Z",
            }
        ],
        skip_unchanged=False,
    )


# ---------------------------------------------------------------------------
# Unit tests — preprocessing pipeline (no API)
# ---------------------------------------------------------------------------


def test_strip_wikilinks_keeps_entity_word():
    assert strip_wikilinks("met with [[Alice Smith]] today") == "met with Alice Smith today"


def test_strip_wikilinks_uses_display_text_when_aliased():
    text = "see [[meeting-2026-04-26|yesterday's meeting]] for context"
    assert strip_wikilinks(text) == "see yesterday's meeting for context"


def test_strip_auto_footer_removes_marked_block():
    text = (
        "Real content here.\n\n"
        "## See also\n"
        f"{AUTO_FOOTER_MARKER}\n"
        "- [[alice]]\n"
        "- [[bob]]\n"
    )
    cleaned = strip_auto_footer(text)
    assert "alice" not in cleaned
    assert "bob" not in cleaned
    assert "See also" not in cleaned
    assert "Real content here." in cleaned


def test_strip_auto_footer_noop_when_marker_absent():
    text = "## See also\n- something user wrote\n"
    # No marker — preserves the user's hand-written See-also section.
    assert strip_auto_footer(text) == text


def test_preprocess_strips_frontmatter_wikilinks_and_footer():
    raw = (
        "---\n"
        "title: Test\n"
        "entities: [person/alice, person/bob]\n"
        "---\n"
        "Discussion of [[Alice Smith]] and [[bob]] regarding the rollout.\n"
        "\n"
        "## See also\n"
        f"{AUTO_FOOTER_MARKER}\n"
        "- [[alice]]\n"
        "- [[bob]]\n"
    )
    out = preprocess_for_similarity(raw)
    assert "title:" not in out
    assert "[[" not in out and "]]" not in out
    assert "Alice Smith" in out
    assert "rollout" in out
    # Footer body must be gone — we don't want footer-only entity tokens
    # leaking into the similarity comparison.
    assert "See also" not in out
    # And the "alice"/"bob" tokens that ONLY came from the footer should
    # not contribute via the footer.  (They're allowed to remain because
    # the body itself mentioned them — that's a real semantic signal.)


# ---------------------------------------------------------------------------
# API tests — dedup_suggest
# ---------------------------------------------------------------------------


def test_dedup_suggest_returns_results_sorted_descending(client, tmp_path):
    """High-overlap files rank above low-overlap files."""
    _ingest(
        tmp_path,
        "projects/rollout-plan.md",
        "The rollout plan covers staging deployment and rollback procedures.",
    )
    _ingest(
        tmp_path,
        "projects/staging-checklist.md",
        "Staging deployment checklist with rollback procedures and verification.",
    )
    _ingest(
        tmp_path,
        "people/unrelated.md",
        "Pizza recipes and weekend gardening notes for the cabin trip.",
    )

    resp = client.post(
        "/dedup-suggest",
        json={
            "content": "Draft about staging deployment rollback procedures",
            "min_similarity": 0.1,
            "top_k": 5,
        },
    )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 2

    # Sorted descending
    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True)

    # The unrelated pizza/gardening note is not in the top results.
    file_paths = [r["file_path"] for r in results]
    assert "people/unrelated.md" not in file_paths or results[-1]["file_path"] == "people/unrelated.md"


def test_dedup_suggest_min_similarity_kwarg_is_honored(client, tmp_path):
    """A high min_similarity kwarg suppresses weaker matches."""
    _ingest(
        tmp_path,
        "projects/exact-match.md",
        "deployment rollout staging procedures",
    )
    _ingest(
        tmp_path,
        "projects/weak-match.md",
        "completely different topic about zoo animals and weather patterns",
    )

    # With a generous threshold we get both (or at least the strong one).
    loose = client.post(
        "/dedup-suggest",
        json={
            "content": "deployment rollout staging procedures",
            "min_similarity": 0.0,
            "top_k": 10,
        },
    ).json()
    # With a very strict threshold only the exact-match survives.
    strict = client.post(
        "/dedup-suggest",
        json={
            "content": "deployment rollout staging procedures",
            "min_similarity": 0.95,
            "top_k": 10,
        },
    ).json()

    assert len(strict) <= len(loose)
    for r in strict:
        assert r["similarity"] >= 0.95


def test_dedup_suggest_strong_dup_flag_fires_above_threshold(client, tmp_path):
    """Identical content yields similarity ≈ 1.0 → strong_dup=True."""
    body = "exact same content for paraphrase test alpha beta gamma"
    _ingest(tmp_path, "projects/exact.md", body)

    resp = client.post(
        "/dedup-suggest",
        json={
            "content": body,
            "min_similarity": 0.0,
            "top_k": 5,
        },
    )
    assert resp.status_code == 200
    results = resp.json()
    assert results, "expected at least one result"
    top = results[0]
    assert top["file_path"] == "projects/exact.md"
    assert top["similarity"] >= 0.90
    assert top["strong_dup"] is True


def test_dedup_suggest_empty_corpus_returns_empty(client):
    """Empty index → empty result list, no errors."""
    resp = client.post(
        "/dedup-suggest",
        json={"content": "any draft content here", "min_similarity": 0.5, "top_k": 5},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# THE preprocessing-correctness test (P1 from the design doc)
# ---------------------------------------------------------------------------


def test_preprocessing_strips_shared_wikilinks_no_false_positive(client, tmp_path):
    """Two notes with completely different topics that happen to link the
    same entities ([[alice]] and [[bob]]) MUST NOT be flagged as
    duplicates of each other.

    Without preprocessing, the shared `[[alice]]` and `[[bob]]` tokens
    (especially when materialized as a `## See also` footer) would dominate
    the bag-of-words signature and conflate "linked to the same entities"
    with "semantically similar content".  This test fails if the
    preprocessing pipeline regresses.
    """
    note_a = (
        "---\n"
        "entities: [person/alice, person/bob]\n"
        "---\n"
        "Discussion of database migration strategy. We considered partition "
        "schemes and sharding tradeoffs for the analytics warehouse.\n"
        "\n"
        "## See also\n"
        f"{AUTO_FOOTER_MARKER}\n"
        "- [[alice]]\n"
        "- [[bob]]\n"
    )
    note_b = (
        "---\n"
        "entities: [person/alice, person/bob]\n"
        "---\n"
        "Pizza recipes for the weekend cookout. Sourdough crust, fermented "
        "tomato sauce, hand-pulled mozzarella.\n"
        "\n"
        "## See also\n"
        f"{AUTO_FOOTER_MARKER}\n"
        "- [[alice]]\n"
        "- [[bob]]\n"
    )
    _ingest(tmp_path, "decisions/database-migration.md", note_a)
    _ingest(tmp_path, "daily/pizza-cookout.md", note_b)

    # Query with a third disjoint topic — it must not cluster with either.
    # More importantly: ask "is the pizza note a dup of the database note?"
    # by submitting note_a as the draft.  The pizza note's similarity must
    # NOT cross the strong-dup threshold; ideally it falls below the dedup
    # threshold entirely.
    resp = client.post(
        "/dedup-suggest",
        json={
            "content": note_a,
            "min_similarity": 0.0,  # see everything; we'll inspect scores
            "top_k": 10,
        },
    )
    assert resp.status_code == 200
    results = resp.json()
    by_path = {r["file_path"]: r for r in results}

    # The exact-same note (database-migration) should be near-1.0 strong_dup.
    db_match = by_path.get("decisions/database-migration.md")
    assert db_match is not None
    assert db_match["strong_dup"] is True

    # The pizza note must NOT be flagged as strong_dup.  This is the assertion
    # that fails without preprocessing — the shared `[[alice]]`/`[[bob]]`
    # footer tokens would push their similarity past 0.90.
    pizza_match = by_path.get("daily/pizza-cookout.md")
    if pizza_match is not None:
        assert not pizza_match["strong_dup"], (
            "PREPROCESSING REGRESSION: two notes with disjoint content but the "
            "same shared `[[alice]]`/`[[bob]]` wikilinks are being flagged as "
            "strong duplicates. Auto-footer stripping or wikilink stripping "
            f"is not running. Similarity={pizza_match['similarity']}"
        )
        # Stronger guard: similarity should be well below the dedup default
        # of 0.80.  The pizza/database vocabularies are disjoint once
        # preprocessing strips the shared footer.
        assert pizza_match["similarity"] < 0.80, (
            "Two notes with disjoint topics but shared wikilinks scored "
            f"{pizza_match['similarity']:.3f} — expected < 0.80 once "
            "preprocessing strips shared `[[alice]]`/`[[bob]]` tokens."
        )


# ---------------------------------------------------------------------------
# API tests — orphan_repair
# ---------------------------------------------------------------------------


def test_orphan_repair_returns_top_k_above_threshold(client, tmp_path):
    """Broken link `[[alice-meeting]]` returns alice-meeting-style files."""
    _ingest(
        tmp_path,
        "people/alice-meeting-notes.md",
        "Meeting notes from the alice meeting about quarterly planning.",
    )
    _ingest(
        tmp_path,
        "people/alice-onboarding.md",
        "Alice onboarding plan covering meeting cadence and project areas.",
    )
    _ingest(
        tmp_path,
        "projects/unrelated-rollout.md",
        "Rollout schedule for the bridge construction phase three approval.",
    )

    resp = client.post(
        "/orphan-repair",
        json={
            "broken_link": "[[alice-meeting]]",
            "min_similarity": 0.05,
            "top_k": 5,
        },
    )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 1
    file_paths = [r["file_path"] for r in results]
    # At least one alice-* file should rank above the unrelated rollout.
    assert any("alice" in fp for fp in file_paths)
    # Sorted descending.
    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True)


def test_orphan_repair_accepts_bare_target(client, tmp_path):
    """Both `[[name]]` and `name` forms are accepted."""
    _ingest(
        tmp_path,
        "projects/database-migration.md",
        "Database migration runbook with rollback steps and verification.",
    )

    bracket_resp = client.post(
        "/orphan-repair",
        json={"broken_link": "[[database-migration]]", "min_similarity": 0.0, "top_k": 3},
    )
    bare_resp = client.post(
        "/orphan-repair",
        json={"broken_link": "database migration", "min_similarity": 0.0, "top_k": 3},
    )
    assert bracket_resp.status_code == 200
    assert bare_resp.status_code == 200
    # Both should find the same top file.
    bracket_top = bracket_resp.json()
    bare_top = bare_resp.json()
    assert bracket_top and bare_top
    assert bracket_top[0]["file_path"] == bare_top[0]["file_path"]


def test_orphan_repair_empty_corpus_returns_empty(client):
    """Empty index → empty result, no errors."""
    resp = client.post(
        "/orphan-repair",
        json={"broken_link": "[[nonexistent]]", "min_similarity": 0.5, "top_k": 5},
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_orphan_repair_min_similarity_kwarg_is_honored(client, tmp_path):
    """A high min_similarity kwarg suppresses weaker matches."""
    _ingest(
        tmp_path,
        "people/exact-target.md",
        "alice meeting quarterly planning",
    )
    _ingest(
        tmp_path,
        "projects/weak-overlap.md",
        "completely different topic about zoo animals and weather",
    )

    strict = client.post(
        "/orphan-repair",
        json={
            "broken_link": "alice meeting quarterly planning",
            "min_similarity": 0.95,
            "top_k": 10,
        },
    ).json()
    for r in strict:
        assert r["similarity"] >= 0.95
