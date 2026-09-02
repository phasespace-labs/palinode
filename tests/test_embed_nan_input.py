"""Regression: a per-input NaN embedding must not be treated as a backend outage.

For some inputs Ollama's bge-m3 produces a NaN vector and the server fails to
serialise it: HTTP 500 with ``failed to encode response: json: unsupported
value: NaN`` — deterministic per input string, while every other input embeds
fine. This used to be wrapped as ``EmbeddingUnavailable`` (a connectivity
outage): the call was retried, the circuit breaker took a hit, the
keyword-only-mode notice fired, ``reconcile`` aborted the whole file so the
note was not indexed at all — not even FTS — and a query hitting it raised
instead of degrading to BM25.

Now it is a typed ``EmbeddingInputError``: no retry, no breaker hit, no
keyword-only notice; reconcile writes just that section FTS-only and indexes
the rest of the file; ``/search`` answers keyword-only with hits marked
``mode: keyword-fallback``; embed-dependent endpoints without a fallback
return a typed 422, not a 503 or 500.

Real SQLite + tmp_path, no DB mocking (per CLAUDE.md). The Ollama backend is
faked at the HTTP layer (``httpx.MockTransport``) with the exact 500 body.
"""
from __future__ import annotations

import pytest

import httpx
from fastapi.testclient import TestClient

import palinode.core.embedder as embedder_mod
import palinode.core.ollama_client as ollama_client_mod
from palinode.api import server as srv
from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.core.embedder import EmbeddingInputError, EmbeddingUnavailable
from palinode.core.ollama_client import (
    CircuitState,
    OllamaClient,
    OllamaRole,
    RetryPolicy,
)
from palinode.indexer import reconcile
from tests._store_helpers import upsert_chunks

_NAN_BODY = {"error": "failed to encode response: json: unsupported value: NaN"}
_VEC = [0.03] * 1024
_POISON = "How much is the painting of a sunset worth compared to what I paid?"


