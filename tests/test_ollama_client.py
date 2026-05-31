"""Unit tests for the centralized Ollama client (#338 Phase 1+2+3).

Drives the acceptance criteria directly:
- callers get a typed OllamaError subclass, never a bare httpx error;
- the circuit breaker opens after the failure threshold;
- once open, calls fast-fail (no network I/O) instead of waiting for a timeout;
- per-role routing sends embed → embed URL, chat/generate → chat URL;
- structured JSON-line logs carry the #337 field set;
- rolling metrics expose p50/p95/error_rate per role.

A fake clock + injected sleep make backoff and cooldown deterministic; an
httpx.MockTransport drives real httpx exception types through the retry path.
"""
from __future__ import annotations

import json
import logging
import random
import time

import httpx
import pytest

from palinode.core.config import config
from palinode.core.ollama_client import (
    CircuitBreaker,
    CircuitState,
    EmbeddingContextError,
    OllamaCircuitOpen,
    OllamaClient,
    OllamaError,
    OllamaRole,
    OllamaTimeout,
    OllamaUnreachable,
    RetryPolicy,
    RollingMetrics,
    _extract_embedding_vector,
    _percentile,
    _resolve_base_url,
    get_ollama_client,
)


# ──────────────────────────────────────────────────────────────────────────
# Test scaffolding
# ──────────────────────────────────────────────────────────────────────────


