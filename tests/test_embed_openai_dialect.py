"""OpenAI-compatible ``/v1/embeddings`` embed dialect.

``embeddings.primary.dialect: openai`` lets llama.cpp (``llama-server
--embedding``), vLLM, and LM Studio serve as the embedding backend. The
contract under test:

- the default dialect is ``ollama`` and still hits ``/api/embed`` — existing
  deployments see no change;
- ``openai`` POSTs ``{"model", "input"}`` to ``/v1/embeddings`` (a base URL
  that already ends in ``/v1`` is not doubled) and reads ``data[].embedding``
  in input order, re-sorting by ``index`` when the server reorders;
- a rejected input (HTTP 400, or llama.cpp's "input is too large to process"
  500) is the per-input ``EmbeddingInputError`` — one request, no retry, no
  circuit-breaker hit — exactly as the Ollama NaN-vector case;
- transport failures go through the same retry/backoff and breaker as the
  Ollama path and surface as ``EmbeddingUnavailable`` at the embedder;
- the Ollama-only ``/api/show`` preflight is skipped;
- an unknown dialect fails config load.

The backend is faked at the HTTP layer (``httpx.MockTransport``); the
integration test at the bottom drives save → index → hybrid search against a
real SQLite store under ``tmp_path`` (no DB mocking, per CLAUDE.md).
"""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import TypeAdapter

import palinode.core.embedder as embedder_mod
import palinode.core.ollama_client as ollama_client_mod
from palinode.core import store
from palinode.core.config import Config, PrimaryEmbeddingConfig, config
from palinode.core.embedder import EmbeddingInputError, EmbeddingUnavailable
from palinode.core.ollama_client import (
    CircuitState,
    OllamaCircuitOpen,
    OllamaClient,
    OllamaError,
    OllamaRole,
    OllamaUnreachable,
    RetryPolicy,
    _extract_openai_embedding_batch,
)
from palinode.indexer import reconcile

_VLLM_400 = {
    "object": "error",
    "message": (
        "This model's maximum context length is 512 tokens. However, you "
        "requested 700 tokens in the input for embedding generation."
    ),
    "type": "BadRequestError",
    "code": 400,
}
_LLAMACPP_500 = {
    "error": {
        "code": 500,
        "message": "input is too large to process. increase the physical batch size",
        "type": "server_error",
    },
}


def _openai_body(vectors: list[list[float]], *, indices: list[int] | None = None,
                 with_index: bool = True) -> dict:
    items = []
    for pos, vec in enumerate(vectors):
        item = {"object": "embedding", "embedding": vec}
        if with_index:
            item["index"] = indices[pos] if indices is not None else pos
        items.append(item)
    return {"object": "list", "data": items, "model": "bge-m3",
            "usage": {"prompt_tokens": 3, "total_tokens": 3}}


def _client(handler, *, retries: int = 3) -> OllamaClient:
    """Real OllamaClient over a MockTransport; backoff sleeps are no-ops."""
    return OllamaClient(
        retry_policy=RetryPolicy(retries=retries),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
    )


@pytest.fixture()
def openai_dialect(monkeypatch):
    monkeypatch.setattr(config.embeddings.primary, "dialect", "openai")
    monkeypatch.setattr(config.embeddings.primary, "url", "http://embed-host:8080")
    monkeypatch.setattr(config.embeddings.primary, "model", "bge-m3")


# ──────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────


def test_default_dialect_is_ollama():
    assert PrimaryEmbeddingConfig().dialect == "ollama"


def test_dialect_is_normalised():
    assert PrimaryEmbeddingConfig(dialect=" OpenAI ").dialect == "openai"


def test_unknown_dialect_fails_config_load():
    with pytest.raises(ValueError, match="embeddings.primary.dialect"):
        PrimaryEmbeddingConfig(dialect="grpc")
    # Through the same validation path load_config() uses.
    with pytest.raises(ValueError, match="embeddings.primary.dialect"):
        TypeAdapter(Config).validate_python(
            {"embeddings": {"primary": {"dialect": "grpc"}}}
        )


def test_openai_dialect_loads_through_config_adapter():
    cfg = TypeAdapter(Config).validate_python(
        {"embeddings": {"primary": {"dialect": "openai", "url": "http://h:8080/v1"}}}
    )
    assert cfg.embeddings.primary.dialect == "openai"


# ──────────────────────────────────────────────────────────────────────────
# Wire shape
# ──────────────────────────────────────────────────────────────────────────


