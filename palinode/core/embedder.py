"""
Palinode Embedder — dual backend (local BGE-M3 + Gemini cloud)

Default: BGE-M3 via Ollama (local, private, for core memory)
Ingestion: gemini-embedding-2-preview (cloud, multimodal, for research docs)
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol

import httpx
from palinode.core.config import config


# ---------------------------------------------------------------------------
# Protocol — the named seam for dependency injection / test fakes
# ---------------------------------------------------------------------------


class EmbedderProtocol(Protocol):
    """Structural typing contract for any embedder backend.

    Any object with ``embed(text) -> list[float]`` and
    ``embed_query(text) -> list[float]`` satisfies this protocol.
    """

    def embed(self, text: str) -> list[float]: ...

    def embed_query(self, text: str) -> list[float]: ...


# ---------------------------------------------------------------------------
# Class-based adapters
# ---------------------------------------------------------------------------


class OllamaEmbedder:
    """Wraps the local Ollama BGE-M3 backend as an EmbedderProtocol."""

    def embed(self, text: str) -> list[float]:
        return _embed_local(text)

    def embed_query(self, text: str) -> list[float]:
        return _embed_local(text)


class GeminiEmbedder:
    """Wraps the Gemini cloud backend as an EmbedderProtocol."""

    def __init__(self, dimension: int = 768) -> None:
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        return _embed_gemini(text, dimension=self.dimension, task_type="RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return _embed_gemini(text, dimension=self.dimension, task_type="RETRIEVAL_QUERY")


class FakeEmbedder:
    """Deterministic embedder for tests — no network, no Ollama required.

    Produces a stable vector from the MD5 hash of the input text,
    tiled to the requested dimensionality and L2-normalized.
    """

    def __init__(self, dim: int = 768, seed: int = 42) -> None:
        self.dim = dim
        self.seed = seed

    def embed(self, text: str) -> list[float]:
        # Seed is mixed into the hash so different FakeEmbedder instances
        # with different seeds produce different vector spaces.
        digest = hashlib.md5(
            f"{self.seed}:{text}".encode(), usedforsecurity=False
        ).digest()
        # 16 bytes from MD5 — tile to cover dim
        raw = [(b / 255.0 - 0.5) for b in digest]
        tiled = [raw[i % len(raw)] for i in range(self.dim)]
        # L2 normalize
        norm = math.sqrt(sum(v * v for v in tiled))
        if norm == 0.0:
            return tiled
        return [v / norm for v in tiled]

    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)


# ---------------------------------------------------------------------------
# Private backend implementations (unchanged)
# ---------------------------------------------------------------------------


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


def _embed_local(text: str) -> list[float]:
    """Embed via local provider specified in config (defaults to Ollama BGE-M3).

    Iterates over known inference API endpoints since Ollama versions
    have changed their primary embed endpoints.

    Args:
        text (str): The text to embed.

    Returns:
        list[float]: The normalized generated embedding.
    """
    url = config.embeddings.primary.url
    model = config.embeddings.primary.model
    timeout = get_local_timeout()

    for endpoint, payload_key in [
        ("/api/embed", "input"),
        ("/api/embeddings", "prompt"),
    ]:
        try:
            response = httpx.post(
                f"{url}{endpoint}",
                json={"model": model, payload_key: text},
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            if "embeddings" in data and len(data["embeddings"]) > 0:
                return data["embeddings"][0]
            elif "embedding" in data:
                return data["embedding"]
        except httpx.HTTPStatusError:
            continue
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


# ---------------------------------------------------------------------------
# Module-level functions — backward-compatible public API
# ---------------------------------------------------------------------------


def embed(text: str, backend: str = "local") -> list[float]:
    """Generate an embedding for the given text.

    Args:
        text (str): The text to embed.
        backend (str): The embedding backend to use - 'local' (Ollama) or 'gemini'.

    Returns:
        list[float]: A list of floats representing the embedding vector.
        An empty list is returned if the request fails or is misconfigured.
    """
    if backend == "gemini" and os.environ.get("GEMINI_API_KEY"):
        return _embed_gemini(text)
    return _embed_local(text)


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


__all__ = [
    "EmbedderProtocol",
    "OllamaEmbedder",
    "GeminiEmbedder",
    "FakeEmbedder",
    "embed",
    "embed_query",
]
