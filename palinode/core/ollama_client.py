"""Centralized Ollama client — the single mediation layer for palinode↔Ollama I/O.

the Ollama traffic-surface hardening (Ollama traffic-surface hardening), Phase 1+2+3.
Before this module every caller built its own ``httpx`` request, set its own timeout,
and swallowed its own errors, so palinode was an *unmediated* dependency on a single
Ollama host's instantaneous responsiveness. This client replaces that with one seam:

* **Per-role routing** (``OllamaRole``) — ``embed`` resolves to the configured
  embedding host, while ``chat`` resolves to the configured chat/generate host. Typed
  methods (:meth:`OllamaClient.embed`, :meth:`OllamaClient.generate`,
  :meth:`OllamaClient.chat_completions`) bind to a role, so an embed call cannot be sent to
  the chat host by construction. URLs are resolved *per call* from the live
  config singleton, so env/config changes and test monkeypatching are honoured.
* **Retry with jittered backoff** on transient failures (read timeout, connect
  error, HTTP 5xx). Per-call ``retries=0`` opts a latency-sensitive path out
  (e.g. the inline-description path must not turn one 5 s timeout into
  three).
* **Circuit breaker per role** that *opens loudly* (WARNING log + surfaced
  state). When open, calls fast-fail with :class:`OllamaCircuitOpen` in well
  under a millisecond instead of waiting for a timeout. Half-opens after a
  cooldown to probe recovery.
* **Structured JSON-line logging**: every call emits one line with
  ``{event, role, endpoint, model, latency_ms, retry_count, circuit_state,
  outcome}`` so an operator can grep a single greppable shape.
* **Rolling latency/error metrics** per role (5-minute window) exposed via
  :meth:`OllamaClient.metrics` for ``/status`` and the ``palinode doctor``
  Ollama check.

Callers migrate onto this seam incrementally (see the Ollama traffic-surface hardening
phasing). This module is additive — it changes no existing behaviour until a caller is
pointed at it. """
from __future__ import annotations

import json
import re
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque

import httpx

from palinode.core.config import config

logger = logging.getLogger(__name__)

# A dedicated structured-event logger so JSON lines can be routed/filtered
# independently of the free-text diagnostic logging on the parent logger.
event_logger = logging.getLogger("palinode.ollama.events")


# ──────────────────────────────────────────────────────────────────────────
# Typed exceptions (acceptance criterion: callers get a typed error, not a
# bare httpx error)
# ──────────────────────────────────────────────────────────────────────────


class OllamaError(RuntimeError):
    """Base class for all Ollama mediation-layer failures.

    Carries the role, model, and (when the failure was an HTTP response) the
    status code, so callers and logs have structured context without re-parsing
    the message string. ``status_code`` lets the embed path distinguish a 404
    ("this Ollama version doesn't have /api/embed — fall back to /api/embeddings")
    from other HTTP errors.
    """

    def __init__(
        self, message: str, *, role: str | None = None,
        model: str | None = None, status_code: int | None = None,
    ) -> None:
        self.role = role
        self.model = model
        self.status_code = status_code
        super().__init__(message)


class OllamaUnreachable(OllamaError):
    """Ollama could not be reached (connect error, or retries exhausted)."""


class OllamaTimeout(OllamaError):
    """An Ollama request timed out (read timeout, after any configured retries)."""


class OllamaCircuitOpen(OllamaError):
    """The circuit breaker is open for this role — the call fast-failed.

    Raised *before* any network I/O, so it is the cheap signal a caller uses to
    degrade gracefully (skip the description, write a placeholder, etc.) instead
    of paying a full timeout per call while Ollama is known-bad.
    """


# ──────────────────────────────────────────────────────────────────────────
# Embed context-overflow signal — defined here (not in embedder.py) so
# the embed path can raise it from inside this client without a circular import.
# Re-exported from palinode.core.embedder for backward compatibility.
# ──────────────────────────────────────────────────────────────────────────


class EmbeddingContextError(RuntimeError):
    """Raised when Ollama rejects an embed call due to context-window overflow.

    Callers can catch this specifically to truncate the input, split into
    sub-chunks, or pick a larger model — rather than receiving a silent empty
    list that looks identical to a connectivity failure.

    Attributes:
        model: The Ollama model name.
        text_len: Character length of the rejected input.
        ollama_message: The raw error string from Ollama's response body.
    """

    def __init__(self, model: str, text_len: int, ollama_message: str) -> None:
        self.model = model
        self.text_len = text_len
        self.ollama_message = ollama_message
        super().__init__(
            f"Ollama context-window overflow — model={model!r} text_len={text_len} "
            f"error={ollama_message!r}. "
            f"Recovery: increase num_ctx in the modelfile (e.g. ollama create {model} "
            f"with 'PARAMETER num_ctx 8192'), truncate the input before calling embed(), "
            f"or split into smaller chunks."
        )


# Patterns in Ollama error responses that indicate context overflow. Ollama
# 0.3+ returns these in the JSON body with HTTP 200.
_CTX_OVERFLOW_PATTERNS = (
    "too long for max context",
    "prompt is too long",
    "context length exceeded",
    "exceeds context",
    "num_ctx",
)


_NAN_RE = re.compile(r"\bnan\b", re.IGNORECASE)


def _is_nan_message(message: str) -> bool:
    """True for the serialiser's NaN rejection (``json: unsupported value: NaN``)."""
    return bool(_NAN_RE.search(message or ""))


def _is_ctx_overflow_message(message: str) -> bool:
    """Return True if the Ollama error message indicates context overflow."""
    msg_lower = (message or "").lower()
    return any(p in msg_lower for p in _CTX_OVERFLOW_PATTERNS)


