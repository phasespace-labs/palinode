"""Observable retrieval mode and derived-index readiness, without model probes."""
from __future__ import annotations

from typing import Any

from palinode.core import store
from palinode.core.config import config
from palinode.core.scope import ScopeChain


def diagnostics() -> dict[str, Any]:
    """Store-wide administrative /status diagnostics; never use in recall.

    These counts describe derived rows, not a filesystem freshness scan. Database
    errors propagate: an unavailable index must not look like an empty corpus.
    """
    with store.reader() as reader:
        chunks = reader.chunk_count()
        vectors = reader.vector_count()
    state = "not_indexed" if chunks == 0 else "ready"
    if chunks and config.search.retrieval_mode != "lexical" and vectors < chunks:
        state = "embeddings_pending"
    return {
        "configured_mode": config.search.retrieval_mode,
        "active_mode": config.search.retrieval_mode,
        "index_state": state,
        "indexed_chunks": chunks,
        "vector_chunks": vectors,
        "coverage": "indexed_corpus_only",
    }


def search_diagnostics(
    active_mode: str, *, matched: bool, chain: ScopeChain | None,
    confidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Readiness of the caller-visible indexed corpus, with no global counts.

    Use live visibility, just like recall itself. In particular, a store holding
    only hidden memories must produce the same diagnostic as an empty store.
    Readiness stops at the first visible file (a delivered hit already proves
    it). Only files with vectorless chunks need further visibility reads.

    ``confidence`` is this delivery's verdict block from
    :func:`palinode.core.confidence.assess` (``confidence`` +  ``arms``),
    merged in when the mode ranked something against a query. It answers a
    different question from ``outcome``: ``matched`` says rows came back,
    the verdict says whether any of them is worth treating as the answer.
    """
    from palinode.core.visibility import is_visible

    ready = matched
    pending = False
    cache: dict[str, dict[str, Any]] = {}

    def visible(file: tuple[str, dict[str, Any]]) -> bool:
        file_path, meta = file
        return is_visible(chain, file_path, fallback_metadata=meta, cache=cache)

    with store.reader() as reader:
        if not ready:
            ready = any(visible(file) for file in reader.indexed_files())
        if ready and config.search.retrieval_mode != "lexical":
            pending = any(visible(file) for file in reader.indexed_files(vectorless=True))
    value = {
        "configured_mode": config.search.retrieval_mode,
        "active_mode": active_mode,
        "index_state": "embeddings_pending" if pending else ("ready" if ready else "not_indexed"),
        "coverage": "visible_indexed_corpus_only",
        "outcome": "matched" if matched else ("no_match" if ready else "not_indexed"),
    }
    if confidence:
        value.update(confidence)
    return value
