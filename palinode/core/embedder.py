"""
Palinode Embedder — local BGE-M3 via Ollama.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from palinode.core.config import config
from palinode.core.ollama_client import (
    EmbeddingContextError,
    EmbeddingInputError,
    OllamaError,
    OllamaRole,
    _is_ctx_overflow_message,  # noqa: F401  — deliberate re-export, see below
    get_ollama_client,
)

logger = logging.getLogger(__name__)

# EmbeddingContextError and _is_ctx_overflow_message now live in
# palinode.core.ollama_client (so the client can raise the error from inside the
# embed path without a circular import) and are re-exported here for backward
# compatibility — existing `from palinode.core.embedder import
# EmbeddingContextError` imports keep working (Phase 3).
__all__ = [
    "EmbeddingContextError",
    "EmbeddingInputError",
    "EmbeddingUnavailable",
    "embed",
    "embed_many",
    "check_model_context",
]


# --------------------------------------------------------------------------
# Backend-failure signal — the embedder boundary contract
# --------------------------------------------------------------------------


class EmbeddingUnavailable(RuntimeError):
    """Raised by ``embed()`` when the local backend fails to produce a vector.

    Historically this was a silent contract: a connectivity/timeout/HTTP
    failure against Ollama logged a WARNING and returned ``[]``. Every
    in-process caller that skipped the REST layer's broad exception handling
    inherited that falsy vector by default, and the failure resurfaced two
    modules downstream — sqlite-vec rejecting a zero-length query vector in
    ``store.search`` / ``search_hybrid`` with an ``OperationalError`` that
    names the SQL layer, not the network failure that actually caused it.
    Raising here attributes the failure at the point it happened.

    Attributes:
        backend: which embedding backend failed (always ``"local"``).
        model: the Ollama model name that was asked to embed.
        text_len: character length of the input that failed to embed (never
            the text itself — logs/exceptions must not carry raw content).
        cause: ``str()`` of the underlying :class:`OllamaError`, also
            available via ``__cause__`` for anything that walks the chain.

    Recovery: this is the backend failing, not the caller's input — check
    ``palinode doctor`` (the ``ollama_circuit_health`` check covers the embed
    role), confirm Ollama is reachable and the model is pulled, then retry.

    Whether to catch this or let it propagate is a per-caller decision, not
    a blanket one — two patterns are in use: a watcher/indexer/consolidation
    pass that can tolerate a missed cycle catches it and degrades (retry next
    pass); an interactive surface (search, save, triggers) lets it propagate
    so the failure reaches an operator or user instead of masquerading as an
    empty result.
    """

    def __init__(self, *, backend: str, model: str, text_len: int, cause: str) -> None:
        self.backend = backend
        self.model = model
        self.text_len = text_len
        self.cause = cause
        super().__init__(
            f"Embedding backend unavailable — backend={backend} model={model!r} "
            f"text_len={text_len} cause={cause!r}. The embedder failed to "
            f"produce a vector for this call; this is a backend outage, not a "
            f"malformed input. Recovery: run `palinode doctor` (check "
            f"ollama_circuit_health for the embed role), confirm Ollama is "
            f"reachable and the model is pulled, then retry."
        )


# --------------------------------------------------------------------------
# Context-window preflight check
# --------------------------------------------------------------------------

# Minimum expected num_ctx for the embed model. bge-m3 supports 8192;
# the Ollama default is 4096 which silently truncates/errors on large chunks.
_MIN_EXPECTED_CTX = 8192

# Preflight guard — only check once per process.
_preflight_lock = threading.Lock()
_preflight_done = False

# One-time notice: the first time an embed fails (cold/absent model, unreachable
# Ollama), tell the operator — once, plainly — that Palinode is running in
# keyword-only mode and how to enable semantic search. Avoids burying the signal
# under per-call WARNINGs on a fresh install.
_keyword_only_lock = threading.Lock()
_keyword_only_notice_done = False


def _notice_keyword_only_once() -> None:
    """Log the keyword-only-mode guidance exactly once per process."""
    global _keyword_only_notice_done
    with _keyword_only_lock:
        if _keyword_only_notice_done:
            return
        _keyword_only_notice_done = True
    if config.embeddings.primary.dialect == "openai":
        fix = (
            "confirm the OpenAI-compatible server at embeddings.primary.url "
            "answers POST /v1/embeddings for this model (llama.cpp: "
            "`llama-server --embedding`; vLLM / LM Studio: the model is loaded)"
        )
    else:
        fix = (
            "`ollama pull bge-m3` (or point embeddings.primary.url at a working "
            "Ollama host)"
        )
    logger.warning(
        "Embeddings unavailable — running in keyword-only mode (BM25/FTS5). "
        "Save, search, and audit still work; semantic recall is off until an "
        "embedder is reachable. To enable it: %s. "
        "op=embed outcome=keyword_only_mode model=%s dialect=%s",
        fix,
        config.embeddings.primary.model,
        config.embeddings.primary.dialect,
    )


def _parse_num_ctx(parameters: object) -> int | None:
    """Return the runtime ``num_ctx`` from an ``/api/show`` ``parameters`` field.

    Ollama serialises ``parameters`` as the modelfile's PARAMETER lines — a
    newline-delimited ``"key<spaces>value"`` string. Older builds returned a
    mapping. Anything else (absent, malformed, non-numeric) is ``None`` so the
    caller falls back to the capability value or skips the check.
    """
    if parameters is None:
        return None
    if isinstance(parameters, dict):
        value = parameters.get("num_ctx")
    elif isinstance(parameters, str):
        value = None
        for line in parameters.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "num_ctx":
                value = parts[1]
                break
    else:
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _capability_ctx(model_info: object) -> int | None:
    """Return the architecture context length from ``/api/show`` ``model_info``.

    The key is ``"<architecture>.context_length"`` — ``llama.context_length``
    for GGUF llama-family models, ``bert.context_length`` for bge-m3 — so any
    ``*.context_length`` suffix counts. The canonical ``llama.`` key is preferred
    when several are present.
    """
    if not isinstance(model_info, dict):
        return None
    candidates = {
        key: value for key, value in model_info.items()
        if isinstance(key, str) and key.endswith(".context_length") and value is not None
    }
    if not candidates:
        return None
    value = candidates.get("llama.context_length", next(iter(candidates.values())))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def check_model_context(
    model: Optional[str] = None,
    min_ctx: int = _MIN_EXPECTED_CTX,
) -> None:
    """Query Ollama /api/show and warn if num_ctx is below min_ctx.

    Called once at first embed (lazy preflight). Never raises — a failed
    preflight check is a warning, not a fatal error. The embed call proceeds
    regardless; this is purely diagnostic.

    Args:
        model: Model name (defaults to config).
        min_ctx: Minimum acceptable num_ctx value (default 8192 for bge-m3).
    """
    if model is None:
        model = config.embeddings.primary.model

    dialect = config.embeddings.primary.dialect
    if dialect != "ollama":
        # /api/show is Ollama-native; an OpenAI-compatible server (llama.cpp,
        # vLLM, LM Studio) has no equivalent, so the ctx guard is simply not
        # available there. Skip rather than log a spurious "check skipped".
        logger.debug(
            "embed preflight: /api/show not available for dialect=%s — skipping "
            "op=preflight model=%s",
            dialect, model,
        )
        return

    try:
        # Phase 3: route /api/show through the centralized client (EMBED
        # role). retries=0 — preflight is best-effort and must not amplify load.
        data = get_ollama_client().show(model, role=OllamaRole.EMBED, retries=0)
        # Two distinct numbers can come back, and they mean different things:
        #   - `parameters` carries the *runtime* num_ctx the model is actually
        #     loaded with. Ollama returns it as a newline-delimited
        #     "key value" string (a mapping in some older builds).
        #   - `model_info["<arch>.context_length"]` is the architecture's
        #     *capability* — "llama." for GGUF llama-family models, "bert."
        #     for bge-m3. It says what the model could do, not what it is
        #     configured to do.
        # The runtime value wins when present: a model that reports an 8192
        # capability but runs at 4096 truncates at 4096.
        runtime_ctx = _parse_num_ctx(data.get("parameters"))
        capability_ctx = _capability_ctx(data.get("model_info"))
        ctx = runtime_ctx if runtime_ctx is not None else capability_ctx
        if ctx is None:
            # We can't guarantee a key across all versions — skip the check.
            logger.debug(
                "embed preflight: could not read num_ctx from /api/show for model=%s "
                "(key not present in response — skipping ctx check)",
                model,
            )
            return

        ctx_int = int(ctx)
        if ctx_int < min_ctx:
            capability_note = (
                " (the model itself supports %d)" % capability_ctx
                if capability_ctx is not None and capability_ctx != ctx_int
                else ""
            )
            logger.warning(
                "embed preflight: model=%s has num_ctx=%d which is below the "
                "recommended minimum of %d%s. Inputs longer than %d tokens will "
                "silently fail or be truncated. Fix: create a custom Ollama "
                "modelfile with 'PARAMETER num_ctx %d' and rebuild the model.",
                model, ctx_int, min_ctx, capability_note, ctx_int, min_ctx,
            )
        else:
            logger.debug(
                "embed preflight: model=%s num_ctx=%d (>= %d — ok)",
                model, ctx_int, min_ctx,
            )
    except (OllamaError, OSError, ValueError, KeyError) as e:
        # Preflight is best-effort; never block embed on it. OllamaError covers
        # connect/timeout/HTTP/circuit-open from the centralized client.
        # INFO not DEBUG (docs/logging.md): a preflight that can't run at
        # all is worth one operator-visible line — it means the ctx guard is
        # silently inactive for this process.
        logger.info(
            "embed preflight: /api/show check skipped op=preflight model=%s error=%r",
            model, str(e),
        )


def embed(text: str) -> list[float]:
    """Generate an embedding for the given text.

    Args:
        text (str): The text to embed.

    Returns:
        list[float]: A non-empty list of floats representing the embedding
        vector. Never an empty list — a failed or misconfigured backend
        raises instead (see ``Raises``); this function has no falsy success
        return.

    Raises:
        EmbeddingContextError: When Ollama explicitly rejects the input due to
            context-window overflow. Callers that want to handle truncation
            specially should catch this specifically.
        EmbeddingInputError: When the backend deterministically rejects this
            one input (e.g. a NaN vector it cannot serialise) while remaining
            healthy for every other input. Callers should degrade per-input
            (FTS-only index, keyword-only answer), not per-backend.
        EmbeddingUnavailable: When the local backend cannot be reached, times
            out, or errors (connectivity/HTTP/circuit-open). Replaces the old
            silent-``[]`` contract — callers that want graceful degradation
            must now catch this explicitly rather than checking for a falsy
            return.
    """
    return _embed_local(text)


def embed_many(texts: list[str]) -> list[list[float]]:
    """Generate one ordered embedding per input in a single backend call.

    The whole result is validated by :class:`OllamaClient` before it is
    returned. Typed context and per-input failures propagate unchanged;
    backend failures become :class:`EmbeddingUnavailable`, matching
    :func:`embed`.
    """
    return _embed_many_local(texts)


def _run_preflight_once() -> None:
    """Run the context preflight check exactly once per process."""
    global _preflight_done
    with _preflight_lock:
        if not _preflight_done:
            _preflight_done = True
            try:
                check_model_context()
            except Exception as e:  # noqa: BLE001 — preflight must never take an embed down
                # check_model_context already swallows the expected failure
                # classes; this is the floor under everything else. Preflight
                # is best-effort by contract, so a defect in it must cost one
                # log line, never the payload the caller is embedding.
                logger.warning(
                    "embed preflight: check raised and was ignored op=preflight error=%r",
                    str(e),
                )


def _embed_local(text: str) -> list[float]:
    """Embed via local provider specified in config (defaults to Ollama BGE-M3).

    Iterates over known inference API endpoints since Ollama versions
    have changed their primary embed endpoints.

    Args:
        text (str): The text to embed.

    Returns:
        list[float]: The normalized generated embedding. Always non-empty —
        a failed call raises rather than returning a falsy vector.

    Raises:
        EmbeddingContextError: When Ollama returns an explicit context-overflow
            error. See EmbeddingContextError for recovery guidance.
        EmbeddingUnavailable: When the client fails on connectivity, timeout,
            an HTTP error, an open circuit, or an unexpected response shape.
            See EmbeddingUnavailable for recovery guidance and which callers
            should catch it.
    """
    # Lazy preflight: check num_ctx once per process so operators get an early
    # warning about misconfigured modelfiles.
    _run_preflight_once()

    model = config.embeddings.primary.model

    # Phase 3: route through the centralized client. It owns the
    # /api/embed → /api/embeddings fallback, retry/backoff, circuit breaking,
    # and the structured per-call JSON logging (palinode.ollama.events). This
    # wrapper's contract: raise EmbeddingUnavailable on connectivity/timeout
    # failure, re-raise EmbeddingContextError on a context-window overflow.
    # Never a falsy return.
    try:
        return get_ollama_client().embed(text)
    except EmbeddingContextError:
        # Typed signal — propagate so callers can truncate / split.
        raise
    except EmbeddingInputError:
        # Typed per-input signal (e.g. bge-m3 NaN vector) — propagate so
        # callers can degrade this one input (FTS-only index, keyword-only
        # answer). Deliberately NOT wrapped as EmbeddingUnavailable and NOT
        # triggering the keyword-only-mode notice: the backend is healthy.
        raise
    except OllamaError as e:
        # Connect/timeout/HTTP/circuit-open/unexpected-shape. text_len, not
        # raw text, so logs never carry user content. Structured key=value
        # per docs/logging.md — greppable on op/outcome alongside the
        # ollama_client per-call event line.
        logger.warning(
            "embed failed; raising EmbeddingUnavailable "
            "op=embed model=%s text_len=%d outcome=error error=%r",
            model, len(text), str(e),
        )
        # First failure this process → surface the plain keyword-only-mode notice
        # so a fresh-install operator sees one clear line, not just per-call noise.
        _notice_keyword_only_once()
        raise EmbeddingUnavailable(
            backend="local", model=model, text_len=len(text), cause=str(e)
        ) from e


def _embed_many_local(texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` through the local provider's ordered batch boundary."""
    if not texts:
        return []

    _run_preflight_once()
    model = config.embeddings.primary.model
    text_len = sum(len(text) for text in texts)
    try:
        return get_ollama_client().embed_many(texts)
    except EmbeddingContextError:
        raise
    except EmbeddingInputError:
        raise
    except OllamaError as e:
        logger.warning(
            "batch embed failed; raising EmbeddingUnavailable "
            "op=embed_many model=%s inputs=%d text_len=%d outcome=error error=%r",
            model, len(texts), text_len, str(e),
        )
        _notice_keyword_only_once()
        raise EmbeddingUnavailable(
            backend="local", model=model, text_len=text_len, cause=str(e)
        ) from e