class OllamaInputError(OllamaError):
    """A per-input failure on a healthy backend — permanent for this input.

    Raised when the response body proves the request reached the model and the
    model choked on *this specific input* (e.g. bge-m3 emitting a NaN vector
    that Ollama then fails to JSON-serialise, an HTTP 500 that is
    deterministic per input string). Unlike a connectivity 5xx it is not
    retried and does not trip the circuit breaker: the backend is up, and
    counting it toward the breaker would put a healthy host into keyword-only
    mode over one pathological string.
    """


class EmbeddingInputError(RuntimeError):
    """Raised when the embed backend deterministically rejects one input.

    Sibling of :class:`EmbeddingContextError` — typed so callers can degrade
    per-input (index the chunk FTS-only, answer the query keyword-only)
    instead of treating the failure as a backend outage. Known trigger:
    Ollama's bge-m3 producing a NaN vector for certain strings, which the
    server fails to serialise ("json: unsupported value: NaN", HTTP 500),
    deterministically for that input while every other input embeds fine.

    Attributes:
        model: The Ollama model name.
        text_len: Character length of the rejected input (never the text).
        ollama_message: The raw error string from Ollama's response body.
    """

    def __init__(self, model: str, text_len: int, ollama_message: str) -> None:
        self.model = model
        self.text_len = text_len
        self.ollama_message = ollama_message
        super().__init__(
            f"Embed backend rejected this input — model={model!r} "
            f"text_len={text_len} error={ollama_message!r}. The backend is "
            f"healthy; the failure is deterministic for this input. "
            f"Recovery: the chunk stays keyword-searchable (FTS-only); "
            f"rewording the text usually embeds cleanly."
        )


# Response-body signatures that prove a 5xx is a per-input model failure, not
# a backend outage. Ollama returns the NaN-vector case as HTTP 500 with
# 'failed to encode response: json: unsupported value: NaN'; llama.cpp's
# `llama-server --embedding` returns HTTP 500 'input is too large to process.
# increase the physical batch size' for an input past its batch window —
# deterministic for that input while the server stays healthy.
_INPUT_ERROR_PATTERNS = (
    "unsupported value: nan",
    "input is too large to process",
)


def _is_input_error_message(message: str) -> bool:
    """Return True if a 5xx body names a deterministic per-input failure."""
    msg_lower = (message or "").lower()
    return any(p in msg_lower for p in _INPUT_ERROR_PATTERNS)


def _extract_embedding_vector(data: Any) -> list[float] | None:
    """Pull the embedding vector from either Ollama response shape.

    ``/api/embed`` returns ``{"embeddings": [[...]]}``; the legacy
    ``/api/embeddings`` returns ``{"embedding": [...]}``. Returns None when
    neither carries a non-empty vector (caller then checks for ctx-overflow /
    unexpected shape).
    """
    if not isinstance(data, dict):
        return None
    embs = data.get("embeddings")
    if isinstance(embs, list) and embs and isinstance(embs[0], list):
        return embs[0]
    emb = data.get("embedding")
    if isinstance(emb, list) and emb:
        return emb
    return None


def _extract_embedding_batch(
    data: Any,
    *,
    expected_count: int,
) -> list[list[float]] | None:
    """Return a complete, ordered ``/api/embed`` batch or ``None``.

    Ollama promises one non-empty numeric vector per input, in input order.
    Validate that contract for the *whole* response before exposing any vector
    to callers: a partial response, an empty/malformed vector, or inconsistent
    dimensions must fail closed rather than letting reconciliation write only
    part of a file.
    """
    if not isinstance(data, dict):
        return None
    embeddings = data.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != expected_count:
        return None

    normalized: list[list[float]] = []
    dimensions: int | None = None
    for vector in embeddings:
        if (
            not isinstance(vector, list)
            or not vector
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in vector
            )
        ):
            return None
        if dimensions is None:
            dimensions = len(vector)
        elif len(vector) != dimensions:
            return None
        normalized.append([float(value) for value in vector])
    return normalized


def _extract_openai_embedding_batch(
    data: Any,
    *,
    expected_count: int,
) -> list[list[float]] | None:
    """Return a complete, input-ordered ``/v1/embeddings`` batch or ``None``.

    The OpenAI shape is ``{"data": [{"index": i, "embedding": [...]}, ...]}``.
    Servers are allowed to return items out of order, so when every item
    carries an ``index`` the batch is re-sorted by it; a missing or duplicate
    ``index`` fails closed rather than guessing. Vector validation matches
    :func:`_extract_embedding_batch` (non-empty, numeric, consistent
    dimensions, one per input) so a partial batch never reaches a caller.
    """
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if not isinstance(items, list) or len(items) != expected_count:
        return None

    ordered: list[Any] = [None] * expected_count
    indices = [item.get("index") if isinstance(item, dict) else None for item in items]
    if all(isinstance(i, int) and not isinstance(i, bool) for i in indices):
        if sorted(indices) != list(range(expected_count)):
            return None
        for item, i in zip(items, indices, strict=True):
            ordered[i] = item
    elif any(i is not None for i in indices):
        return None
    else:
        ordered = list(items)

    vectors = [item.get("embedding") if isinstance(item, dict) else None for item in ordered]
    return _extract_embedding_batch({"embeddings": vectors}, expected_count=expected_count)


# ──────────────────────────────────────────────────────────────────────────
# Chat completions — the text, plus why the model stopped
# ──────────────────────────────────────────────────────────────────────────

#: Stop reasons that mean "the model was cut off", normalised to lower case.
#: ``length`` is the OpenAI shape (and what Ollama's ``done_reason`` says);
#: the others are what OpenAI-compatible servers and shims emit for the same
#: condition. Anything else — ``stop``, ``tool_calls``, absent — is a model
#: that finished on its own terms.
_TRUNCATION_STOP_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})


