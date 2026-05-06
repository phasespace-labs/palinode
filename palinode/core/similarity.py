"""Cosine similarity helper for embedding vectors."""
from __future__ import annotations

import math

__all__ = ["cosine"]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    BGE-M3 outputs are L2-normalized so this reduces to a dot product, but we
    keep the explicit norm denominator for correctness against any embedder
    that doesn't normalize (e.g. Gemini at certain dimensions).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
