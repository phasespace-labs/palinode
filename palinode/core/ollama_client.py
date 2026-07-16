"""Centralized Ollama client — the single mediation layer for palinode↔Ollama I/O.

#338 (Ollama traffic-surface hardening), Phase 1+2+3. Before this module every
caller built its own ``httpx`` request, set its own timeout, and swallowed its
own errors, so palinode was an *unmediated* dependency on a single Ollama host's
instantaneous responsiveness. This client replaces that with one seam:

* **Per-role routing** (``OllamaRole``) — ``embed`` resolves to the configured
  embedding host, while ``chat`` resolves to the configured chat/generate host. Typed
  methods (:meth:`OllamaClient.embed`, :meth:`OllamaClient.generate`,
  :meth:`OllamaClient.chat`) bind to a role, so an embed call cannot be sent to
  the chat host by construction. URLs are resolved *per call* from the live
  config singleton, so env/config changes and test monkeypatching are honoured.
* **Retry with jittered backoff** on transient failures (read timeout, connect
  error, HTTP 5xx). Per-call ``retries=0`` opts a latency-sensitive path out
  (e.g. the #336 inline-description path must not turn one 5 s timeout into
  three).
* **Circuit breaker per role** that *opens loudly* (WARNING log + surfaced
  state). When open, calls fast-fail with :class:`OllamaCircuitOpen` in well
  under a millisecond instead of waiting for a timeout. Half-opens after a
  cooldown to probe recovery.
* **Structured JSON-line logging** (#337): every call emits one line with
  ``{event, role, endpoint, model, latency_ms, retry_count, circuit_state,
  outcome}`` so an operator can grep a single greppable shape.
* **Rolling latency/error metrics** per role (5-minute window) exposed via
  :meth:`OllamaClient.metrics` for ``/status`` and the ``palinode doctor``
  Ollama check.

Callers migrate onto this seam incrementally (see #338 phasing). This module is
additive — it changes no existing behaviour until a caller is pointed at it.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Optional

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


def _is_ctx_overflow_message(message: str) -> bool:
    """Return True if the Ollama error message indicates context overflow."""
    msg_lower = (message or "").lower()
    return any(p in msg_lower for p in _CTX_OVERFLOW_PATTERNS)


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
                    raise OllamaError(
                        f"Ollama returned HTTP {status} for {op} (role={role.value}): {e}",
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
        """Emit one structured JSON-line log event (#337 field convention)."""
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

        Raises:
            EmbeddingContextError: when Ollama reports a context-window overflow
                (HTTP 200 with an error body) — re-raised immediately, not retried
                against the other endpoint (the overflow is the model's, not the
                endpoint's).
            OllamaTimeout / OllamaUnreachable / OllamaError: on transient failure
                after the retry/circuit policy, or an unexpected response shape.
        """
        mdl = model or config.embeddings.primary.model
        tmo = timeout if timeout is not None else httpx.Timeout(
            config.embeddings.primary.timeout_seconds,
            connect=config.embeddings.primary.connect_timeout_seconds,
        )
        last_exc: OllamaError | None = None
        for endpoint, payload_key in (("/api/embed", "input"), ("/api/embeddings", "prompt")):
            try:
                data = self._request_json(
                    OllamaRole.EMBED, endpoint, {"model": mdl, payload_key: text},
                    timeout=tmo, retries=retries, model=mdl, op="embed",
                )
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

    def chat(
        self, messages: list[dict[str, str]], *, model: str | None = None,
        timeout: float | httpx.Timeout | None = None, retries: int | None = None,
        role: OllamaRole = OllamaRole.CHAT,
    ) -> dict[str, Any]:
        """POST to the chat host's ``/api/chat`` (non-streaming)."""
        mdl = model or config.auto_summary.model
        tmo = timeout if timeout is not None else 90.0
        return self._request_json(
            role, "/api/chat", {"model": mdl, "messages": messages, "stream": False},
            timeout=tmo, retries=retries, model=mdl, op="chat",
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
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            keys = sorted(data.keys()) if isinstance(data, dict) else "?"
            raise OllamaError(
                f"malformed chat_completions response (keys={keys})",
                role=role.value, model=model,
            ) from e

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
