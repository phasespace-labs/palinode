"""What a contentless prompt scores against this store, for the vector mark.

A large store of one project's notes hands *any* prompt a chunk at 0.60
cosine: on the dogfood store "continue" scores 0.601, on the 56-chunk
relevance fixture 0.471. The vector arm's confident mark (0.60, see
:mod:`palinode.core.confidence`) was calibrated on that fixture, so it sits
about 0.13 above the fixture's contentless baseline; carried to another store,
the mark keeps that height above the store's own baseline:
``max(0.60, null + VECTOR_NULL_MARGIN)``.

The baseline costs one embedding and one vector search (about 130 ms on a
14,000-chunk store). It is a property of the whole store and barely moves when
a note is saved, so it is measured once and reused until the store has grown
or shrunk by :data:`REFRESH_MIN_CHUNKS` chunks or :data:`REFRESH_FRACTION` of
its size, whichever is larger, or the measurement is :data:`MAX_AGE_S` old.
A different database or embedding model always measures afresh. Re-measuring
on every write would put that cost on the first prompt after each save, and a
store that is indexing does that all day. When it cannot be measured (no
embedding endpoint, an empty store) the mark is the plain 0.60.
"""
from __future__ import annotations

import logging
import threading
import time

from palinode.core.config import config

logger = logging.getLogger("palinode.vector_baseline")

#: The contentless prompt the baseline is measured with.
NULL_PROMPT = "continue"
#: The fixture-calibrated mark's height above that fixture's contentless
#: baseline: 0.60 - 0.471.
VECTOR_NULL_MARGIN = 0.129

#: Re-measure once the chunk count has moved by this many chunks...
REFRESH_MIN_CHUNKS = 200
#: ...or by this share of the store, whichever is larger.
REFRESH_FRACTION = 0.02
#: Re-measure at least this often, so in-place rewrites that keep the count
#: (a re-embed with a new chunking) are picked up within a day.
MAX_AGE_S = 24 * 3600.0

_lock = threading.Lock()
#: The last measurement: ``{"store": (db path, model), "count": chunk count
#: when measured, "at": monotonic time, "value": cosine or None}``.
_cache: dict[str, object] = {}


def _index_state() -> tuple[tuple[str, str], int]:
    from palinode.core import store

    db = store.get_db()
    try:
        (count,) = db.execute("SELECT count(*) FROM chunks").fetchone()
    finally:
        db.close()
    return (str(config.db_path), config.embeddings.primary.model), int(count)


def _still_current(identity: tuple[str, str], count: int, now: float) -> bool:
    if not _cache or _cache["store"] != identity:
        return False
    measured = int(_cache["count"])
    drift = abs(count - measured)
    if drift >= max(REFRESH_MIN_CHUNKS, REFRESH_FRACTION * measured):
        return False
    return now - float(_cache["at"]) < MAX_AGE_S


def null_cosine() -> float | None:
    """The best cosine :data:`NULL_PROMPT` reaches in this store, or ``None``."""
    from palinode.core import embedder, store

    try:
        identity, count = _index_state()
    except Exception:  # noqa: BLE001 - a missing store has no baseline
        return None
    with _lock:
        if _still_current(identity, count, time.monotonic()):
            return _cache["value"]  # type: ignore[return-value]
    value: float | None = None
    try:
        vector = embedder.embed(NULL_PROMPT)
        if vector:
            # top_k=10, not 1: the vector search filters after its KNN window.
            hits = store.search(vector, top_k=10, threshold=0.0, record_access=False)
            value = max((float(hit["score"]) for hit in hits), default=None)
    except Exception as exc:  # noqa: BLE001 - the plain mark is the fallback
        logger.info("vector baseline unavailable: %s", exc)
        return None
    with _lock:
        _cache.clear()
        _cache.update(store=identity, count=count, at=time.monotonic(), value=value)
    return value


def vector_confident_mark(margin: float = VECTOR_NULL_MARGIN) -> float | None:
    """``null + margin`` for this store, or ``None`` when no baseline exists."""
    value = null_cosine()
    return None if value is None else value + margin
