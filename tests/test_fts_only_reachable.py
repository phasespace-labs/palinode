"""Regression: a chunk written FTS-only must be reachable at the default
``/search`` threshold.

The NaN-input fix made ``reconcile`` write a section FTS-only when the
embedder rejects that one input (bge-m3's NaN vector), and
``EmbeddingInputError``'s recovery text promises the chunk "stays
keyword-searchable". It did not: the chunk has no ``chunks_vec`` row, so the
vector arm can never carry it, and its normalized BM25 score (``raw / 25.0``)
never clears the shared per-arm floor that the BM25-arm measurement
deliberately left in place for chunks that *have* a vector. The row was in
``chunks``, in ``chunks_fts``, matched ``MATCH`` — and ``/search`` would not
return it while a vector-bearing control in the same two-document store came
back normally.

Fix: ``search_hybrid`` marks each BM25 candidate with ``has_vector`` and
``rank_hybrid`` exempts vectorless candidates from the floor. This file
proves it end-to-end against a real SQLite store (no DB mocking, per
CLAUDE.md) — the poisoned chunk is written through the real
``reconcile.apply`` FTS-only path using the NaN-input regression test's
selective embedder — and pins that a chunk with a vector is floored exactly
as before.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import palinode.core.embedder as embedder_mod
from palinode.api import server as srv
from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.indexer import reconcile
from tests._store_helpers import upsert_chunks

# The NaN-input regression test's embedder seam: rejects exactly the poison
# string with the typed per-input error, embeds everything else.
from tests.test_embed_nan_input import _POISON, _VEC, _SelectiveEmbedder

_KEYWORD_QUERY = "painting sunset worth paid"
_TS = "2026-09-05T00:00:00+00:00"


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    # Same shape as test_embed_nan_input.tmp_store (importing a fixture by
    # name trips F811 on every test parameter that names it).
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


def _poison_doc() -> str:
    # A short body stays a single ``root`` section (parser keeps documents
    # under ~2000 chars whole), so the whole memory is the rejected input —
    # exactly the exposure the issue describes.
    return (
        "---\n"
        "id: poison\n"
        "category: insights\n"
        "---\n\n"
        f"{_POISON}\n"
    )


def _write_poison_fts_only(tmp_store) -> str:
    path = str(tmp_store / "insights" / "poison.md")
    p = reconcile.plan(reconcile.derive(path, _poison_doc()))
    diff = reconcile.apply(p, embedder=_SelectiveEmbedder())
    assert diff.committed is True and diff.embed_failures == 1 and diff.vec_ok is False
    return path


def _write_vectored(tmp_store, name: str, content: str, embedding: list[float]) -> str:
    path = str(tmp_store / "insights" / f"{name}.md")
    upsert_chunks(
        [{
            "id": f"chunk-{name}",
            "file_path": path,
            "section_id": "root",
            "category": "insights",
            "content": content,
            "metadata": {},
            "created_at": _TS,
            "last_updated": _TS,
            "embedding": embedding,
        }],
        skip_unchanged=False,
    )
    return path


def _orthogonal_to_vec() -> list[float]:
    # Cosine 0 against ``_VEC`` (a constant vector): half +x, half -x.
    half = len(_VEC) // 2
    return [0.03] * half + [-0.03] * half


@pytest.fixture(autouse=True)
def _decay_off(monkeypatch):
    monkeypatch.setattr(config.decay, "enabled", False)


class TestStoreLevel:
    def test_fts_only_chunk_survives_default_threshold(self, tmp_store):
        poison_path = _write_poison_fts_only(tmp_store)
        _write_vectored(
            tmp_store, "control",
            "Fungal hyphae trade phosphorus to host trees for carbon.", _VEC,
        )

        # The test is only meaningful if the FTS arm alone would NOT clear
        # the floor — the pre-fix mechanism.
        fts = store.search_fts(_KEYWORD_QUERY)
        assert [h["file_path"] for h in fts] == [poison_path]
        assert fts[0]["score"] < config.search.mcp_threshold

        hits = store.search_hybrid(
            _KEYWORD_QUERY, _VEC, threshold=config.search.api_threshold,
            record_access=False,
        )
        paths = [h["file_path"] for h in hits]
        assert poison_path in paths, "FTS-only chunk must be reachable at the default floor"
        poison_hit = next(h for h in hits if h["file_path"] == poison_path)
        assert poison_hit["has_vector"] is False
        assert poison_hit["raw_score"] is None

    def test_vectored_chunk_is_floored_exactly_as_before(self, tmp_store):
        """Same query — the difference is whether the chunk has a vector and
        how strong its keyword match is. The FTS floor is relative to the best
        keyword match (``config.search.fts_threshold`` × top): the vectored
        chunk matches one query word, lands under that floor, has cosine 0 to
        the query, and stays excluded; the vectorless one is exempt from the
        FTS floor and is returned however weak its BM25."""
        poison_path = _write_poison_fts_only(tmp_store)
        vectored_path = _write_vectored(
            tmp_store, "vectored",
            "The painting hangs in the hall.",   # one of the four query words
            _orthogonal_to_vec(),
        )

        fts = store.search_fts(_KEYWORD_QUERY)
        assert {h["file_path"] for h in fts} == {poison_path, vectored_path}
        assert all(h["score"] < config.search.api_threshold for h in fts)
        by_path = {h["file_path"]: h["score"] for h in fts}
        assert by_path[vectored_path] < config.search.fts_threshold * by_path[poison_path]

        hits = store.search_hybrid(
            _KEYWORD_QUERY, _VEC, threshold=config.search.api_threshold,
            record_access=False,
        )
        paths = [h["file_path"] for h in hits]
        assert poison_path in paths
        assert vectored_path not in paths, (
            "a chunk WITH a vector must still be thresholded per-arm, as before"
        )

    def test_vectored_chunk_above_cosine_floor_keeps_its_score(self, tmp_store):
        control_path = _write_vectored(
            tmp_store, "control",
            "Fungal hyphae trade phosphorus to host trees for carbon.", _VEC,
        )
        before = store.search_hybrid(
            _KEYWORD_QUERY, _VEC, threshold=config.search.api_threshold,
            record_access=False,
        )
        assert [h["file_path"] for h in before] == [control_path]

        _write_poison_fts_only(tmp_store)
        after = store.search_hybrid(
            _KEYWORD_QUERY, _VEC, threshold=config.search.api_threshold,
            record_access=False,
        )
        control_after = next(h for h in after if h["file_path"] == control_path)
        assert control_after["raw_score"] == before[0]["raw_score"]
        assert control_after["score"] == before[0]["score"]
        assert control_after.get("has_vector", True) is True

    def test_exemption_retires_once_vector_is_backfilled(self, tmp_store):
        """After a REEMBED pass writes the vector, the chunk is an ordinary
        vectored candidate again and the FTS floor applies to it. A strict
        explicit floor (nothing clears it unless exempt) makes that visible:
        vectorless, the chunk is returned; vectored, it is not."""
        poison_path = _write_poison_fts_only(tmp_store)
        strict = dict(threshold=config.search.api_threshold, fts_threshold=2.0, record_access=False)
        before = store.search_hybrid(_KEYWORD_QUERY, _VEC, **strict)
        assert poison_path in [h["file_path"] for h in before]

        class _HealedEmbedder:
            def embed(self, text: str) -> list[float]:
                return _orthogonal_to_vec()

        p2 = reconcile.plan(reconcile.derive(poison_path, _poison_doc()))
        assert [pw.reason for pw in p2.to_index] == [reconcile.REEMBED]
        assert reconcile.apply(p2, embedder=_HealedEmbedder()).vec_ok is True

        hits = store.search_hybrid(_KEYWORD_QUERY, _VEC, **strict)
        assert poison_path not in [h["file_path"] for h in hits]


class TestSearchEndpoint:
    @pytest.fixture()
    def client(self, tmp_store, monkeypatch):
        monkeypatch.setattr(config.auto_summary, "enabled", False)
        # Queries embed fine; only the poisoned memory was rejected at index time.
        monkeypatch.setattr(embedder_mod, "embed", lambda text: _VEC)
        srv._rate_counters.clear()
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        srv._rate_counters.clear()

    def test_search_returns_fts_only_chunk_at_default_threshold(self, client, tmp_store):
        poison_path = _write_poison_fts_only(tmp_store)
        _write_vectored(
            tmp_store, "control",
            "Fungal hyphae trade phosphorus to host trees for carbon.", _VEC,
        )

        res = client.post("/search", json={"query": _KEYWORD_QUERY})
        assert res.status_code == 200
        hits = res.json()
        assert poison_path in [h["file_path"] for h in hits], hits
        assert not any(h.get("mode") == "keyword-fallback" for h in hits)