def _nan_client(calls: list) -> OllamaClient:
    """Real OllamaClient whose HTTP layer always returns the NaN 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json=_NAN_BODY)

    return OllamaClient(
        retry_policy=RetryPolicy(retries=3),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class TestClientLayer:
    """ollama_client: typed error, no retry storm, no breaker hit."""

    def test_nan_500_raises_typed_error_without_retries(self):
        calls: list = []
        oc = _nan_client(calls)
        try:
            with pytest.raises(EmbeddingInputError) as exc_info:
                oc.embed(_POISON, model="bge-m3")
            # Deterministic per input: one request, no backoff retries, and no
            # legacy-endpoint fallback (same model, same input).
            assert len(calls) == 1
            assert exc_info.value.model == "bge-m3"
            assert exc_info.value.text_len == len(_POISON)
            assert "unsupported value: NaN" in exc_info.value.ollama_message
            # Never the raw text in the message.
            assert _POISON not in str(exc_info.value)
        finally:
            oc.close()

    def test_nan_500_does_not_trip_the_circuit_breaker(self):
        calls: list = []
        oc = _nan_client(calls)
        try:
            for _ in range(5):
                with pytest.raises(EmbeddingInputError):
                    oc.embed(_POISON, model="bge-m3")
            # A healthy backend must not be pushed toward keyword-only mode by
            # one pathological string, however often it is retried.
            assert oc._circuit(OllamaRole.EMBED).state is CircuitState.CLOSED
        finally:
            oc.close()


class TestEmbedderBoundary:
    """embedder.embed: propagates the typed signal, no outage side effects."""

    @pytest.fixture()
    def nan_backend(self, monkeypatch):
        calls: list = []
        oc = _nan_client(calls)
        monkeypatch.setattr(ollama_client_mod, "_singleton", oc)
        monkeypatch.setattr(embedder_mod, "_preflight_done", True)
        monkeypatch.setattr(embedder_mod, "_keyword_only_notice_done", False)
        yield calls
        oc.close()

    def test_raises_input_error_not_unavailable(self, nan_backend):
        with pytest.raises(EmbeddingInputError):
            embedder_mod.embed(_POISON)

    def test_no_keyword_only_mode_notice(self, nan_backend):
        # The keyword-only notice announces a backend outage; a per-input
        # failure on a healthy backend must not fire it.
        with pytest.raises(EmbeddingInputError):
            embedder_mod.embed(_POISON)
        assert embedder_mod._keyword_only_notice_done is False


class _SelectiveEmbedder:
    """Embeds everything except the poison string (raises typed input error)."""

    def embed(self, text: str) -> list[float]:
        if _POISON in text:
            raise EmbeddingInputError(
                model="bge-m3", text_len=len(text),
                ollama_message=_NAN_BODY["error"],
            )
        return _VEC


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


def _two_section_doc() -> str:
    # Bodies under ~2000 chars stay a single "root" section (see
    # parser.parse_markdown); pad both sections so the H2 split engages and
    # the per-section degradation is actually exercised.
    filler = "The tomatoes and the marigolds thrived along the south fence. " * 20
    return (
        "---\n"
        "id: nan-note\n"
        "category: insights\n"
        "---\n\n"
        "# Note\n\n"
        f"## Healthy\n\nA perfectly embeddable fact about the garden.\n{filler}\n\n"
        f"## Poisoned\n\n{_POISON}\n{filler}\n"
    )


class TestReconcilePerSectionDegradation:
    """reconcile.apply: the poisoned section goes FTS-only; the file commits."""

    def _apply(self, path: str) -> reconcile.Diff:
        p = reconcile.plan(reconcile.derive(path, _two_section_doc()))
        assert len(p.to_index) >= 2, "doc must split into multiple sections"
        return reconcile.apply(p, embedder=_SelectiveEmbedder())

    def test_file_commits_with_poisoned_section_fts_only(self, tmp_store):
        path = str(tmp_store / "insights" / "nan-note.md")
        diff = self._apply(path)

        assert diff.committed is True
        assert diff.embed_failures == 1
        assert diff.vec_ok is False
        # The note did NOT vanish from recall: the poisoned section is
        # keyword-searchable (this exact loss — not indexed at all, not even
        # FTS — was the production defect).
        hits = store.search_fts("painting sunset worth paid")
        assert any(_POISON in h["content"] for h in hits)
        # The healthy section embedded normally.
        hits = store.search_fts("embeddable fact garden")
        assert len(hits) == 1

    def test_poisoned_section_is_replanned_as_reembed(self, tmp_store):
        # Self-healing: the vector-less chunk is picked up as REEMBED on the
        # next pass, so a fixed model backfills the vector automatically.
        path = str(tmp_store / "insights" / "nan-note.md")
        self._apply(path)

        p2 = reconcile.plan(reconcile.derive(path, _two_section_doc()))
        reasons = [pw.reason for pw in p2.to_index]
        assert reasons == [reconcile.REEMBED]

    def test_backend_outage_still_aborts_whole_file(self, tmp_store):
        # The fail-closed contract for real outages is unchanged.
        class _DeadEmbedder:
            def embed(self, text: str) -> list[float]:
                raise EmbeddingUnavailable(
                    backend="local", model="bge-m3",
                    text_len=len(text), cause="connection refused",
                )

        path = str(tmp_store / "insights" / "nan-note.md")
        p = reconcile.plan(reconcile.derive(path, _two_section_doc()))
        diff = reconcile.apply(p, embedder=_DeadEmbedder())
        assert diff.committed is False
        assert store.search_fts("painting sunset worth paid") == []


class TestSearchKeywordFallback:
    """/search degrades to the keyword arm; other endpoints get a typed 422."""

    @pytest.fixture()
    def client(self, tmp_store, monkeypatch):
        monkeypatch.setattr(config.auto_summary, "enabled", False)
        srv._rate_counters.clear()
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        srv._rate_counters.clear()

    @pytest.fixture()
    def poisoned_query_embedder(self, monkeypatch):
        def _raise(text: str) -> list[float]:
            raise EmbeddingInputError(
                model="bge-m3", text_len=len(text),
                ollama_message=_NAN_BODY["error"],
            )
        monkeypatch.setattr(embedder_mod, "embed", _raise)

    def test_search_answers_keyword_only_with_mode_marker(
        self, client, tmp_store, poisoned_query_embedder
    ):
        upsert_chunks(
            [{
                "id": "c1",
                "file_path": str(tmp_store / "insights" / "sunset.md"),
                "section_id": "root",
                "category": "insights",
                "content": "the sunset painting was appraised at a high worth",
                "metadata": {},
                "created_at": "2026-08-31T00:00:00+00:00",
                "last_updated": "2026-08-31T00:00:00+00:00",
                "embedding": _VEC,
            }],
            skip_unchanged=False,
        )
        res = client.post("/search", json={"query": "sunset painting worth"})
        assert res.status_code == 200
        hits = res.json()
        assert len(hits) == 1
        assert hits[0]["mode"] == "keyword-fallback"
        assert "sunset painting" in hits[0]["content"]

    def test_endpoint_without_fallback_returns_typed_422(
        self, client, poisoned_query_embedder
    ):
        res = client.post(
            "/check-triggers", json={"query": "poisoned trigger context"}
        )
        assert res.status_code == 422
        detail = res.json()["detail"]
        assert "rejected" in detail
        assert "unsupported value: NaN" in detail
        assert "poisoned trigger context" not in detail