class Clock:
    """Injectable monotonic/wall clock. Constant unless ticked."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t

    def tick(self, dt: float) -> None:
        self.t += dt


def make_client(handler, *, clock=None, retries=3, sleeps=None):
    """Build an OllamaClient wired to a MockTransport handler + fake clock."""
    clock = clock or Clock()
    if sleeps is None:
        sleeps = []
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return OllamaClient(
        retry_policy=RetryPolicy(retries=retries),
        http_client=http_client,
        sleep=lambda s: sleeps.append(s),
        monotonic=clock.monotonic,
        now=clock.time,
        rng=random.Random(0),
    ), clock, sleeps


def ok_response(body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body or {"response": "hello"})
    return handler


# ──────────────────────────────────────────────────────────────────────────
# Per-role routing (misroute impossible by construction)
# ──────────────────────────────────────────────────────────────────────────


def test_resolve_base_url_per_role(monkeypatch):
    monkeypatch.setattr(config.embeddings.primary, "url", "http://embed-host:11434")
    monkeypatch.setattr(config.auto_summary, "ollama_url", "http://chat-host:11434")
    assert _resolve_base_url(OllamaRole.EMBED) == "http://embed-host:11434"
    assert _resolve_base_url(OllamaRole.CHAT) == "http://chat-host:11434"


def test_chat_falls_back_to_primary_when_unset(monkeypatch):
    monkeypatch.setattr(config.embeddings.primary, "url", "http://embed-host:11434")
    monkeypatch.setattr(config.auto_summary, "ollama_url", None)
    assert _resolve_base_url(OllamaRole.CHAT) == "http://embed-host:11434"


def test_embed_and_generate_route_to_their_own_hosts(monkeypatch):
    monkeypatch.setattr(config.embeddings.primary, "url", "http://embed-host:11434")
    monkeypatch.setattr(config.auto_summary, "ollama_url", "http://chat-host:11434")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
        return httpx.Response(200, json={"response": "ok"})

    client, _, _ = make_client(handler)
    client.embed("hi")
    client.generate("hi")
    assert seen[0] == "http://embed-host:11434/api/embed"
    assert seen[1] == "http://chat-host:11434/api/generate"


# ──────────────────────────────────────────────────────────────────────────
# Success path + metrics
# ──────────────────────────────────────────────────────────────────────────


def test_success_returns_body_and_records_metric():
    client, _, _ = make_client(ok_response({"response": "world"}))
    data = client.generate("prompt", retries=0)
    assert data == {"response": "world"}
    m = client.metrics()["chat"]
    assert m["count_5m"] == 1
    assert m["error_rate_5m"] == 0.0
    assert m["circuit_state"] == "closed"


# ──────────────────────────────────────────────────────────────────────────
# Typed exceptions (never leak a bare httpx error)
# ──────────────────────────────────────────────────────────────────────────


def test_read_timeout_raises_ollama_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, _, _ = make_client(handler, retries=0)
    with pytest.raises(OllamaTimeout) as ei:
        client.generate("p", retries=0)
    assert ei.value.role == "chat"


def test_connect_error_raises_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client, _, _ = make_client(handler, retries=0)
    with pytest.raises(OllamaUnreachable):
        client.generate("p", retries=0)


def test_4xx_raises_generic_error_not_retried_and_does_not_trip(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"error": "no such model"})

    client, _, sleeps = make_client(handler, retries=3)
    with pytest.raises(OllamaError) as ei:
        client.generate("p")
    # Not a timeout/unreachable subclass distinction — but base OllamaError, and
    # crucially: tried once, no backoff sleeps, circuit stays closed.
    assert not isinstance(ei.value, (OllamaTimeout, OllamaUnreachable))
    assert calls["n"] == 1
    assert sleeps == []
    assert client.circuit_state(OllamaRole.CHAT) is CircuitState.CLOSED


# ──────────────────────────────────────────────────────────────────────────
# Retry / backoff
# ──────────────────────────────────────────────────────────────────────────


def test_retries_then_succeeds():
    seq = [httpx.ReadTimeout, httpx.ReadTimeout, "ok"]

    def handler(request):
        item = seq.pop(0)
        if item == "ok":
            return httpx.Response(200, json={"response": "recovered"})
        raise item("transient", request=request)

    client, _, sleeps = make_client(handler, retries=3)
    data = client.generate("p")
    assert data["response"] == "recovered"
    assert len(sleeps) == 2  # two backoffs before the third attempt succeeded


def test_5xx_is_retried_then_raises_when_exhausted():
    def handler(request):
        return httpx.Response(503, json={"error": "overloaded"})

    client, _, sleeps = make_client(handler, retries=2)
    with pytest.raises(OllamaError):
        client.generate("p")
    assert len(sleeps) == 2  # retried twice before giving up


def test_retries_zero_is_single_shot():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    client, _, sleeps = make_client(handler, retries=3)
    with pytest.raises(OllamaTimeout):
        client.generate("p", retries=0)
    assert calls["n"] == 1
    assert sleeps == []


# ──────────────────────────────────────────────────────────────────────────
# Circuit breaker (the headline #338 behaviour)
# ──────────────────────────────────────────────────────────────────────────


def test_circuit_opens_after_threshold_consecutive_failures():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client, _, _ = make_client(handler, retries=0)
    # Default threshold is 5. First 4 fail closed; the 5th opens.
    for _ in range(4):
        with pytest.raises(OllamaUnreachable):
            client.generate("p", retries=0)
        assert client.circuit_state(OllamaRole.CHAT) is CircuitState.CLOSED
    with pytest.raises(OllamaUnreachable):
        client.generate("p", retries=0)
    assert client.circuit_state(OllamaRole.CHAT) is CircuitState.OPEN


def test_open_circuit_fast_fails_without_network_io():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("down", request=request)

    client, _, _ = make_client(handler, retries=0)
    for _ in range(5):
        with pytest.raises(OllamaUnreachable):
            client.generate("p", retries=0)
    assert client.circuit_state(OllamaRole.CHAT) is CircuitState.OPEN
    calls_before = calls["n"]

    # Next call must fast-fail with the typed circuit error and do NO network I/O.
    t0 = time.perf_counter()
    with pytest.raises(OllamaCircuitOpen):
        client.generate("p", retries=0)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert calls["n"] == calls_before  # transport never touched
    assert elapsed_ms < 10.0  # fast-fail, not a timeout wait


def test_circuit_half_opens_after_cooldown_and_closes_on_success():
    state = {"mode": "fail"}

    def handler(request):
        if state["mode"] == "fail":
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"response": "back"})

    clock = Clock()
    client, clock, _ = make_client(handler, clock=clock, retries=0)
    for _ in range(5):
        with pytest.raises(OllamaUnreachable):
            client.generate("p", retries=0)
    assert client.circuit_state(OllamaRole.CHAT) is CircuitState.OPEN

    # Before cooldown: still fast-fails.
    with pytest.raises(OllamaCircuitOpen):
        client.generate("p", retries=0)

    # Advance past cooldown (default 60s) and let Ollama recover.
    clock.tick(61.0)
    state["mode"] = "ok"
    data = client.generate("p", retries=0)
    assert data["response"] == "back"
    assert client.circuit_state(OllamaRole.CHAT) is CircuitState.CLOSED


def test_circuit_half_open_failure_reopens():
    def handler(request):
        raise httpx.ConnectError("still down", request=request)

    clock = Clock()
    client, clock, _ = make_client(handler, clock=clock, retries=0)
    for _ in range(5):
        with pytest.raises(OllamaUnreachable):
            client.generate("p", retries=0)
    assert client.circuit_state(OllamaRole.CHAT) is CircuitState.OPEN
    clock.tick(61.0)
    # Half-open probe fails → immediately re-opens.
    with pytest.raises(OllamaUnreachable):
        client.generate("p", retries=0)
    assert client.circuit_state(OllamaRole.CHAT) is CircuitState.OPEN


# ──────────────────────────────────────────────────────────────────────────
# Structured logging (#337 field convention)
# ──────────────────────────────────────────────────────────────────────────


def test_structured_log_has_required_fields(caplog):
    caplog.set_level(logging.INFO, logger="palinode.ollama.events")
    client, _, _ = make_client(ok_response())
    client.generate("p", retries=0)
    events = [
        json.loads(r.message)
        for r in caplog.records
        if r.name == "palinode.ollama.events"
    ]
    assert events, "expected at least one structured event line"
    ev = events[-1]
    for key in ("event", "op", "role", "endpoint", "model", "latency_ms",
                "retry_count", "circuit_state", "outcome"):
        assert key in ev, f"missing structured field {key!r}"
    assert ev["role"] == "chat"
    assert ev["endpoint"] == "/api/generate"
    assert ev["outcome"] == "ok"


def test_open_circuit_logs_fast_fail_event(caplog):
    caplog.set_level(logging.WARNING, logger="palinode.ollama.events")

    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client, _, _ = make_client(handler, retries=0)
    for _ in range(5):
        with pytest.raises(OllamaUnreachable):
            client.generate("p", retries=0)
    with pytest.raises(OllamaCircuitOpen):
        client.generate("p", retries=0)
    outcomes = [
        json.loads(r.message)["outcome"]
        for r in caplog.records if r.name == "palinode.ollama.events"
    ]
    assert "circuit_opened" in outcomes
    assert "fast_fail" in outcomes


# ──────────────────────────────────────────────────────────────────────────
# Metrics snapshot
# ──────────────────────────────────────────────────────────────────────────


def test_metrics_snapshot_percentiles_and_error_rate():
    clock = Clock()
    m = RollingMetrics(window_seconds=300.0, now=clock.time)
    for v in (10, 20, 30, 40, 200):
        m.record(v, ok=True)
    m.record(50, ok=False)
    snap = m.snapshot()
    assert snap["count_5m"] == 6
    assert snap["error_rate_5m"] == round(1 / 6, 4)
    assert snap["p50_ms"] is not None
    assert snap["p95_ms"] >= snap["p50_ms"]


def test_metrics_window_evicts_old_samples():
    clock = Clock()
    m = RollingMetrics(window_seconds=300.0, now=clock.time)
    m.record(10, ok=True)
    clock.tick(301.0)
    m.record(20, ok=True)
    snap = m.snapshot()
    assert snap["count_5m"] == 1  # old sample evicted


def test_percentile_helper():
    assert _percentile([], 0.5) is None
    assert _percentile([42.0], 0.95) == 42.0
    assert _percentile([1, 2, 3, 4], 0.5) == 2
    assert _percentile([1, 2, 3, 4], 1.0) == 4


# ──────────────────────────────────────────────────────────────────────────
# CircuitBreaker unit behaviour
# ──────────────────────────────────────────────────────────────────────────


def test_circuit_breaker_window_reset():
    clock = Clock()
    cb = CircuitBreaker(fail_threshold=3, window_seconds=30.0, monotonic=clock.monotonic)
    cb.record_failure()
    cb.record_failure()
    clock.tick(31.0)  # window elapses; consecutive run resets
    assert cb.record_failure() is False  # count restarts at 1, not 3
    assert cb.state is CircuitState.CLOSED


def test_circuit_breaker_success_resets():
    clock = Clock()
    cb = CircuitBreaker(fail_threshold=2, monotonic=clock.monotonic)
    cb.record_failure()
    cb.record_success()
    assert cb.record_failure() is False  # prior failure cleared by success
    assert cb.state is CircuitState.CLOSED


# ──────────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────────


def test_get_ollama_client_is_singleton():
    assert get_ollama_client() is get_ollama_client()


# ──────────────────────────────────────────────────────────────────────────
# embed() — full contract: dual-endpoint, parsing, ctx-overflow (#338 Phase 3)
# ──────────────────────────────────────────────────────────────────────────


def test_extract_embedding_vector_shapes():
    assert _extract_embedding_vector({"embeddings": [[0.1, 0.2]]}) == [0.1, 0.2]
    assert _extract_embedding_vector({"embedding": [0.3, 0.4]}) == [0.3, 0.4]
    assert _extract_embedding_vector({"embeddings": []}) is None
    assert _extract_embedding_vector({"embedding": []}) is None
    assert _extract_embedding_vector({"error": "x"}) is None
    assert _extract_embedding_vector("nope") is None


def test_embed_success_new_shape(monkeypatch):
    monkeypatch.setattr(config.embeddings.primary, "url", "http://embed-host:11434")

    def handler(request):
        assert request.url.path == "/api/embed"
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    client, _, _ = make_client(handler)
    assert client.embed("hi") == [0.1, 0.2, 0.3]


def test_embed_falls_back_to_legacy_endpoint_on_404():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/embed":
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"embedding": [0.5, 0.6]})

    client, _, _ = make_client(handler, retries=0)
    assert client.embed("hi") == [0.5, 0.6]
    assert seen == ["/api/embed", "/api/embeddings"]


def test_embed_context_overflow_raises_and_does_not_try_second_endpoint():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"error": "prompt is too long for max context"})

    client, _, _ = make_client(handler, retries=0)
    with pytest.raises(EmbeddingContextError) as ei:
        client.embed("x" * 5000)
    assert ei.value.text_len == 5000
    assert "too long" in ei.value.ollama_message
    assert calls["n"] == 1  # overflow is the model's, not the endpoint's — no fallback


def test_embed_timeout_raises_typed_not_fallback():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, _, _ = make_client(handler, retries=0)
    with pytest.raises(OllamaTimeout):
        client.embed("hi")  # a timeout on /api/embed is not a 404 → no legacy fallback


def test_embed_unexpected_shape_exhausts_both_then_raises():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"weird": "shape"})

    client, _, _ = make_client(handler, retries=0)
    with pytest.raises(OllamaError):
        client.embed("hi")
    assert paths == ["/api/embed", "/api/embeddings"]  # tried both on unexpected shape


# ──────────────────────────────────────────────────────────────────────────
# chat_completions() — OpenAI-compatible (consolidation + lint) (#338 Phase 4)
# ──────────────────────────────────────────────────────────────────────────


def test_chat_completions_returns_content_and_uses_base_url_override():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "verdict"}}]})

    client, _, _ = make_client(handler, retries=0)
    out = client.chat_completions(
        [{"role": "user", "content": "hi"}], model="m",
        base_url="http://consol-host:8000", role=OllamaRole.CONSOLIDATION,
    )
    assert out == "verdict"
    assert seen["url"] == "http://consol-host:8000/v1/chat/completions"


def test_chat_completions_malformed_response_raises_ollama_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    client, _, _ = make_client(handler, retries=0)
    with pytest.raises(OllamaError):
        client.chat_completions(
            [{"role": "user", "content": "hi"}], model="m", base_url="http://h:8000",
        )


def test_chat_completions_timeout_raises_typed():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, _, _ = make_client(handler, retries=0)
    with pytest.raises(OllamaTimeout):
        client.chat_completions(
            [{"role": "user", "content": "hi"}], model="m", base_url="http://h:8000",
        )


# ──────────────────────────────────────────────────────────────────────────
# ping() — liveness probe, bypasses the circuit breaker (#338 Phase 5)
# ──────────────────────────────────────────────────────────────────────────


def test_ping_true_on_any_response():
    def handler(request):
        return httpx.Response(200, text="Ollama is running")

    client, _, _ = make_client(handler)
    assert client.ping(OllamaRole.EMBED) is True


def test_ping_false_on_connect_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client, _, _ = make_client(handler)
    assert client.ping(OllamaRole.CHAT) is False


def test_ping_does_not_open_or_consult_circuit():
    """ping must not record failures (no breaker pollution) nor be blocked by it."""
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client, _, _ = make_client(handler, retries=0)
    for _ in range(10):
        assert client.ping(OllamaRole.EMBED) is False
    # 10 failed pings must NOT have opened the EMBED circuit.
    assert client.circuit_state(OllamaRole.EMBED) is CircuitState.CLOSED
    # And pings aren't recorded in metrics.
    assert client.metrics().get("embed", {}).get("count_5m", 0) == 0
