"""Reconcile a memory file's derived state — the write path's single seam.

One markdown file on disk implies a set of derived artifacts: ``chunks`` rows,
their vectors, their FTS5 tokens, the ``entities`` table, and the cached
frontmatter in ``chunks.metadata``. Keeping those five in agreement with the
file used to be spread across the watcher, this module's predecessor, and a
handful of ``store`` methods — each of which added rows but only some of which
removed them, which is the shared root of the rename-orphans-entities
and frontmatter-edit-invisible defects.

This module concentrates that knowledge into three stages:

``derive(path, content) -> DerivedState``
    Pure. Parses the file and computes chunk ids, per-section body hashes, one
    per-file metadata hash, and the entity refs. No DB, no embedder, no clock —
    so a caller (or a test) can ask *what should be true* without a database.

``plan(state) -> Plan``
    Reads the DB once and decides, per section, what needs (re)indexing, what
    needs only a metadata refresh, and which stale chunk rows to prune — plus
    whether the entity rows changed. No writes.

``apply(plan, embedder) -> Diff``
    Embeds and writes in **one transaction**. Fail-closed: if any section that
    needs a vector cannot be embedded, the whole transaction is rolled back and
    nothing is written, so the index never reflects a half-applied edit. The
    deliberate cold-host degradation (all sections written FTS-only) is *not* a
    failure and still commits.

``reconcile(path, content) -> Diff`` runs all three; ``index_file`` wraps it to
preserve the legacy result shape for existing callers.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from palinode.core import embedder as _embedder
from palinode.core import parser, store
from palinode.core.embedder import EmbeddingInputError, EmbeddingUnavailable
from palinode.core.hashing import stable_md5_hexdigest
from palinode.core.ollama_client import get_ollama_client

logger = logging.getLogger("palinode.indexer")

# Cold-embed probe cache. Only negative verdicts need caching: a successful
# probe IS an embed, so it flips the client's ``has_embedded_ok`` and this
# cache is never consulted again. The TTL keeps a keyword-only install
# (embedder absent forever) at one bounded probe per window instead of one per
# indexed file when the watcher sweeps a batch.
_PROBE_TTL_S = 30.0
_probe_cache: dict[str, Any] = {"ts": 0.0, "ok": None}


def _embeds_deferred(client: Any) -> bool:
    """True when this pass should skip embeds (cold/absent embed path).

    Until an embed has succeeded in-process, one bounded ``probe_embed``
    (cached ``_PROBE_TTL_S`` seconds on failure) stands in for letting every
    section pay the full embed timeout.
    """
    if client.has_embedded_ok:
        return False
    now = time.monotonic()
    if _probe_cache["ok"] is not None and (now - _probe_cache["ts"]) < _PROBE_TTL_S:
        return not _probe_cache["ok"]
    ok = client.probe_embed()
    _probe_cache["ts"] = now
    _probe_cache["ok"] = ok
    return not ok


# ── stage 1: derive (pure) ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Section:
    """One derived chunk, before any DB contact."""
    chunk_id: str
    section_id: str
    content: str
    content_hash: str


@dataclass(frozen=True)
class DerivedState:
    """The complete derived state a file *should* have. Output of ``derive``.

    Pure function of ``(path, content)`` — no field is read from the DB, the
    embedder, or the clock, so two derivations of the same bytes are equal.
    """
    file_path: str
    category: str
    sections: tuple[Section, ...]
    metadata: dict[str, Any]
    meta_hash: str
    entities: tuple[str, ...]
    created_at: str
    last_updated: str


def derive(file_path: str, content: str) -> DerivedState:
    """Compute a file's intended derived state. Pure."""
    metadata, sections = parser.parse_markdown(content)
    category = metadata.get(
        "category", os.path.basename(os.path.dirname(file_path))
    )
    derived_sections = tuple(
        Section(
            chunk_id=stable_md5_hexdigest(f"{file_path}#{sec['section_id']}"),
            section_id=sec["section_id"],
            content=sec["content"],
            content_hash=hashlib.sha256(sec["content"].encode()).hexdigest(),
        )
        for sec in sections
    )
    # Entity input is metadata['entities'] verbatim. Body-wikilink ingestion
    # and canonicalization are deliberately out of scope for the write path.
    raw_entities = metadata.get("entities") or []
    entities = tuple(raw_entities) if isinstance(raw_entities, list) else ()
    return DerivedState(
        file_path=file_path,
        category=category,
        sections=derived_sections,
        metadata=metadata,
        meta_hash=store.meta_hash(metadata),
        entities=entities,
        created_at=metadata.get("created_at", ""),
        last_updated=metadata.get("last_updated", ""),
    )