def test_default_dialect_still_hits_api_embed(monkeypatch):
    monkeypatch.setattr(config.embeddings.primary, "url", "http://embed-host:11434")
    assert config.embeddings.primary.dialect == "ollama"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    oc = _client(handler)
    try:
        assert oc.embed("hi") == [0.1, 0.2]
    finally:
        oc.close()
    assert seen == ["/api/embed"]


def test_openai_single_embed_posts_model_and_input(openai_dialect):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_openai_body([[0.5, 0.25, 0.125]]))

    oc = _client(handler)
    try:
        assert oc.embed("hello") == [0.5, 0.25, 0.125]
        assert oc.has_embedded_ok is True
    finally:
        oc.close()

    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == "http://embed-host:8080/v1/embeddings"
    assert json.loads(req.content) == {"model": "bge-m3", "input": ["hello"]}


def test_base_url_ending_in_v1_is_not_doubled(openai_dialect, monkeypatch):
    monkeypatch.setattr(config.embeddings.primary, "url", "http://embed-host:8080/v1/")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_openai_body([[1.0, 2.0]]))

    oc = _client(handler)
    try:
        oc.embed("x")
    finally:
        oc.close()
    assert seen == ["http://embed-host:8080/v1/embeddings"]


def test_openai_batch_preserves_input_order_when_server_reorders(openai_dialect):
    texts = ["alpha", "beta", "gamma"]
    by_text = {"alpha": [1.0, 0.0], "beta": [0.0, 1.0], "gamma": [1.0, 1.0]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["input"] == texts
        # Server answers gamma, alpha, beta — carrying the true indices.
        return httpx.Response(200, json=_openai_body(
            [by_text["gamma"], by_text["alpha"], by_text["beta"]], indices=[2, 0, 1],
        ))

    oc = _client(handler)
    try:
        assert oc.embed_many(texts) == [by_text[t] for t in texts]
    finally:
        oc.close()


def test_openai_batch_without_index_uses_positional_order(openai_dialect):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_body(
            [[1.0, 0.0], [0.0, 1.0]], with_index=False,
        ))

    oc = _client(handler)
    try:
        assert oc.embed_many(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    finally:
        oc.close()


@pytest.mark.parametrize("body", [
    {"object": "list", "data": []},                                     # empty
    _openai_body([[1.0]], indices=[0]) | {"data": [{"index": 0, "embedding": [1.0]},
                                                    {"index": 0, "embedding": [2.0]}]},  # dup index
    {"object": "list", "data": [{"index": 0, "embedding": [1.0]},
                                {"embedding": [2.0]}]},                 # mixed index/no index
    {"object": "list", "data": [{"index": 0, "embedding": [1.0]},
                                {"index": 1, "embedding": [1.0, 2.0]}]},  # ragged dims
    {"object": "list", "data": [{"index": 0, "embedding": []},
                                {"index": 1, "embedding": [1.0]}]},     # empty vector
    {"embeddings": [[1.0], [2.0]]},                                     # Ollama shape
])
def test_openai_malformed_batch_fails_closed(openai_dialect, body):
    assert _extract_openai_embedding_batch(body, expected_count=2) is None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    oc = _client(handler)
    try:
        with pytest.raises(OllamaError, match="unexpected embed response shape"):
            oc.embed_many(["a", "b"])
    finally:
        oc.close()


# ──────────────────────────────────────────────────────────────────────────
# Error mapping — identical caller contract to the Ollama path
# ──────────────────────────────────────────────────────────────────────────


def test_openai_400_maps_to_embedding_input_error(openai_dialect):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json=_VLLM_400)

    poison = "a string the server refuses"
    oc = _client(handler)
    try:
        with pytest.raises(EmbeddingInputError) as exc_info:
            oc.embed(poison)
        assert len(calls) == 1                      # no retry
        assert oc._circuit(OllamaRole.EMBED).state is CircuitState.CLOSED
        assert exc_info.value.model == "bge-m3"
        assert exc_info.value.text_len == len(poison)
        assert "maximum context length" in exc_info.value.ollama_message
        assert poison not in str(exc_info.value)   # never the raw text
    finally:
        oc.close()