def _finish_reason(data: Any) -> str | None:
    """The stop reason from a chat-completions response, or ``None``.

    Reads ``choices[0].finish_reason`` (the OpenAI shape that vLLM / llama.cpp /
    LM Studio emit) and falls back to a top-level ``done_reason``, which is what
    Ollama puts on its own chat responses. Never raises: a server that reports
    nothing yields ``None``, which callers read as "cannot tell".
    """
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str) and reason:
            return reason
    reason = data.get("done_reason")
    return reason if isinstance(reason, str) and reason else None


class ChatCompletionText(str):
    """The assistant message content, carrying why generation stopped.

    A ``str`` subclass rather than a tuple or dataclass so this is a
    backwards-compatible return type: every existing caller of
    :meth:`OllamaClient.chat_completions` keeps slicing, regexing and
    ``.strip()``-ing it unchanged, while the one caller that needs to know the
    response was cut off at ``max_tokens`` reads ``.truncated``.

    Truncation used to be invisible, and the cost was not theoretical: a
    consolidation pass whose 60 s LLM call hit the token cap mid-array parsed
    to zero operations and was reported as a clean "nothing to compact".
    Callers that may receive a plain ``str`` (a test fake, a seam someone
    injected) should read it defensively:
    ``getattr(text, "truncated", False)``.
    """

    finish_reason: str | None
    truncated: bool

    def __new__(cls, content: str, *, finish_reason: str | None = None) -> ChatCompletionText:
        obj = super().__new__(cls, content)
        obj.finish_reason = finish_reason
        obj.truncated = (finish_reason or "").lower() in _TRUNCATION_STOP_REASONS
        return obj


# ──────────────────────────────────────────────────────────────────────────
# Roles — the per-endpoint routing that makes misroutes impossible
# ──────────────────────────────────────────────────────────────────────────


class OllamaRole(str, Enum):
    """Logical Ollama targets, each resolving to its own configured base URL.

    EMBED  → embedding host (never chat models).
    CHAT   → chat/summarization host.
    CONSOLIDATION → the consolidation LLM host (may differ from CHAT).
    """

    EMBED = "embed"
    CHAT = "chat"
    CONSOLIDATION = "consolidation"


def _resolve_base_url(role: OllamaRole) -> str:
    """Resolve a role's base URL from the live config singleton.

    Resolved per call (not cached) so env-var / config reloads and test
    monkeypatching take effect immediately. Mirrors the existing fallbacks:
    the chat/consolidation hosts fall back to the primary embed URL when their
    dedicated URL is unset, matching today's ``auto_summary.ollama_url or
    embeddings.primary.url`` behaviour.
    """
    primary = config.embeddings.primary.url
    if role is OllamaRole.EMBED:
        return primary
    if role is OllamaRole.CHAT:
        return getattr(config.auto_summary, "ollama_url", None) or primary
    if role is OllamaRole.CONSOLIDATION:
        # consolidation.llm_url is the canonical field; fall back to chat, then primary.
        consolidation = getattr(config, "consolidation", None)
        url = getattr(consolidation, "llm_url", None) if consolidation else None
        return url or getattr(config.auto_summary, "ollama_url", None) or primary
    return primary  # pragma: no cover — exhaustive above


def _embed_dialect() -> str:
    """The configured embed wire dialect (``"ollama"`` or ``"openai"``), read live."""
    return config.embeddings.primary.dialect


_OPENAI_EMBED_PATH = "/v1/embeddings"


def _openai_embed_base_url() -> str:
    """The EMBED host with any trailing ``/v1`` removed.

    OpenAI-style servers are commonly configured as ``http://host:8080/v1``
    (the way an OpenAI SDK ``base_url`` is written); the request path already
    carries ``/v1``, so strip it once rather than emitting ``/v1/v1/embeddings``.
    """
    base = _resolve_base_url(OllamaRole.EMBED).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