# ── stage 2: plan (one read) ──────────────────────────────────────────────────

# Why a section is being written — preserved so the legacy result can keep
# distinguishing a fresh/changed write from a re-embed of an FTS-only row.
WRITE = "write"      # new file, or the body changed
REEMBED = "reembed"  # body unchanged but the vector was missing


@dataclass(frozen=True)
class PlannedWrite:
    section: Section
    reason: str  # WRITE | REEMBED


@dataclass
class Plan:
    """What ``apply`` must do to make the DB match a ``DerivedState``."""
    state: DerivedState
    to_index: list[PlannedWrite] = field(default_factory=list)
    meta_only: list[Section] = field(default_factory=list)
    delete_ids: list[str] = field(default_factory=list)
    entities_changed: bool = False
    unchanged: int = 0

    @property
    def is_noop(self) -> bool:
        return not (
            self.to_index or self.meta_only or self.delete_ids
            or self.entities_changed
        )


def plan(state: DerivedState) -> Plan:
    """Diff the derived state against the DB. Reads only — no writes.

    Testable with a real DB and no embedder: the interesting assertions
    ("a stale entity row is scheduled for removal", "a frontmatter-only edit
    is meta_only, not a re-embed") are answerable here.
    """
    p = Plan(state=state)
    derived_ids = {s.chunk_id for s in state.sections}

    db = store.get_db()
    try:
        for sec in state.sections:
            row = db.execute(
                "SELECT content_hash, meta_hash FROM chunks WHERE id = ?",
                (sec.chunk_id,),
            ).fetchone()
            if row is None:
                p.to_index.append(PlannedWrite(sec, WRITE))
                continue
            if row["content_hash"] != sec.content_hash:
                p.to_index.append(PlannedWrite(sec, WRITE))
                continue
            # Body unchanged. A missing vector means an FTS-only row that must
            # converge once the embedder is reachable — re-index it. Otherwise
            # only the frontmatter can be stale.
            if not _vec_present(db, sec.chunk_id):
                p.to_index.append(PlannedWrite(sec, REEMBED))
            elif row["meta_hash"] != state.meta_hash:
                p.meta_only.append(sec)
            else:
                p.unchanged += 1

        existing = db.execute(
            "SELECT id FROM chunks WHERE file_path = ?", (state.file_path,)
        ).fetchall()
        p.delete_ids = [r["id"] for r in existing if r["id"] not in derived_ids]

        current_entities = {
            r["entity_ref"] for r in db.execute(
                "SELECT entity_ref FROM entities WHERE file_path = ?",
                (state.file_path,),
            ).fetchall()
        }
        p.entities_changed = current_entities != set(state.entities)
    finally:
        db.close()
    return p


def _vec_present(db: Any, chunk_id: str) -> bool:
    """True iff the vec0 row exists — the observable symptom of the post /save returns 200
before content work.

    The FTS5 ``chunks_fts`` table is external-content, so a row there merely
    tracks the ``chunks`` row and is a useless presence check; ``chunks_vec``
    is real storage, so a missing id is the direct signal a row was written
    without its vector.
    """
    try:
        return db.execute(
            "SELECT 1 FROM chunks_vec WHERE id = ?", (chunk_id,)
        ).fetchone() is not None
    except Exception as e:
        logger.debug(
            "index presence check failed; treating as absent "
            "op=index chunk_id=%s error=%r", chunk_id, str(e),
        )
        return False


