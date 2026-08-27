"""Tests for session-end semantic dedup.

Option (a): when ``palinode_session_end`` is called and a recently indexed
save has near-identical content, skip the individual file write but still
append to daily/ and the project-status file.

These tests exercise the real DB + helper path (no mocks for the store) and
patch only the embedder to keep the suite Ollama-free.
"""
from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from palinode.core import store
from palinode.core.config import config
from tests._store_helpers import upsert_chunks


EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    """L2-normalize so cosine similarity equals dot product (matches BGE-M3)."""
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0:
        return vec
    return [x / n for x in vec]


def _orthogonal_embedding(seed: int) -> list[float]:
    """Make a near-orthogonal unit vector keyed by ``seed`` (deterministic)."""
    vec = [0.0] * EMBED_DIM
    # Spread mass across a small set of indices so two different seeds produce
    # vectors with nearly-zero cosine overlap.
    primary = seed % EMBED_DIM
    secondary = (seed * 7 + 3) % EMBED_DIM
    vec[primary] = 0.9
    vec[secondary] = 0.4
    return _normalize(vec)


def _matching_embedding() -> list[float]:
    """A canonical "session content" embedding, shared by both prior + new save."""
    return _orthogonal_embedding(42)


def _index_prior_save(
    *,
    file_path: str,
    slug_id: str,
    content: str,
    embedding: list[float],
    mtime: datetime | None = None,
) -> None:
    """Create a markdown file on disk + a chunks/chunks_vec row for it.

    The dedup helper uses file mtime as the recency signal, so we touch the
    file and (optionally) backdate it via ``os.utime``.
    """
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(file_path, (ts, ts))

    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    upsert_chunks([{
        "id": slug_id,
        "file_path": file_path,
        "section_id": None,
        "category": "projects",
        "content": content,
        "metadata": {},
        "created_at": now_iso,
        "last_updated": now_iso,
        "embedding": embedding,
    }])


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point config at a fresh tmp directory and init a real SQLite DB.

    No git, no real Ollama; the embedder gets patched per-test so each test
    can dictate the similarity outcome it wants to assert against.
    """
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    for d in ("projects", "insights", "daily"):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)
    store.init_db()
    yield memory_dir


def _count_individual_session_files(memory_dir: str) -> int:
    projects_dir = os.path.join(memory_dir, "projects")
    if not os.path.isdir(projects_dir):
        return 0
    return sum(1 for f in os.listdir(projects_dir) if f.startswith("session-end-"))


def test_below_threshold_writes_both_files(_isolated_env):
    """A recent save on a different topic must NOT trigger dedup — both files written."""
    memory_dir = _isolated_env

    # Pre-index a save on a totally different topic
    _index_prior_save(
        file_path=os.path.join(memory_dir, "projects", "old-topic.md"),
        slug_id="projects-old-topic",
        content="Worked on entity normalization rules",
        embedding=_orthogonal_embedding(1),
    )

    # The new session-end content is on yet another topic — embeddings disagree
    fake_new = _orthogonal_embedding(99)

    with (
        mock.patch("palinode.core.embedder.embed", return_value=fake_new),
        mock.patch("palinode.api.server._generate_description", return_value="Different topic"),
    ):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(
            summary="Shipped semantic dedup for session-end",
            project="palinode",
            source="test",
        )
        result = session_end_api(req)

    # Both files written; no dedup metadata in response
    assert result.get("individual_file") is not None
    assert os.path.exists(result["individual_file"])
    assert "deduplicated_against" not in result
    daily_path = os.path.join(memory_dir, result["daily_file"])
    assert os.path.exists(daily_path)
    assert _count_individual_session_files(memory_dir) == 1


def test_above_threshold_skips_individual_file(_isolated_env):
    """A recent near-identical save MUST suppress the individual file write."""
    memory_dir = _isolated_env

    matching = _matching_embedding()

    # Pre-index a prior save with the matching embedding
    _index_prior_save(
        file_path=os.path.join(memory_dir, "projects", "earlier-snapshot.md"),
        slug_id="projects-earlier-snapshot",
        content="Earlier snapshot of the same work",
        embedding=matching,
    )

    # Pre-create the project status file so we can verify its append still happens
    status_path = os.path.join(memory_dir, "projects", "palinode-status.md")
    with open(status_path, "w", encoding="utf-8") as f:
        f.write("# palinode status\n")

    with (
        mock.patch("palinode.core.embedder.embed", return_value=matching),
        mock.patch("palinode.api.server._generate_description", return_value="Reformatted"),
    ):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(
            summary="Same content, reformatted by session-end",
            project="palinode",
            source="test",
        )
        result = session_end_api(req)

    # Individual file suppressed
    assert result.get("individual_file") is None
    assert _count_individual_session_files(memory_dir) == 0
    # Response carries the matched slug
    assert result.get("deduplicated_against") == "projects-earlier-snapshot"
    # Daily note still appended
    daily_path = os.path.join(memory_dir, result["daily_file"])
    assert os.path.exists(daily_path)
    assert "Same content, reformatted" in open(daily_path, encoding="utf-8").read()
    # Project status file still appended
    status_text = open(status_path, encoding="utf-8").read()
    assert "Same content, reformatted" in status_text


def test_above_threshold_outside_window_does_not_dedup(_isolated_env):
    """An old save (outside the lookback window) must not trigger dedup."""
    memory_dir = _isolated_env

    matching = _matching_embedding()

    # File mtime two hours ago — outside the default 60-minute window
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    _index_prior_save(
        file_path=os.path.join(memory_dir, "projects", "old-snapshot.md"),
        slug_id="projects-old-snapshot",
        content="Old snapshot from earlier today",
        embedding=matching,
        mtime=long_ago,
    )

    with (
        mock.patch("palinode.core.embedder.embed", return_value=matching),
        mock.patch("palinode.api.server._generate_description", return_value="Fresh save"),
    ):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(
            summary="A fresh session-end after a long break",
            project="palinode",
            source="test",
        )
        result = session_end_api(req)

    # Both files written; no dedup metadata in response
    assert result.get("individual_file") is not None
    assert os.path.exists(result["individual_file"])
    assert "deduplicated_against" not in result
    assert _count_individual_session_files(memory_dir) == 1


def test_embedder_failure_writes_both_files(_isolated_env):
    """When the embedder returns empty, dedup is skipped and both files written."""
    memory_dir = _isolated_env

    # Even with a perfect-match save indexed, an empty embedding means no dedup
    _index_prior_save(
        file_path=os.path.join(memory_dir, "projects", "perfect-match.md"),
        slug_id="projects-perfect-match",
        content="Same as new",
        embedding=_matching_embedding(),
    )

    with (
        mock.patch("palinode.core.embedder.embed", return_value=[]),
        mock.patch("palinode.api.server._generate_description", return_value="Empty embed"),
    ):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(
            summary="Should still write because dedup degraded gracefully",
            project="palinode",
            source="test",
        )
        result = session_end_api(req)

    assert result.get("individual_file") is not None
    assert os.path.exists(result["individual_file"])
    assert "deduplicated_against" not in result


def test_recent_save_embeddings_skips_daily_files(_isolated_env):
    """Daily files must be excluded from the dedup candidate list."""
    memory_dir = _isolated_env
    matching = _matching_embedding()

    # Daily-style file — should NOT be returned by recent_save_embeddings
    _index_prior_save(
        file_path=os.path.join(memory_dir, "daily", "2026-04-26.md"),
        slug_id="daily-2026-04-26",
        content="Daily journal entry",
        embedding=matching,
    )

    recent = store.recent_save_embeddings(60)
    slugs = [s for s, _ in recent]
    assert "daily-2026-04-26" not in slugs


def test_recent_save_embeddings_returns_embedding_data(_isolated_env):
    """Sanity check: stored embeddings round-trip through the helper as floats."""
    emb = _matching_embedding()
    _index_prior_save(
        file_path=os.path.join(_isolated_env, "projects", "x.md"),
        slug_id="projects-x",
        content="some content",
        embedding=emb,
    )
    recent = store.recent_save_embeddings(60)
    assert len(recent) == 1
    slug, vec = recent[0]
    assert slug == "projects-x"
    assert len(vec) == EMBED_DIM
    # The recovered vector should match the inserted one to within float32 precision
    delta = max(abs(a - b) for a, b in zip(vec, emb, strict=False))
    assert delta < 1e-5