def test_llamacpp_too_large_500_maps_to_embedding_input_error(openai_dialect):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json=_LLAMACPP_500)

    oc = _client(handler)
    try:
        for _ in range(6):
            with pytest.raises(EmbeddingInputError) as exc_info:
                oc.embed_many(["one", "two"])
        assert len(calls) == 6                      # one request per call, no retry
        assert oc._circuit(OllamaRole.EMBED).state is CircuitState.CLOSED
        assert "too large to process" in exc_info.value.ollama_message
    finally:
        oc.close()


def test_openai_other_4xx_is_not_a_per_input_error(openai_dialect):
    """401/404 (auth, unknown model) is a backend problem, not this input's."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    oc = _client(handler)
    try:
        with pytest.raises(OllamaError) as exc_info:
            oc.embed("hi")
        assert not isinstance(exc_info.value, EmbeddingInputError)
        assert exc_info.value.status_code == 404
        assert "model not found" in str(exc_info.value)
    finally:
        oc.close()


def test_openai_5xx_retries_then_trips_breaker_like_ollama(openai_dialect):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="overloaded")

    oc = _client(handler, retries=2)
    try:
        # Default breaker: 5 consecutive failures open it.
        for _ in range(5):
            with pytest.raises(OllamaUnreachable):
                oc.embed("hi")
        assert len(calls) == 5 * 3                  # 1 + 2 retries per call
        assert oc._circuit(OllamaRole.EMBED).state is CircuitState.OPEN
        with pytest.raises(OllamaCircuitOpen):
            oc.embed("hi")
        assert len(calls) == 15                     # fast-fail: no network I/O
    finally:
        oc.close()


# ──────────────────────────────────────────────────────────────────────────
# Embedder boundary + preflight
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def wired(monkeypatch, openai_dialect):
    """Install a MockTransport client as the process singleton; reset preflight state."""
    def _install(handler, *, retries: int = 0) -> OllamaClient:
        oc = _client(handler, retries=retries)
        monkeypatch.setattr(ollama_client_mod, "_singleton", oc)
        monkeypatch.setattr(embedder_mod, "_preflight_done", False)
        monkeypatch.setattr(embedder_mod, "_keyword_only_notice_done", False)
        return oc
    return _install


def test_embedder_skips_api_show_preflight_for_openai(wired):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_openai_body([[0.1, 0.2]]))

    wired(handler)
    assert embedder_mod.embed("hi") == [0.1, 0.2]
    assert paths == ["/v1/embeddings"]
    assert embedder_mod._preflight_done is True


def test_embedder_runs_api_show_preflight_for_ollama(wired, monkeypatch):
    monkeypatch.setattr(config.embeddings.primary, "dialect", "ollama")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"llama.context_length": 8192}})
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    wired(handler)
    assert embedder_mod.embed("hi") == [0.1, 0.2]
    assert paths == ["/api/show", "/api/embed"]


def test_embedder_input_error_passes_through_without_outage_notice(wired):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_VLLM_400)

    wired(handler)
    with pytest.raises(EmbeddingInputError):
        embedder_mod.embed("rejected")
    assert embedder_mod._keyword_only_notice_done is False


def test_embedder_wraps_transport_failure_as_unavailable(wired, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    wired(handler)
    with pytest.raises(EmbeddingUnavailable) as exc_info:
        embedder_mod.embed("hi")
    assert exc_info.value.model == "bge-m3"
    assert "connection refused" in exc_info.value.cause
    assert embedder_mod._keyword_only_notice_done is True
    assert any("dialect=openai" in r.getMessage() for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────────
# Integration: save → index → hybrid search on a real SQLite store
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


def test_openai_dialect_roundtrip_on_real_store(tmp_store, wired):
    dims = int(config.embeddings.primary.dimensions)
    vec = [0.03] * dims
    served: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        inputs = json.loads(request.content)["input"]
        served.append(inputs)
        return httpx.Response(200, json=_openai_body([vec for _ in inputs]))

    wired(handler)

    path = str(tmp_store / "insights" / "llamacpp-note.md")
    content = (
        "---\nid: llamacpp-note\ncategory: insights\n---\n\n"
        "# Note\n\nThe greenhouse thermostat is wired through the relay board.\n"
    )
    diff = reconcile.apply(reconcile.plan(reconcile.derive(path, content)))
    assert diff.committed is True
    assert diff.embed_failures == 0
    assert served, "the index pass must have embedded through /v1/embeddings"

    query = "greenhouse thermostat relay"
    hits = store.search_hybrid(query, embedder_mod.embed(query), record_access=False)
    assert any("relay board" in h["content"] for h in hits)