# ── stage 3: apply (one write transaction) ────────────────────────────────────


class _EmbedOutage(Exception):
    """Raised inside the apply transaction to trigger a fail-closed rollback."""

    def __init__(self, failed: int, section_id: str) -> None:
        super().__init__(f"embed failed for section {section_id!r}")
        self.failed = failed
        self.section_id = section_id


@dataclass
class Diff:
    """What ``apply`` did. ``committed`` is false on a fail-closed rollback."""
    committed: bool = False
    deferred: bool = False
    written: int = 0
    reembedded: int = 0
    unchanged: int = 0
    deleted: int = 0
    meta_updated: int = 0
    entities_replaced: bool = False
    embed_failures: int = 0
    vec_ok: bool = True
    fts_ok: bool = True
    error: str | None = None


def apply(p: Plan, embedder: Any = _embedder) -> Diff:
    """Embed and write a plan in one transaction. Fail-closed on embed outage.

    In embedding mode, every section in ``to_index`` must embed; the first
    *backend* failure (``EmbeddingUnavailable``) rolls the whole transaction
    back so the on-disk file is retried intact and the index is never left
    half-applied. A typed *per-input* rejection (``EmbeddingInputError``,
    e.g. a NaN vector for one pathological string) does not abort: that
    section alone is written FTS-only — the same keyword-searchable shape the
    deferred path writes — and the rest of the file indexes normally. The
    vector-less chunk is re-planned as REEMBED on later passes, so it heals
    itself if the model stops rejecting the input. In cold-defer mode no
    embed is attempted — all sections are written FTS-only and the pass
    commits, which is the designed keyword-searchable-now degradation, not a
    failure.
    """
    state = p.state
    diff = Diff(unchanged=p.unchanged)
    if p.is_noop:
        diff.committed = True
        return diff

    deferred = _embeds_deferred(get_ollama_client())
    diff.deferred = deferred
    metadata_json = json.dumps(state.metadata, default=str)
    now = store.utc_now_z()

    try:
        with store.transaction() as db:
            cur = db.cursor()

            # Embed first, fail-closed: nothing is written until every needed
            # vector is in hand (or we are deferring embeds entirely).
            embeddings: dict[str, list[float]] = {}
            if not deferred:
                use_scalar_fallback = True
                embed_many = getattr(embedder, "embed_many", None)
                if p.to_index and callable(embed_many):
                    try:
                        batch = embed_many([
                            pw.section.content for pw in p.to_index
                        ])
                    except EmbeddingInputError:
                        # A batch rejection proves at least one deterministic
                        # per-input failure but cannot identify which section.
                        # Retry individually so only the poisoned section loses
                        # its vector and healthy sections still index normally.
                        logger.info(
                            "batch embed rejected an input; retrying sections "
                            "individually op=index file_path=%s sections=%d",
                            state.file_path, len(p.to_index),
                        )
                    except EmbeddingUnavailable as e:
                        raise _EmbedOutage(
                            diff.embed_failures + 1,
                            p.to_index[0].section.section_id,
                        ) from e
                    else:
                        valid_batch = (
                            isinstance(batch, list)
                            and len(batch) == len(p.to_index)
                            and all(isinstance(vector, list) and vector for vector in batch)
                        )
                        if not valid_batch:
                            actual = len(batch) if isinstance(batch, list) else None
                            logger.warning(
                                "batch embed returned an invalid response "
                                "op=index file_path=%s expected=%d actual=%s",
                                state.file_path, len(p.to_index), actual,
                            )
                            raise _EmbedOutage(
                                diff.embed_failures + 1,
                                p.to_index[0].section.section_id,
                            )
                        embeddings.update({
                            pw.section.chunk_id: vector
                            for pw, vector in zip(p.to_index, batch, strict=True)
                        })
                        use_scalar_fallback = False

                if use_scalar_fallback:
                    for pw in p.to_index:
                        try:
                            emb = embedder.embed(pw.section.content)
                        except EmbeddingInputError as e:
                            # Per-input failure on a healthy backend (e.g. bge-m3
                            # NaN vector for this exact string). Aborting the
                            # whole file here made the note vanish from recall
                            # entirely — not even FTS. Degrade just this section
                            # to the FTS-only shape the deferred path already
                            # writes; the rest of the file indexes normally.
                            logger.warning(
                                "embed rejected this input; section written "
                                "FTS-only op=index file_path=%s section_id=%s "
                                "text_len=%d error=%r",
                                state.file_path, pw.section.section_id,
                                len(pw.section.content), e.ollama_message,
                            )
                            diff.embed_failures += 1
                            diff.vec_ok = False
                            continue
                        except EmbeddingUnavailable as e:
                            # Backend failure, typed at the embedder boundary. The
                            # watcher/indexer path wants retry-and-continue, not a
                            # crash: fold it into the same fail-closed abort a
                            # falsy `[]` used to trigger, so the file is retried
                            # intact on the next pass.
                            raise _EmbedOutage(
                                diff.embed_failures + 1, pw.section.section_id
                            ) from e
                        if not emb:
                            raise _EmbedOutage(
                                diff.embed_failures + 1, pw.section.section_id
                            )
                        embeddings[pw.section.chunk_id] = emb

            for pw in p.to_index:
                vec_ok, fts_ok = store.write_chunk_row(
                    cur,
                    chunk_id=pw.section.chunk_id,
                    file_path=state.file_path,
                    section_id=pw.section.section_id,
                    category=state.category,
                    content=pw.section.content,
                    metadata_json=metadata_json,
                    content_hash=pw.section.content_hash,
                    meta_hash=state.meta_hash,
                    created_at=state.created_at,
                    last_updated=state.last_updated,
                    embedding=embeddings.get(pw.section.chunk_id, []),
                )
                diff.vec_ok = diff.vec_ok and vec_ok
                diff.fts_ok = diff.fts_ok and fts_ok
                if pw.reason == REEMBED:
                    diff.reembedded += 1
                else:
                    diff.written += 1

            for sec in p.meta_only:
                store.write_chunk_meta(
                    cur, sec.chunk_id, metadata_json, state.meta_hash
                )
                diff.meta_updated += 1

            if p.delete_ids:
                store.prune_chunk_ids(cur, p.delete_ids)
                diff.deleted = len(p.delete_ids)

            if p.entities_changed:
                store.replace_entities(
                    cur, state.file_path, list(state.entities),
                    state.category, now,
                )
                diff.entities_replaced = True

        diff.committed = True
    except _EmbedOutage as e:
        # Transaction rolled back by the context manager — the file is
        # unchanged on disk and its prior index survives intact. One WARNING
        # names the file so a retry storm is traceable.
        diff.committed = False
        diff.embed_failures = e.failed
        diff.error = "embedder unreachable — reconcile aborted, will retry"
        logger.warning(
            "reconcile aborted: embed failed, no changes written "
            "op=index file_path=%s section_id=%s",
            state.file_path, e.section_id,
        )
        return diff

    if deferred and p.to_index:
        diff.vec_ok = False
        diff.error = (
            "embed deferred: probe failed (cold or absent embedder); "
            "rows are keyword-searchable now, re-embed follows"
        )
        logger.info(
            "embeds deferred; sections written FTS-only "
            "op=index file_path=%s sections_deferred=%d",
            state.file_path, len(p.to_index),
        )
    return diff


def reconcile(file_path: str, content: str) -> Diff:
    """derive → plan → apply. The whole write path for one file."""
    return apply(plan(derive(file_path, content)))
