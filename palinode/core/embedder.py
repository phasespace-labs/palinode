"""
Palinode Embedder — dual backend (local BGE-M3 + Gemini cloud)

Default: BGE-M3 via Ollama (local, private, for core memory)
Ingestion: gemini-embedding-2-preview (cloud, multimodal, for research docs)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import httpx
from palinode.core.config import config

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Typed exceptions — #335
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Context-window preflight check — #335
# --------------------------------------------------------------------------

# Patterns in Ollama error responses that indicate context overflow.
# Ollama 0.3+ returns these in the JSON body with HTTP 200.
_CTX_OVERFLOW_PATTERNS = (
    "too long for max context",
    "prompt is too long",
    "context length exceeded",
    "exceeds context",
    "num_ctx",
)

# Minimum expected num_ctx for the embed model. bge-m3 supports 8192;
# the Ollama default is 4096 which silently truncates/errors on large chunks.
_MIN_EXPECTED_CTX = 8192

# Preflight guard — only check once per process.
_preflight_lock = threading.Lock()
_preflight_done = False


def _is_ctx_overflow_message(message: str) -> bool:
    """Return True if the Ollama error message indicates context overflow."""
    msg_lower = message.lower()
    return any(p in msg_lower for p in _CTX_OVERFLOW_PATTERNS)


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
        url: Ollama base URL (defaults to config).
        model: Model name (defaults to config).
        min_ctx: Minimum acceptable num_ctx value (default 8192 for bge-m3).
    """
    if url is None:
        url = config.embeddings.primary.url
    if model is None:
        model = config.embeddings.primary.model

    try:
        resp = httpx.post(
            f"{url}/api/show",
            json={"name": model},
            timeout=httpx.Timeout(5.0, connect=3.0),
        )
        resp.raise_for_status()
        data = resp.json()
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
    except (httpx.HTTPError, OSError, ValueError, KeyError) as e:
        # Preflight is best-effort; never block embed on it.
        logger.debug(
            "embed preflight: /api/show check skipped for model=%s: %s",
            model, e,
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
    # warning about misconfigured modelfiles (#335).
    _run_preflight_once()

    url = config.embeddings.primary.url
    model = config.embeddings.primary.model
    timeout = get_local_timeout()

    for attempt, (endpoint, payload_key) in enumerate(
        [("/api/embed", "input"), ("/api/embeddings", "prompt")]
    ):
        t0 = time.monotonic()
        try:
            response = httpx.post(
                f"{url}{endpoint}",
                json={"model": model, payload_key: text},
                timeout=timeout,
            )
            response.raise_for_status()
            elapsed_ms = (time.monotonic() - t0) * 1000

            data = response.json()
            if "embeddings" in data and len(data["embeddings"]) > 0:
                logger.debug(
                    "embed ok — model=%s endpoint=%s text_len=%d elapsed_ms=%.0f",
                    model, endpoint, len(text), elapsed_ms,
                )
                return data["embeddings"][0]
            elif "embedding" in data:
                logger.debug(
                    "embed ok — model=%s endpoint=%s text_len=%d elapsed_ms=%.0f",
                    model, endpoint, len(text), elapsed_ms,
                )
                return data["embedding"]

            # Response was 200 but contained neither expected key.
            # Detect Ollama's context-overflow pattern — it returns HTTP 200
            # with {"error": "prompt is too long for max context"} (#335).
            error_msg = data.get("error", "")
            if error_msg and _is_ctx_overflow_message(error_msg):
                raise EmbeddingContextError(
                    model=model,
                    text_len=len(text),
                    ollama_message=error_msg,
                )

            logger.warning(
                "embed: unexpected Ollama response shape — model=%s endpoint=%s "
                "text_len=%d response_keys=%r; trying next endpoint",
                model, endpoint, len(text), list(data.keys()),
            )

        except EmbeddingContextError:
            # Re-raise immediately — this is a typed signal, not a retry-able error.
            raise
        except httpx.TimeoutException as e:
            logger.warning(
                "embed: timeout on attempt %d — model=%s endpoint=%s "
                "text_len=%d timeout_s=%s: %s",
                attempt + 1, model, endpoint, len(text),
                config.embeddings.primary.timeout_seconds, e,
                exc_info=True,
            )
            continue
        except httpx.HTTPStatusError as e:
            logger.warning(
                "embed: HTTP %s on attempt %d — model=%s endpoint=%s "
                "text_len=%d: %s",
                e.response.status_code, attempt + 1, model, endpoint, len(text), e,
                exc_info=True,
            )
            continue
        except httpx.RequestError as e:
            # ConnectError, ReadError, etc — Ollama process may be down.
            logger.warning(
                "embed: connection error on attempt %d — model=%s endpoint=%s "
                "text_len=%d: %s",
                attempt + 1, model, endpoint, len(text), e,
                exc_info=True,
            )
            continue

    logger.warning(
        "embed: all endpoints exhausted — model=%s url=%s text_len=%d "
        "timeout_s=%s; returning empty vector",
        model, url, len(text), config.embeddings.primary.timeout_seconds,
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