# ──────────────────────────────────────────────────────────────────────────
# Circuit breaker
# ──────────────────────────────────────────────────────────────────────────


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass
class CircuitBreaker:
    """Per-role circuit breaker.

    Opens after ``fail_threshold`` consecutive failures observed within
    ``window_seconds``. While open, :meth:`allow` returns ``False`` until
    ``cooldown_seconds`` have elapsed, after which a single half-open probe is
    permitted; its outcome closes (success) or re-opens (failure) the circuit.

    All time reads go through ``monotonic`` so tests can inject a fake clock.
    """

    fail_threshold: int = 5
    window_seconds: float = 30.0
    cooldown_seconds: float = 60.0
    monotonic: Callable[[], float] = time.monotonic

    state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _first_failure_at: float | None = field(default=None, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def allow(self) -> bool:
        """Return True if a call may proceed; transition OPEN→HALF_OPEN on cooldown."""
        if self.state is CircuitState.OPEN:
            assert self._opened_at is not None
            if self.monotonic() - self._opened_at >= self.cooldown_seconds:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # CLOSED or HALF_OPEN both allow the call through.
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._first_failure_at = None
        self._opened_at = None
        self.state = CircuitState.CLOSED

    def record_failure(self) -> bool:
        """Record a failure. Returns True if this failure *opened* the circuit."""
        now = self.monotonic()
        # A half-open probe failure immediately re-opens.
        if self.state is CircuitState.HALF_OPEN:
            self._opened_at = now
            self.state = CircuitState.OPEN
            return True

        # Reset the consecutive run if the prior failure aged out of the window.
        if self._first_failure_at is None or (now - self._first_failure_at) > self.window_seconds:
            self._first_failure_at = now
            self._consecutive_failures = 0

        self._consecutive_failures += 1
        if (
            self.state is CircuitState.CLOSED
            and self._consecutive_failures >= self.fail_threshold
        ):
            self._opened_at = now
            self.state = CircuitState.OPEN
            return True
        return False


# ──────────────────────────────────────────────────────────────────────────
# Rolling metrics
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _Sample:
    at: float          # wall-clock seconds (for windowing)
    latency_ms: float
    ok: bool


class RollingMetrics:
    """Per-role rolling-window latency + error-rate tracker for /status & doctor."""

    def __init__(self, window_seconds: float = 300.0, now: Callable[[], float] = time.time) -> None:
        self.window_seconds = window_seconds
        self._now = now
        self._samples: Deque[_Sample] = deque()

    def record(self, latency_ms: float, ok: bool) -> None:
        self._samples.append(_Sample(self._now(), latency_ms, ok))
        self._evict()

    def _evict(self) -> None:
        cutoff = self._now() - self.window_seconds
        while self._samples and self._samples[0].at < cutoff:
            self._samples.popleft()

    def snapshot(self) -> dict[str, Any]:
        self._evict()
        latencies = sorted(s.latency_ms for s in self._samples)
        total = len(self._samples)
        errors = sum(1 for s in self._samples if not s.ok)
        return {
            "count_5m": total,
            "error_rate_5m": round(errors / total, 4) if total else 0.0,
            "p50_ms": _percentile(latencies, 0.50),
            "p95_ms": _percentile(latencies, 0.95),
        }


def _percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list; None when empty."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(sorted_values[0], 2)
    # Nearest-rank: rank = ceil(q * N), clamped to [1, N].
    import math
    rank = max(1, min(len(sorted_values), math.ceil(q * len(sorted_values))))
    return round(sorted_values[rank - 1], 2)


# ──────────────────────────────────────────────────────────────────────────
# Retry policy
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class RetryPolicy:
    """Exponential backoff with jitter for transient Ollama failures.

    ``retries`` is the number of *additional* attempts after the first, so
    ``retries=0`` means single-shot (used by latency-sensitive paths). Backoff
    for attempt ``i`` (0-indexed) is ``base * (factor ** i)`` capped at
    ``max_backoff``, then multiplied by a uniform jitter in ``[1-jitter, 1]``.
    """

    retries: int = 3
    base_seconds: float = 0.25
    factor: float = 4.0
    max_backoff: float = 4.0
    jitter: float = 0.25

    def backoff(self, attempt: int, rng: random.Random) -> float:
        raw = min(self.base_seconds * (self.factor ** attempt), self.max_backoff)
        return raw * (1.0 - rng.uniform(0.0, self.jitter))


# Retryable transient httpx errors (connect/read/pool timeouts, connect errors).
_RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)


# ──────────────────────────────────────────────────────────────────────────
# The client
# ──────────────────────────────────────────────────────────────────────────


