"""
Palinode Embedder — dual backend (local BGE-M3 + Gemini cloud)

Default: BGE-M3 via Ollama (local, private, for core memory)
Ingestion: gemini-embedding-2-preview (cloud, multimodal, for research docs)
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import httpx
from palinode.core.config import config
from palinode.core.ollama_client import (
    EmbeddingContextError,
    OllamaError,
    OllamaRole,
    _is_ctx_overflow_message,
    get_ollama_client,
)

logger = logging.getLogger(__name__)

# EmbeddingContextError and _is_ctx_overflow_message now live in
# palinode.core.ollama_client (so the client can raise the error from inside the
# embed path without a circular import) and are re-exported here for backward
# compatibility — existing `from palinode.core.embedder import
# EmbeddingContextError` imports keep working (Phase 3).
__all__ = ["EmbeddingContextError", "embed", "embed_query", "check_model_context"]

# --------------------------------------------------------------------------
# Context-window preflight check
# --------------------------------------------------------------------------

# Minimum expected num_ctx for the embed model. bge-m3 supports 8192;
# the Ollama default is 4096 which silently truncates/errors on large chunks.
_MIN_EXPECTED_CTX = 8192

# Preflight guard — only check once per process.
_preflight_lock = threading.Lock()
_preflight_done = False


def check_model_context(
    url: Optional[str] = None,
    model: Optional[str] = None,
    min_ctx: int = _MIN_EXPECTED_CTX,
) -> None:
    """Query Ollama /api/show and warn if num_ctx is below min_ctx.

    Called once at first embed (lazy preflight). Never raises — a failed
    preflight check is a warning, not a fatal error. The embed call proceeds
    regardless; this is purely diagnostic.

    Args:
        url: Deprecated/ignored — the centralized client resolves the EMBED URL
            from config (#338 Phase 3). Kept for signature compatibility.
        model: Model name (defaults to config).
        min_ctx: Minimum acceptable num_ctx value (default 8192 for bge-m3).
    """
    if model is None:
        model = config.embeddings.primary.model

    try:
        # Phase 3: route /api/show through the centralized client (EMBED
        # role). retries=0 — preflight is best-effort and must not amplify load.
        data = get_ollama_client().show(model, role=OllamaRole.EMBED, retries=0)
        # Ollama /api/show returns model_info with key "llama.context_length"
        # for GGUF models. For bge-m3 the key is typically under model_info.
        model_info = data.get("model_info", {})
        # Try the canonical key first, then the legacy parameters dict.
        ctx = model_info.get(
            "llama.context_length",
            data.get("parameters", {}).get("num_ctx", None)
        )
        if ctx is None:
            # Some Ollama versions embed num_ctx in the "details" block.
            # We can't guarantee a key across all versions — skip the check.
            logger.debug(
                "embed preflight: could not read num_ctx from /api/show for model=%s "
                "(key not present in response — skipping ctx check)",
                model,
            )
            return

        ctx_int = int(ctx)
        if ctx_int < min_ctx:
            logger.warning(
                "embed preflight: model=%s has num_ctx=%d which is below the "
                "recommended minimum of %d. Inputs longer than %d tokens will "
                "silently fail or be truncated. Fix: create a custom Ollama "
                "modelfile with 'PARAMETER num_ctx %d' and rebuild the model.",
                model, ctx_int, min_ctx, ctx_int, min_ctx,
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


def get_local_timeout() -> httpx.Timeout:
    """Gets the HTTPX timeout tuple for local embeddings from config."""
    return httpx.Timeout(
        config.embeddings.primary.timeout_seconds,
        connect=config.embeddings.primary.connect_timeout_seconds
    )


def get_gemini_timeout() -> httpx.Timeout:
    """Gets the HTTPX timeout tuple for Gemini embeddings from config."""
    return httpx.Timeout(
        config.embeddings.research.timeout_seconds,
        connect=10.0
    )


def embed(text: str, backend: str = "local") -> list[float]:
    """Generate an embedding for the given text.

    Args:
        text (str): The text to embed.
        backend (str): The embedding backend to use - 'local' (Ollama) or 'gemini'.

    Returns:
        list[float]: A list of floats representing the embedding vector.
        An empty list is returned if the request fails or is misconfigured.

    Raises:
        EmbeddingContextError: When Ollama explicitly rejects the input due to
            context-window overflow. Callers that want to handle truncation
            specially should catch this; callers that want graceful degradation
            can let it propagate to the top-level except and receive [] instead.
    """
    if backend == "gemini" and os.environ.get("GEMINI_API_KEY"):
        return _embed_gemini(text)
    return _embed_local(text)


def _run_preflight_once() -> None:
    """Run the context preflight check exactly once per process."""
    global _preflight_done
    with _preflight_lock:
        if not _preflight_done:
            _preflight_done = True
            check_model_context()


def _embed_local(text: str) -> list[float]:
    """Embed via local provider specified in config (defaults to Ollama BGE-M3).

    Iterates over known inference API endpoints since Ollama versions
    have changed their primary embed endpoints.

    Args:
        text (str): The text to embed.

    Returns:
        list[float]: The normalized generated embedding.

    Raises:
        EmbeddingContextError: When Ollama returns an explicit context-overflow
            error. See EmbeddingContextError for recovery guidance.
    """
    # Lazy preflight: check num_ctx once per process so operators get an early
    # warning about misconfigured modelfiles.
    _run_preflight_once()

    model = config.embeddings.primary.model

    # Phase 3: route through the centralized client. It owns the
    # /api/embed → /api/embeddings fallback, retry/backoff, circuit breaking,
    # and the structured per-call JSON logging (palinode.ollama.events). This
    # wrapper preserves the public contract: returns [] on connectivity/timeout
    # failure, re-raises EmbeddingContextError on a context-window overflow.
    try:
        return get_ollama_client().embed(text)
    except EmbeddingContextError:
        # Typed signal — propagate so callers can truncate / split.
        raise
    except OllamaError as e:
        # Connect/timeout/HTTP/circuit-open/unexpected-shape — degrade to an
        # empty vector (the contract the indexer relies on to skip + retry).
        # text_len, not raw text, so logs never carry user content.
        # Structured key=value per docs/logging.md — greppable on
        # op/outcome alongside the ollama_client per-call event line.
        logger.warning(
            "embed failed; returning empty vector "
            "op=embed model=%s text_len=%d outcome=error error=%r",
            model, len(text), str(e),
        )
        return []


def _embed_gemini(text: str, dimension: int = 768, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    """Embed via Gemini API (768/1536/3072d, Matryoshka).

    Args:
        text (str): The text content to embed.
        dimension (int): Requested dimensionality of the vector (defaults to 768).
        task_type (str): The context hint task type.

    Returns:
        list[float]: The generated embedding vector.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    model = config.embeddings.research.model
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"

    try:
        response = httpx.post(
            f"{gemini_url}?key={gemini_key}",
            json={
                "model": f"models/{model}",
                "content": {"parts": [{"text": text}]},
                "taskType": task_type,
                "outputDimensionality": dimension,
            },
            timeout=get_gemini_timeout(),
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Gemini rejected the request (auth, quota, bad model). Previously this
        # raised unlogged; surface a WARNING with the failing endpoint + status
        # so an operator can tell embed degradation from a model outage.
        # Endpoint is logged without the key query-string (no secret leak).
        logger.warning(
            "gemini embed failed op=embed model=%s endpoint=%s outcome=http_%d",
            model, gemini_url, e.response.status_code,
        )
        raise
    except httpx.HTTPError as e:
        # Connect/timeout/transport error reaching Gemini.
        logger.warning(
            "gemini embed failed op=embed model=%s endpoint=%s outcome=unreachable error=%r",
            model, gemini_url, str(e),
        )
        raise
    data = response.json()
    return data.get("embedding", {}).get("values", [])


def embed_query(text: str, backend: str = "local") -> list[float]:
    """Embed a search query.

    Delegates to RETRIEVAL_QUERY task type for the Gemini backend, enabling
    optimized short-query to long-document matching. Local backend remains standard.

    Args:
        text (str): The user search query.
        backend (str): Either 'local' or 'gemini'.

    Returns:
        list[float]: The query embedding vector.
    """
    if backend == "gemini" and os.environ.get("GEMINI_API_KEY"):
        return _embed_gemini(text, task_type="RETRIEVAL_QUERY")
    return _embed_local(text)
