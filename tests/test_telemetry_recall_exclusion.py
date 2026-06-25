"""Tests for telemetry recall-exclusion (ADR-015 §2.3, #398).

Memories carrying ``metadata.kind: telemetry`` in frontmatter (which flattens to
a top-level ``kind`` field via save_api) are machine/monitor writes. They are
HARD-EXCLUDED from default semantic recall (§6 Q3) so monitoring churn does not
pollute human recall — but remain retrievable when the caller passes an explicit
``include_telemetry`` override.

The filter lives in the shared store layer (``search`` / ``search_fts`` /
``list_recent``, inherited by ``search_hybrid``) so all surfaces — API, MCP,
CLI, plugin — inherit it (ADR-010 parity), the same pattern #371 used.

All tests use real SQLite in tmp_path (no mocked DB). Only the embedder is
bypassed by inserting deterministic unit vectors directly.
"""
from __future__ import annotations

import math
import os
from unittest.mock import patch

import pytest

from palinode.core import store
from palinode.core.config import config

EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _embedding(seed: int) -> list[float]:
    """Deterministic near-orthogonal unit vector keyed by ``seed``."""
    vec = [0.0] * EMBED_DIM
    vec[seed % EMBED_DIM] = 0.9
    vec[(seed * 7 + 3) % EMBED_DIM] = 0.4
    return _normalize(vec)