class OllamaClient:
    """Thread-safe, pooled, circuit-broken Ollama client.

    One instance per process is the intended usage (see :func:`get_ollama_client`),
    so the underlying ``httpx.Client`` connection pool is shared. All public
    request methods are safe to call from multiple threads concurrently; circuit
    and metrics state is guarded by a lock.
    """

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy | None = None,
        circuit_factory: Callable[[], CircuitBreaker] | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._retry = retry_policy or RetryPolicy()
        self._circuit_factory = circuit_factory or (lambda: CircuitBreaker(monotonic=monotonic))
        self._client = http_client or httpx.Client()
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._circuits: dict[OllamaRole, CircuitBreaker] = {}
        self._metrics: dict[OllamaRole, RollingMetrics] = {}
        self._embed_ok_once = False

    # -- internal state accessors (lock-guarded) --------------------------------

    def _circuit(self, role: OllamaRole) -> CircuitBreaker:
        with self._lock:
            cb = self._circuits.get(role)
            if cb is None:
                cb = self._circuit_factory()
                self._circuits[role] = cb
            return cb

    def _metric(self, role: OllamaRole) -> RollingMetrics:
        with self._lock:
            m = self._metrics.get(role)
            if m is None:
                m = RollingMetrics(now=self._now)
                self._metrics[role] = m
            return m

    def circuit_state(self, role: OllamaRole) -> CircuitState:
        return self._circuit(role).state

    def metrics(self) -> dict[str, Any]:
        """Per-role snapshot for ``/status`` and the doctor Ollama check.

        Shape: ``{"embed": {p50_ms, p95_ms, error_rate_5m, count_5m,
        circuit_state}, "chat": {...}, ...}`` covering every role observed so far.
        """
        out: dict[str, Any] = {}
        with self._lock:
            roles = set(self._metrics) | set(self._circuits)
        for role in roles:
            snap = self._metric(role).snapshot()
            snap["circuit_state"] = self._circuit(role).state.value
            out[role.value] = snap
        return out

    # -- the core request path --------------------------------------------------

    def _request_json(
        self,
        role: OllamaRole,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float | httpx.Timeout,
        retries: int | None,
        model: str | None,
        op: str,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """POST ``payload`` to ``{role_url}{path}``, returning parsed JSON.

        ``base_url`` overrides the role's configured URL for this call — used by
        the consolidation fallback chain, where each attempt targets a different
        host while staying under the same role's circuit/metrics bucket.

        Applies circuit breaking, retry/backoff, structured logging, and metrics.
        Raises a typed :class:`OllamaError` subclass on failure; never leaks a
        bare httpx exception.
        """
        cb = self._circuit(role)
        if not cb.allow():
            self._emit(
                "circuit_open_fast_fail", role, path, model, latency_ms=0.0,
                retry_count=0, circuit_state=cb.state.value, outcome="fast_fail",
                op=op, level=logging.WARNING,
            )
            raise OllamaCircuitOpen(
                f"Ollama circuit open for role={role.value} — fast-failing {op} "
                f"(cooldown {self._retry_cooldown(cb):.0f}s). Last known: degraded/unreachable.",
                role=role.value, model=model,
            )

        max_retries = self._retry.retries if retries is None else retries
        url = f"{base_url or _resolve_base_url(role)}{path}"
        last_exc: Exception | None = None
        t_start = self._monotonic()

        for attempt in range(max_retries + 1):
            t0 = self._monotonic()
            try:
                resp = self._client.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError) as je:
                    # Ollama answered but the body wasn't JSON. Treat like a 4xx:
                    # permanent for this call, don't retry, don't trip the breaker
                    # (the host is up). Wrap so callers never see a bare decode error.
                    self._on_failure(role, (self._monotonic() - t0) * 1000.0, trip=False)
                    self._emit(
                        "request", role, path, model,
                        latency_ms=(self._monotonic() - t0) * 1000.0,
                        retry_count=attempt, circuit_state=cb.state.value,
                        outcome="bad_body", op=op, level=logging.WARNING,
                    )
                    raise OllamaError(
                        f"Ollama returned a non-JSON body for {op} (role={role.value}): {je}",
                        role=role.value, model=model,
                    ) from je
                latency_ms = (self._monotonic() - t0) * 1000.0
                self._on_success(role, latency_ms)
                self._emit(
                    "request", role, path, model, latency_ms=latency_ms,
                    retry_count=attempt, circuit_state=cb.state.value,
                    outcome="ok", op=op, level=logging.INFO,
                )
                return data
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                last_exc = e
                # 4xx is a permanent caller/payload error — do not retry, do not
                # trip the breaker (Ollama is up and answering).
                if status < 500:
                    self._on_failure(role, (self._monotonic() - t0) * 1000.0, trip=False)
                    self._emit(
                        "request", role, path, model,
                        latency_ms=(self._monotonic() - t0) * 1000.0,
                        retry_count=attempt, circuit_state=cb.state.value,
                        outcome=f"http_{status}", op=op, level=logging.WARNING,
                    )
                    # Carry the server's own explanation: an OpenAI-style 400
                    # body names the rejected input, and `str(e)` alone is just
                    # the status line plus an MDN link.
                    try:
                        body_4xx = (e.response.text or "").strip()[:300]
                    except Exception:
                        body_4xx = ""
                    raise OllamaError(
                        f"Ollama returned HTTP {status} for {op} (role={role.value}): {e}"
                        + (f" body={body_4xx!r}" if body_4xx else ""),
                        role=role.value, model=model, status_code=status,
                    ) from e
                # A 5xx whose body names a per-input model failure (bge-m3
                # NaN vector the server cannot serialise) is deterministic for
                # this input: retrying wastes the backoff, and tripping the
                # breaker would punish a healthy backend. Treat like a 4xx —
                # permanent, no retry, no trip — but typed so the embed path
                # can degrade per-input.
                try:
                    body_text = e.response.text or ""
                except Exception:
                    body_text = ""
                if _is_input_error_message(body_text):
                    self._on_failure(role, (self._monotonic() - t0) * 1000.0, trip=False)
                    self._emit(
                        "request", role, path, model,
                        latency_ms=(self._monotonic() - t0) * 1000.0,
                        retry_count=attempt, circuit_state=cb.state.value,
                        outcome=f"http_{status}_input", op=op, level=logging.WARNING,
                    )
                    raise OllamaInputError(
                        f"Ollama {op} failed for this input (role={role.value}, "
                        f"HTTP {status}): {body_text.strip()}",
                        role=role.value, model=model, status_code=status,
                    ) from e
                # 5xx is transient — fall through to retry handling.
                if attempt < max_retries:
                    self._backoff_sleep(attempt)
                    continue
            except _RETRYABLE_EXC as e:
                last_exc = e
                if attempt < max_retries:
                    self._backoff_sleep(attempt)
                    continue
            # Out of retries (or non-retryable transient): record + trip + raise.
            latency_ms = (self._monotonic() - t0) * 1000.0
            opened = self._on_failure(role, latency_ms, trip=True)
            total_ms = (self._monotonic() - t_start) * 1000.0
            self._emit(
                "request", role, path, model, latency_ms=total_ms,
                retry_count=attempt, circuit_state=cb.state.value,
                outcome="timeout" if _is_timeout(last_exc) else "unreachable",
                op=op, level=logging.WARNING,
            )
            if opened:
                self._emit(
                    "circuit_opened", role, path, model, latency_ms=total_ms,
                    retry_count=attempt, circuit_state=cb.state.value,
                    outcome="circuit_opened", op=op, level=logging.WARNING,
                )
            raise self._wrap_exc(last_exc, role, model, op) from last_exc

        # Unreachable: the loop always returns or raises. Defensive only.
        raise self._wrap_exc(last_exc, role, model, op)  # pragma: no cover

    # -- helpers ----------------------------------------------------------------

    def _backoff_sleep(self, attempt: int) -> None:
        self._sleep(self._retry.backoff(attempt, self._rng))

    def _retry_cooldown(self, cb: CircuitBreaker) -> float:
        if cb._opened_at is None:
            return cb.cooldown_seconds
        remaining = cb.cooldown_seconds - (self._monotonic() - cb._opened_at)
        return max(0.0, remaining)

    def _on_success(self, role: OllamaRole, latency_ms: float) -> None:
        self._circuit(role).record_success()
        self._metric(role).record(latency_ms, ok=True)

    def _on_failure(self, role: OllamaRole, latency_ms: float, *, trip: bool) -> bool:
        self._metric(role).record(latency_ms, ok=False)
        if trip:
            return self._circuit(role).record_failure()
        return False

    @staticmethod
    def _wrap_exc(exc: Exception | None, role: OllamaRole, model: str | None, op: str) -> OllamaError:
        if _is_timeout(exc):
            return OllamaTimeout(
                f"Ollama {op} timed out (role={role.value}): {exc}",
                role=role.value, model=model,
            )
        return OllamaUnreachable(
            f"Ollama {op} failed to reach host (role={role.value}): {exc}",
            role=role.value, model=model,
        )

    def _emit(
        self, event: str, role: OllamaRole, endpoint: str, model: str | None,
        *, latency_ms: float, retry_count: int, circuit_state: str, outcome: str,
        op: str, level: int,
    ) -> None:
        """Emit one structured JSON-line log event (the logging audit field convention)."""
        event_logger.log(level, json.dumps({
            "event": event,
            "op": op,
            "role": role.value,
            "endpoint": endpoint,
            "model": model,
            "latency_ms": round(latency_ms, 1),
            "retry_count": retry_count,
            "circuit_state": circuit_state,
            "outcome": outcome,
        }, sort_keys=True))

    # -- typed public methods (per-role binding) --------------------------------

    def embed(
        self, text: str, *, model: str | None = None,
        timeout: float | httpx.Timeout | None = None, retries: int | None = None,
    ) -> list[float]:
        """Return the embedding vector for ``text`` from the EMBED host.

        Owns the full embed contract: tries ``/api/embed`` (newer Ollama, payload
        key ``input``) and falls back to the legacy ``/api/embeddings`` (key
        ``prompt``) **only** on a 404 (API-version mismatch). Parses either
        response shape (``embeddings`` list-of-lists or legacy ``embedding``).
        With ``embeddings.primary.dialect: openai`` the call is routed to
        :meth:`_embed_openai` (``/v1/embeddings``) instead, same contract.

        Raises:
            EmbeddingContextError: when Ollama reports a context-window overflow
                (HTTP 200 with an error body) — re-raised immediately, not retried
                against the other endpoint (the overflow is the model's, not the
                endpoint's).
            EmbeddingInputError: when the backend deterministically rejects this
                one input (e.g. a NaN vector it cannot serialise, HTTP 500) —
                raised after one CPU retry when the rejection is NaN-shaped and
                ``embeddings.primary.nan_cpu_retry`` is on (the GPU path's F16
                overflow is the known cause; the CPU path embeds the same input
                correctly), otherwise immediately. No legacy-endpoint fallback
                (same model, same input), no circuit-breaker hit.
            OllamaTimeout / OllamaUnreachable / OllamaError: on transient failure
                after the retry/circuit policy, or an unexpected response shape.
        """
        mdl = model or config.embeddings.primary.model
        tmo = timeout if timeout is not None else httpx.Timeout(
            config.embeddings.primary.timeout_seconds,
            connect=config.embeddings.primary.connect_timeout_seconds,
        )
        if _embed_dialect() == "openai":
            return self._embed_openai(
                [text], model=mdl, timeout=tmo, retries=retries, op="embed",
            )[0]
        last_exc: OllamaError | None = None
        for endpoint, payload_key in (("/api/embed", "input"), ("/api/embeddings", "prompt")):
            try:
                data = self._request_json(
                    OllamaRole.EMBED, endpoint, {"model": mdl, payload_key: text},
                    timeout=tmo, retries=retries, model=mdl, op="embed",
                )
            except OllamaInputError as e:
                # Deterministic per-input failure (NaN vector): the legacy
                # endpoint runs the same model on the same input, so no
                # endpoint fallback. A NaN-shaped rejection gets one retry on
                # the CPU path (see PrimaryEmbeddingConfig.nan_cpu_retry);
                # anything else surfaces the typed per-input signal now.
                if config.embeddings.primary.nan_cpu_retry and _is_nan_message(str(e)):
                    vec = self._embed_cpu_retry(endpoint, payload_key, mdl, text, tmo)
                    if vec is not None:
                        self._embed_ok_once = True
                        return vec
                raise EmbeddingInputError(
                    model=mdl, text_len=len(text), ollama_message=str(e)
                ) from e
            except OllamaError as e:
                last_exc = e
                # Old Ollama lacks /api/embed → 404. Fall back to the legacy
                # endpoint. Any other error (timeout, connect, 5xx-exhausted,
                # circuit-open) is not an API-version issue — re-raise.
                if e.status_code == 404 and endpoint == "/api/embed":
                    continue
                raise
            vec = _extract_embedding_vector(data)
            if vec is not None:
                self._embed_ok_once = True
                return vec
            # 200 OK but no vector. Ollama reports ctx overflow as an error body
            # raise immediately, do not try the other endpoint.
            err_msg = data.get("error", "") if isinstance(data, dict) else ""
            if err_msg and _is_ctx_overflow_message(err_msg):
                raise EmbeddingContextError(
                    model=mdl, text_len=len(text), ollama_message=err_msg
                )
            # Unexpected shape — record and try the next endpoint.
            keys = sorted(data.keys()) if isinstance(data, dict) else []
            event_logger.warning(json.dumps({
                "event": "embed_unexpected_shape", "op": "embed", "role": "embed",
                "endpoint": endpoint, "model": mdl, "response_keys": keys,
            }, sort_keys=True))
            last_exc = OllamaError(
                f"unexpected embed response shape from {endpoint} (response_keys={keys})",
                role="embed", model=mdl,
            )
        # Both endpoints exhausted without a vector.
        raise last_exc or OllamaUnreachable(
            "embed: all endpoints exhausted", role="embed", model=mdl
        )

    def _embed_cpu_retry(
        self, endpoint: str, payload_key: str, mdl: str, text: str,
        tmo: float | httpx.Timeout,
    ) -> list[float] | None:
        """One retry of a NaN-rejected input on the CPU path. ``keep_alive: 0``
        so the CPU-resident instance unloads after this call and the next
        normal request reloads on the GPU — without it a long server-side
        keep_alive pins the model on the CPU for every later caller. Returns
        the vector, or ``None`` on any failure (the caller then raises the
        original typed error)."""
        try:
            data = self._request_json(
                OllamaRole.EMBED, endpoint,
                {"model": mdl, payload_key: text, "keep_alive": 0, "options": {"num_gpu": 0}},
                timeout=tmo, retries=0, model=mdl, op="embed",
            )
        except OllamaError as e:
            event_logger.warning(json.dumps({
                "event": "embed_nan_cpu_retry", "op": "embed", "role": "embed",
                "endpoint": endpoint, "model": mdl, "outcome": "failed", "error": str(e)[:200],
            }, sort_keys=True))
            return None
        vec = _extract_embedding_vector(data)
        event_logger.info(json.dumps({
            "event": "embed_nan_cpu_retry", "op": "embed", "role": "embed",
            "endpoint": endpoint, "model": mdl,
            "outcome": "ok" if vec is not None else "no_vector", "text_len": len(text),
        }, sort_keys=True))
        return vec

    def embed_many(
        self, texts: list[str], *, model: str | None = None,
        timeout: float | httpx.Timeout | None = None, retries: int | None = None,
    ) -> list[list[float]]:
        """Return one ordered embedding per input using one ``/api/embed`` call.

        The modern Ollama endpoint accepts a list in ``input``. The response is
        accepted only when its cardinality, vector shapes, and dimensions are
        valid for the entire batch. Older Ollama versions that return 404 for
        ``/api/embed`` retain compatibility through the scalar legacy fallback.

        An empty input is a no-op and performs no network request. With
        ``embeddings.primary.dialect: openai`` the batch goes to
        :meth:`_embed_openai` as one ``/v1/embeddings`` call.
        """
        if not texts:
            return []

        mdl = model or config.embeddings.primary.model
        tmo = timeout if timeout is not None else httpx.Timeout(
            config.embeddings.primary.timeout_seconds,
            connect=config.embeddings.primary.connect_timeout_seconds,
        )
        if _embed_dialect() == "openai":
            return self._embed_openai(
                texts, model=mdl, timeout=tmo, retries=retries, op="embed_many",
            )
        try:
            data = self._request_json(
                OllamaRole.EMBED,
                "/api/embed",
                {"model": mdl, "input": texts},
                timeout=tmo,
                retries=retries,
                model=mdl,
                op="embed_many",
            )
        except OllamaInputError as e:
            raise EmbeddingInputError(
                model=mdl,
                text_len=sum(len(text) for text in texts),
                ollama_message=str(e),
            ) from e
        except OllamaError as e:
            if e.status_code == 404:
                return [
                    self.embed(text, model=mdl, timeout=tmo, retries=retries)
                    for text in texts
                ]
            raise

        embeddings = _extract_embedding_batch(data, expected_count=len(texts))
        if embeddings is not None:
            self._embed_ok_once = True
            return embeddings

        err_msg = data.get("error", "") if isinstance(data, dict) else ""
        if err_msg and _is_ctx_overflow_message(err_msg):
            raise EmbeddingContextError(
                model=mdl,
                text_len=sum(len(text) for text in texts),
                ollama_message=err_msg,
            )

        keys = sorted(data.keys()) if isinstance(data, dict) else []
        raw_embeddings = data.get("embeddings") if isinstance(data, dict) else None
        returned_count = len(raw_embeddings) if isinstance(raw_embeddings, list) else None
        event_logger.warning(json.dumps({
            "event": "embed_unexpected_shape",
            "op": "embed_many",
            "role": "embed",
            "endpoint": "/api/embed",
            "model": mdl,
            "response_keys": keys,
            "expected_count": len(texts),
            "returned_count": returned_count,
        }, sort_keys=True))
        raise OllamaError(
            "unexpected embed batch response shape from /api/embed "
            f"(response_keys={keys}, expected_count={len(texts)}, "
            f"returned_count={returned_count})",
            role="embed",
            model=mdl,
        )

    def _embed_openai(
        self, texts: list[str], *, model: str,
        timeout: float | httpx.Timeout, retries: int | None, op: str,
    ) -> list[list[float]]:
        """Embed ``texts`` through an OpenAI-compatible ``/v1/embeddings`` server.

        The ``embeddings.primary.dialect: openai`` path for llama.cpp / vLLM /
        LM Studio. One POST of ``{"model", "input": [...]}``
        under the EMBED role, so it shares the Ollama path's circuit breaker,
        retry/backoff, metrics, and structured logging by construction. There
        is no legacy-endpoint fallback here — the OpenAI shape has only one
        endpoint — and no ``/api/show`` preflight (see ``embedder``).

        Error mapping keeps the caller contract identical to the Ollama path:

        * HTTP 400 — by OpenAI convention ``invalid_request_error``. The
          payload is fixed apart from the input, so this is the server
          rejecting *this input* (vLLM's over-length reply, for one) →
          :class:`EmbeddingInputError`: no retry, no breaker hit, the chunk
          stays keyword-searchable and is re-embedded on the next pass.
        * a 5xx whose body names a per-input failure (llama.cpp's "input is
          too large to process") → :class:`EmbeddingInputError`, same reasons.
        * anything else (401/404, timeouts, 5xx exhausted, circuit open,
          malformed body) → the usual :class:`OllamaError` family, which the
          embedder wraps as ``EmbeddingUnavailable``.

        :class:`EmbeddingContextError` is not raised on this path: it is the
        typed form of Ollama's HTTP-200 error body, which these servers do
        not emit.
        """
        text_len = sum(len(text) for text in texts)
        try:
            data = self._request_json(
                OllamaRole.EMBED, _OPENAI_EMBED_PATH, {"model": model, "input": texts},
                timeout=timeout, retries=retries, model=model, op=op,
                base_url=_openai_embed_base_url(),
            )
        except OllamaInputError as e:
            raise EmbeddingInputError(
                model=model, text_len=text_len, ollama_message=str(e)
            ) from e
        except OllamaError as e:
            if e.status_code == 400:
                raise EmbeddingInputError(
                    model=model, text_len=text_len, ollama_message=str(e)
                ) from e
            raise

        embeddings = _extract_openai_embedding_batch(data, expected_count=len(texts))
        if embeddings is not None:
            self._embed_ok_once = True
            return embeddings

        keys = sorted(data.keys()) if isinstance(data, dict) else []
        raw_items = data.get("data") if isinstance(data, dict) else None
        returned_count = len(raw_items) if isinstance(raw_items, list) else None
        event_logger.warning(json.dumps({
            "event": "embed_unexpected_shape",
            "op": op,
            "role": "embed",
            "endpoint": _OPENAI_EMBED_PATH,
            "model": model,
            "response_keys": keys,
            "expected_count": len(texts),
            "returned_count": returned_count,
        }, sort_keys=True))
        raise OllamaError(
            f"unexpected embed response shape from {_OPENAI_EMBED_PATH} "
            f"(response_keys={keys}, expected_count={len(texts)}, "
            f"returned_count={returned_count})",
            role="embed",
            model=model,
        )

    def generate(
        self, prompt: str, *, model: str | None = None,
        timeout: float | httpx.Timeout | None = None, retries: int | None = None,
        role: OllamaRole = OllamaRole.CHAT, **options: Any,
    ) -> dict[str, Any]:
        """POST to the chat host's ``/api/generate`` (non-streaming)."""
        mdl = model or config.auto_summary.model
        tmo = timeout if timeout is not None else 90.0
        payload: dict[str, Any] = {"model": mdl, "prompt": prompt, "stream": False}
        if options:
            payload["options"] = options
        return self._request_json(
            role, "/api/generate", payload,
            timeout=tmo, retries=retries, model=mdl, op="generate",
        )

    def show(
        self, model: str, *, role: OllamaRole = OllamaRole.EMBED,
        timeout: float | httpx.Timeout | None = None, retries: int | None = None,
    ) -> dict[str, Any]:
        """POST ``/api/show`` for model metadata (e.g. the embed ctx preflight)."""
        tmo = timeout if timeout is not None else httpx.Timeout(5.0, connect=3.0)
        return self._request_json(
            role, "/api/show", {"name": model},
            timeout=tmo, retries=retries, model=model, op="show",
        )

    def chat_completions(
        self, messages: list[dict[str, str]], *, model: str,
        base_url: str | None = None, temperature: float | None = None,
        max_tokens: int | None = None, timeout: float | httpx.Timeout = 60.0,
        retries: int | None = None, role: OllamaRole = OllamaRole.CONSOLIDATION,
    ) -> str:
        """POST to an **OpenAI-compatible** ``/v1/chat/completions`` and return the
        assistant message content.

        Used by consolidation + lint, which target an OpenAI-compatible server
        (vLLM / llama.cpp) rather than Ollama's native API. ``base_url`` overrides
        the role URL per call so the consolidation fallback chain can walk several
        hosts. Raises :class:`OllamaError` on transport failure *or* a malformed
        response (missing ``choices[0].message.content``).

        The return value is a :class:`ChatCompletionText` — a ``str`` carrying
        ``finish_reason`` / ``truncated``, so a caller that needs to know the
        model was cut off at ``max_tokens`` can ask, and every caller that just
        wants the text is unchanged.
        """
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        data = self._request_json(
            role, "/v1/chat/completions", payload,
            timeout=timeout, retries=retries, model=model,
            op="chat_completions", base_url=base_url,
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            keys = sorted(data.keys()) if isinstance(data, dict) else "?"
            raise OllamaError(
                f"malformed chat_completions response (keys={keys})",
                role=role.value, model=model,
            ) from e
        if not isinstance(content, str):
            # A server that answers with a null/structured content is already a
            # caller-visible bug; wrapping it would only turn it into the string
            # "None". Pass it through exactly as before.
            return content
        return ChatCompletionText(content, finish_reason=_finish_reason(data))

    def ping(self, role: OllamaRole = OllamaRole.EMBED, *, timeout: float = 2.0) -> bool:
        """Liveness probe — a raw GET to the role's base URL.

        Deliberately bypasses the circuit breaker, retries, and metrics: a
        liveness check must report the host's *actual* reachability, not the
        breaker's state (otherwise ``/health`` would report "down" during a
        cooldown even after Ollama recovered). Returns True if the host answered
        at all (any HTTP status), False on connect error / timeout.
        """
        try:
            self._client.get(_resolve_base_url(role), timeout=timeout)
            return True
        except (httpx.HTTPError, OSError):
            return False

    @property
    def has_embedded_ok(self) -> bool:
        """True once any embed has succeeded in this process.

        The cold-embed fast path in ``index_file`` uses this to decide
        whether a bounded :meth:`probe_embed` is needed before attempting
        inline embeds — a proven-warm embed path skips the probe entirely.
        """
        return self._embed_ok_once

    def probe_embed(self, *, timeout: float = 2.0) -> bool:
        """Functional embed probe — confirm the embed *model* produces a vector.

        Unlike :meth:`ping` (a raw GET that returns True if the daemon answers at
        *all*), this runs a real one-token embed with a short timeout and no
        retries. It exists so ``/status`` cannot report a cold or absent
        embedding model as healthy: on a box where Ollama is up but ``bge-m3`` is
        unpulled or cold, ``ping`` is True while embeds hang — the false-green
        this closes. Returns True only when a non-empty vector comes back within
        *timeout*. A single bounded attempt: an open circuit fast-fails here
        instead of waiting.
        """
        try:
            return bool(self.embed("ok", timeout=timeout, retries=0))
        except (OllamaError, EmbeddingContextError):
            return False

    def close(self) -> None:
        self._client.close()


def _is_timeout(exc: Exception | None) -> bool:
    return isinstance(exc, (httpx.TimeoutException,))


# ──────────────────────────────────────────────────────────────────────────
# Process-wide singleton
# ──────────────────────────────────────────────────────────────────────────

_singleton: OllamaClient | None = None
_singleton_lock = threading.Lock()


def get_ollama_client() -> OllamaClient:
    """Return the process-wide :class:`OllamaClient`, creating it on first use."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = OllamaClient()
    return _singleton