def _index_chunk(
    *, chunk_id: str, file_path: str, content: str, seed: int,
    metadata: dict | None = None,
) -> None:
    from datetime import UTC, datetime
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    store.upsert_chunks([{
        "id": chunk_id,
        "file_path": file_path,
        "section_id": None,
        "category": "inbox",
        "content": content,
        "metadata": metadata or {},
        "created_at": now_iso,
        "last_updated": now_iso,
        "embedding": _embedding(seed),
    }])


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point config at a fresh tmp dir + real SQLite DB. No git, no Ollama."""
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    store._db_checked = False
    for d in ("inbox", "insights"):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)
    store.init_db()
    yield memory_dir
    store._db_checked = False


# ── (c) telemetry is excluded from default vector search ─────────────────────

def test_telemetry_excluded_from_default_vector_search(_isolated_env):
    """A kind:telemetry chunk does NOT surface in default store.search."""
    _index_chunk(
        chunk_id="probe", file_path="inbox/uptime-kuma.md",
        content="uptime kuma DOWN HTTP 502", seed=20,
        metadata={"kind": "telemetry"},
    )
    results = store.search(query_embedding=_embedding(20), threshold=0.0)
    assert "probe" not in {r["id"] for r in results}


def test_telemetry_excluded_from_default_hybrid_search(_isolated_env):
    """Parity: the hybrid (vector + BM25) path also excludes telemetry."""
    _index_chunk(
        chunk_id="probe-h", file_path="inbox/uptime-kuma.md",
        content="uptime kuma telemetry incident", seed=21,
        metadata={"kind": "telemetry"},
    )
    results = store.search_hybrid(
        query_text="uptime kuma telemetry incident",
        query_embedding=_embedding(21),
        top_k=5,
        threshold=0.0,
    )
    assert "probe-h" not in {r.get("id") for r in results}


def test_telemetry_excluded_from_default_fts_search(_isolated_env):
    """BM25 keyword search also hard-excludes telemetry by default."""
    _index_chunk(
        chunk_id="probe-f", file_path="inbox/uptime-kuma.md",
        content="distinctivekeyword telemetry marker", seed=22,
        metadata={"kind": "telemetry"},
    )
    results = store.search_fts("distinctivekeyword")
    assert "probe-f" not in {r["id"] for r in results}


def test_telemetry_excluded_from_recency_list(_isolated_env):
    """Empty-query recency mode (list_recent) also excludes telemetry."""
    _index_chunk(
        chunk_id="probe-r", file_path="inbox/uptime-kuma.md",
        content="recent telemetry write", seed=23,
        metadata={"kind": "telemetry"},
    )
    results = store.list_recent(limit=50)
    assert "probe-r" not in {r.get("file_path") for r in results}


# ── (d) telemetry IS included when the explicit override is passed ───────────

def test_telemetry_included_with_explicit_override_vector(_isolated_env):
    """Passing kind_exclude_list=[] (the include-telemetry override) surfaces it."""
    _index_chunk(
        chunk_id="probe2", file_path="inbox/uptime-kuma.md",
        content="uptime kuma DOWN HTTP 502", seed=24,
        metadata={"kind": "telemetry"},
    )
    results = store.search(
        query_embedding=_embedding(24), threshold=0.0, kind_exclude_list=[],
    )
    assert "probe2" in {r["id"] for r in results}


def test_telemetry_included_with_explicit_override_hybrid(_isolated_env):
    _index_chunk(
        chunk_id="probe2-h", file_path="inbox/uptime-kuma.md",
        content="uptime kuma telemetry incident", seed=25,
        metadata={"kind": "telemetry"},
    )
    results = store.search_hybrid(
        query_text="uptime kuma telemetry incident",
        query_embedding=_embedding(25),
        top_k=5,
        threshold=0.0,
        kind_exclude_list=[],
    )
    assert "probe2-h" in {r.get("id") for r in results}


# ── (e) regression guard: non-telemetry memories are unaffected ──────────────

def test_non_telemetry_memories_unaffected(_isolated_env):
    """A normal memory (no kind, or kind != telemetry) is always returned."""
    _index_chunk(
        chunk_id="normal", file_path="inbox/normal.md",
        content="normal human decision memory", seed=26,
        metadata={},  # no kind
    )
    _index_chunk(
        chunk_id="other-kind", file_path="inbox/other.md",
        content="some other kind of memory", seed=27,
        metadata={"kind": "snapshot"},  # a non-telemetry kind
    )
    r_normal = store.search(query_embedding=_embedding(26), threshold=0.0)
    assert "normal" in {r["id"] for r in r_normal}

    r_other = store.search(query_embedding=_embedding(27), threshold=0.0)
    assert "other-kind" in {r["id"] for r in r_other}


def test_mixed_corpus_default_drops_only_telemetry(_isolated_env):
    """With telemetry and non-telemetry sharing a query neighbourhood, default
    search returns the non-telemetry one and drops the telemetry one."""
    _index_chunk(
        chunk_id="human", file_path="inbox/human.md",
        content="shared topic alpha", seed=28, metadata={},
    )
    _index_chunk(
        chunk_id="machine", file_path="inbox/machine.md",
        content="shared topic alpha", seed=28, metadata={"kind": "telemetry"},
    )
    ids = {r["id"] for r in store.search(query_embedding=_embedding(28), threshold=0.0)}
    assert "human" in ids
    assert "machine" not in ids


# ── API surface: include_telemetry override travels through /search ──────────

class TestSearchApiTelemetryOverride:
    """End-to-end through the FastAPI /search endpoint, real DB, mocked embed."""

    @pytest.fixture()
    def api(self, _isolated_env, monkeypatch):
        import importlib

        from fastapi.testclient import TestClient

        # Bearer auth (PALINODE_API_TOKEN) is baked into the app's middleware at
        # module-import time. test_api_bearer_auth.py reloads the server module
        # with a token set and does not restore it, so in the full suite the
        # cached app can carry a token. Reload here with the token cleared so
        # this unauthenticated TestClient isn't 401'd (isolation, not impl).
        for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
            monkeypatch.delenv(_k, raising=False)
        import palinode.api.server as srv
        srv = importlib.reload(srv)

        # Pre-seed a telemetry chunk and a normal chunk that match the same query.
        _index_chunk(
            chunk_id="api-tel", file_path="inbox/api-tel.md",
            content="apisearch telemetry payload", seed=29,
            metadata={"kind": "telemetry"},
        )
        _index_chunk(
            chunk_id="api-norm", file_path="inbox/api-norm.md",
            content="apisearch human payload", seed=29, metadata={},
        )
        # The query embeds to seed=29's vector so both chunks are candidates.
        monkeypatch.setattr(
            "palinode.core.embedder.embed", lambda *_a, **_k: _embedding(29)
        )
        srv._rate_counters.clear()
        with TestClient(srv.app, raise_server_exceptions=True) as c:
            yield c
        srv._rate_counters.clear()

    def test_default_search_excludes_telemetry(self, api):
        res = api.post("/search", json={"query": "apisearch", "threshold": 0.0, "limit": 20})
        assert res.status_code == 200, res.text
        paths = {r["file_path"] for r in res.json()}
        assert "inbox/api-norm.md" in paths
        assert "inbox/api-tel.md" not in paths

    def test_include_telemetry_override_surfaces_it(self, api):
        res = api.post(
            "/search",
            json={"query": "apisearch", "threshold": 0.0, "limit": 20,
                  "include_telemetry": True},
        )
        assert res.status_code == 200, res.text
        paths = {r["file_path"] for r in res.json()}
        assert "inbox/api-tel.md" in paths
