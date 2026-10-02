"""Bounded evidence resolution around search results — read-only.

A search hit is a starting point, not an answer. The record it came from may
have been replaced (``superseded_by``), may be in an open conflict
(``contradicts``), may rest on a source that was since retired
(``backed_by``) — or may have been corrected by a record that names it in
*its* frontmatter, or by one that names nothing at all. Search today returns
the hit's own link refs and leaves every caller to fetch the other side; a
correction ranked below the top-k, or phrased differently, stays unseen.

This module gathers that evidence for a set of seed results under explicit,
deterministic budgets and returns it with explicit coverage. Two things it
never does: it never mutates memory (no file write, no link write, no git
commit, no recall write-back), and it never decides truth — it reports what
the store holds around each seed and how completely it looked. Which side of
a conflict wins, and how a conflict renders, is the job of the resolution
layer above it.

Three kinds of evidence, three budgets
--------------------------------------

* **Link traversal** — ``contradicts`` and ``backed_by`` are followed forward
  (what the record names) and in reverse (which records name it) to
  ``max_depth`` hops, spending ``max_edges`` edges and ``max_files`` file
  reads across the whole request. Every edge is followed once; cycles
  terminate because a node is expanded once.
* **Replacement chains** — ``superseded_by`` is walked forward to the
  visible successor and in reverse to predecessors, under its own
  ``max_replacement_chain`` hop bound, so a long lineage cannot eat the edge
  budget and the edge budget cannot cut a lineage short.
* **Unlinked discovery** (``mode="full"`` only) — for a correction no link
  reaches: exact lookups first (the seed's ``entities`` against the entity
  index; ``sources`` / ``claims`` refs read directly), then bounded alternate
  retrieval (a keyword query built from the seed's title and entities; the
  seed's own stored vector as a neighbour query). Discovery has its own
  ``fallback_max_queries`` / ``fallback_max_reads`` budgets and a fixed
  stopping order: seeds in rank order, stages in the order above. It is
  discovery only — a discovered record is reported as ``discovered`` with the
  stage that found it, and no link is ever written.

On top of those three, every seed — and any replacement that could stand in
its place — gets a **bounded support check**
(:mod:`palinode.core.revalidation`): are the sources it declares still
standing, and are *their* sources, to ``max_support_hops`` hops, cycle-safe
and charged against the same file budget. It reports withdrawn support and a
disproven conclusion as different things and concludes nothing automatically
from a multi-source list unless the record declared a ``backing_policy``. The
findings ride into the resolution layer's ``qualifiers``; they are carried
in-process and add no field to the evidence payload.

Reverse edges come from the index (``chunks.metadata``, the frontmatter the
indexer stored), never from a directory scan: a frontmatter scan of a
4,000-file store measured ~280 ms, a ``json_each`` pass over the same store's
chunk metadata ~15 ms. The index is rebuildable and can lag the file, so a
reverse candidate is confirmed against the live file before it is reported,
and a stale index is named (``index_lag``) rather than trusted.

Coverage
--------

Every seed's evidence carries ``coverage`` — ``complete`` or ``partial`` with
the reasons, from a closed vocabulary (:data:`COVERAGE_REASONS`). A hidden
target contributes ``target_hidden`` and nothing else: not its title, not its
ref, not how many were hidden. A missing link or an exhausted budget is a
reason, never a claim that no counterevidence exists.

Visibility and paths
--------------------

Every expanded record passes the same checks the seed passed: the ref is
resolved through :func:`palinode.core.path_guard.resolve_memory_path` (no
traversal, no symlink escape) and gated by
:func:`palinode.core.visibility.is_visible` on the requester's scope chain,
reading live frontmatter. The exact-path read semantics of the read surfaces
are untouched; this module only ever reads files the guard admits.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

from palinode.core import parser as _parser
from palinode.core import revalidation as _reval
from palinode.core import store
from palinode.core.agent_directed import withhold_agent_directed
from palinode.core.config import config
from palinode.core.lifecycle import Eligibility, eligibility
from palinode.core.parity import RESOLVE_MODES
from palinode.core.path_guard import PathTraversalError, resolve_memory_path
from palinode.core.projection import project_current_text
from palinode.core.scope import ScopeChain, chain_allows
from palinode.core.typed_links import parse_link_refs
from palinode.core.visibility import is_visible, normalize_memory_path

logger = logging.getLogger("palinode.core.evidence")

# ── coverage vocabulary ──────────────────────────────────────────────────────

BUDGET_EDGES = "budget_exhausted:edges"
BUDGET_FILES = "budget_exhausted:files"
BUDGET_DEPTH = "budget_exhausted:depth"
BUDGET_CHAIN = "budget_exhausted:replacement_chain"
BUDGET_SUPPORT_HOPS = _reval.BUDGET_SUPPORT_HOPS
BUDGET_FALLBACK_QUERIES = "budget_exhausted:fallback_queries"
BUDGET_FALLBACK_READS = "budget_exhausted:fallback_reads"
TARGET_HIDDEN = "target_hidden"
TARGET_MISSING = "target_missing"
SCOPE_MISMATCH = "scope_mismatch"
INDEX_LAG = "index_lag"
FALLBACK_DISABLED = "fallback_disabled"

#: The closed set of partial-coverage reasons. Nothing outside it is ever
#: emitted, so a consumer can match on these and a hidden record can leak
#: nothing through a free-text reason.
COVERAGE_REASONS: frozenset[str] = frozenset({
    BUDGET_EDGES, BUDGET_FILES, BUDGET_DEPTH, BUDGET_CHAIN, BUDGET_SUPPORT_HOPS,
    BUDGET_FALLBACK_QUERIES, BUDGET_FALLBACK_READS,
    TARGET_HIDDEN, TARGET_MISSING, SCOPE_MISMATCH, INDEX_LAG, FALLBACK_DISABLED,
})

#: Typed-link fields the resolver follows. ``stale_backing`` is deliberately
#: not one of them: it is a flag the propagation pass already derived from a
#: retired ``backed_by`` source, and the seed carries it in its own metadata.
#: The read-time support check (:meth:`_Run.support`) is what covers the
#: source retired *since* — and the second hop propagation never walked.
LINK_FIELDS: tuple[str, ...] = ("superseded_by", "contradicts", "backed_by")

#: Characters of projected current text carried per record.
EXCERPT_CHARS = 300

#: Candidates any one discovery stage may return; the read budget is the
#: real bound, this only keeps a huge entity fan-out from being enumerated.
_FALLBACK_TOP_K = 8


# ── budgets ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceBudget:
    """Hard ceilings for one request. Defaults come from ``config.search.evidence``.

    ``max_files`` / ``max_edges`` and the two ``fallback_*`` budgets are spent
    across all seeds, in seed rank order, so exhaustion is deterministic for a
    given store state and seed list. ``max_depth`` and
    ``max_replacement_chain`` are per traversal from each seed.
    """

    max_files: int = 24
    max_edges: int = 48
    max_depth: int = 2
    max_replacement_chain: int = 8
    fallback_max_queries: int = 18
    fallback_max_reads: int = 12
    #: Hops of ``backed_by`` the support check walks from a record, kept apart
    #: from ``max_depth`` for the same reason the replacement chain is: the
    #: second hop is what the check exists for and a spent edge budget must
    #: not be able to silence it.
    max_support_hops: int = 2

    @classmethod
    def from_config(cls, cfg: Any | None = None) -> EvidenceBudget:
        src = cfg if cfg is not None else config.search.evidence
        return cls(
            max_files=int(src.max_files),
            max_edges=int(src.max_edges),
            max_depth=int(src.max_depth),
            max_replacement_chain=int(src.max_replacement_chain),
            fallback_max_queries=int(src.fallback_max_queries),
            fallback_max_reads=int(src.fallback_max_reads),
            max_support_hops=int(src.max_support_hops),
        )


# ── result types ─────────────────────────────────────────────────────────────


@dataclass
class EvidenceRecord:
    """One expanded record. Only ever built for a visible, readable file."""

    ref: str
    rel_path: str
    #: ``superseded_by`` | ``contradicts`` | ``backed_by`` for linked records;
    #: ``sources`` | ``entity`` | ``keyword`` | ``neighbor`` for discovered.
    relation: str
    #: ``forward`` (the origin names this record), ``reverse`` (this record
    #: names the origin) or ``discovered`` (no link; found by a fallback stage).
    direction: str
    #: Hops from the seed (1 for a direct link; 0 is never a record).
    depth: int
    #: The visible ref this record was reached from.
    via: str
    title: str | None
    currency: str
    currency_reason: str
    #: Index/source agreement for this record's file: ``valid`` | ``stale``
    #: (the index lags the file, see ``index_lag``) | ``unknown`` (unindexed).
    freshness: str
    effective_at: str | None
    epistemic: str | None
    #: Head of the projected current text — never a retired wording.
    excerpt: str
    #: SHA-256 of this record's file as it was read for this request — the
    #: exact revision the excerpt above came from, so a delivery receipt can
    #: name it instead of reporting ``unknown``. A **whole-file** hash
    #: (``file_sha256``), a different domain from the indexed per-section
    #: ``content_hash`` a search row carries; the two are never compared.
    content_hash: str | None = None
    #: The live frontmatter this record was built from, carried in-process for
    #: the resolution layer above (:mod:`palinode.core.resolution`), which is
    #: pure and cannot read the file itself. Deliberately NOT serialized: the
    #: evidence payload keeps the shape it shipped with, and raw frontmatter is
    #: not something a search result should spill.
    meta: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "rel_path": self.rel_path,
            "relation": self.relation,
            "direction": self.direction,
            "depth": self.depth,
            "via": self.via,
            "title": self.title,
            "currency": self.currency,
            "currency_reason": self.currency_reason,
            "freshness": self.freshness,
            "effective_at": self.effective_at,
            "epistemic": self.epistemic,
            "excerpt": self.excerpt,
            "content_hash": self.content_hash,
        }


@dataclass
class SeedEvidence:
    """Evidence gathered around one seed result."""

    seed_ref: str | None
    #: The seed's own live frontmatter, carried in-process for the resolution
    #: layer (same contract as :attr:`EvidenceRecord.meta`); ``None`` when the
    #: seed file could not be read. Not serialized.
    seed_meta: dict[str, Any] | None = None
    replacements: list[EvidenceRecord] = field(default_factory=list)
    conflicts: list[EvidenceRecord] = field(default_factory=list)
    support: list[EvidenceRecord] = field(default_factory=list)
    discovered: list[EvidenceRecord] = field(default_factory=list)
    reasons: set[str] = field(default_factory=set)
    #: Whether the seed's own indexed section still matches its file at
    #: expansion time — ``stale`` when the file changed underneath the search.
    seed_freshness: str = "unknown"
    #: The seed's own section as it reads *on disk right now*, projected to
    #: current text — the same live read every :class:`EvidenceRecord` excerpt
    #: comes from, carried in-process for the renderer above (same contract as
    #: :attr:`seed_meta`; deliberately not serialized). The index chose this
    #: seed; it does not get to supply its wording, because under
    #: :data:`INDEX_LAG` the two are different text and only one of them is
    #: what the file says. ``None`` when the seed file could not be read.
    seed_text: str | None = None
    #: SHA-256 of the seed file as read for this request — the revision
    #: :attr:`seed_text` came from, in the ``file_sha256`` domain
    #: (:attr:`EvidenceRecord.content_hash`), never comparable with the
    #: indexed per-section hash the seed row carries.
    seed_content_hash: str | None = None
    #: Read-time support checks, keyed by ref: the seed's own, and one for
    #: every replacement that could stand in its place
    #: (:mod:`palinode.core.revalidation`). Carried in-process for the
    #: resolution layer, exactly like :attr:`seed_meta`, and deliberately
    #: **not** serialized: the qualification reaches a reader through the
    #: resolution block's ``qualifiers``, not through a new evidence field.
    support_checks: dict[str, _reval.SupportCheck] = field(
        default_factory=dict, repr=False
    )

    def coverage(self) -> dict[str, Any]:
        return {
            "status": "partial" if self.reasons else "complete",
            "reasons": sorted(self.reasons),
        }

    def records(self) -> list[EvidenceRecord]:
        return [*self.replacements, *self.conflicts, *self.support, *self.discovered]

    def to_dict(self) -> dict[str, Any]:
        return {
            "replacements": [r.to_dict() for r in self.replacements],
            "conflicts": [r.to_dict() for r in self.conflicts],
            "support": [r.to_dict() for r in self.support],
            "discovered": [r.to_dict() for r in self.discovered],
            "seed_freshness": self.seed_freshness,
            "coverage": self.coverage(),
        }


@dataclass
class EvidenceResult:
    """What :func:`resolve_evidence` returns: one entry per seed, in seed order."""

    seeds: list[SeedEvidence]
    mode: str
    budget: EvidenceBudget
    #: Work actually done — for benchmarks and logs, not part of the payload.
    stats: dict[str, int]

    def coverage(self) -> dict[str, Any]:
        reasons: set[str] = set()
        for s in self.seeds:
            reasons |= s.reasons
        return {"status": "partial" if reasons else "complete", "reasons": sorted(reasons)}


def fold_coverage(blocks: Iterable[dict[str, Any] | None]) -> dict[str, Any]:
    """Request-level coverage from per-result ``evidence`` blocks (for renderers)."""
    reasons: set[str] = set()
    for b in blocks:
        cov = (b or {}).get("coverage") if isinstance(b, dict) else None
        if isinstance(cov, dict):
            reasons |= {r for r in cov.get("reasons", []) if isinstance(r, str)}
    return {"status": "partial" if reasons else "complete", "reasons": sorted(reasons)}


# ── per-request cache ────────────────────────────────────────────────────────


@dataclass
class _Loaded:
    rel: str
    ref: str
    abs_path: str
    meta: dict[str, Any]
    sections: list[dict[str, str]]
    projected_body: str
    #: SHA-256 of the file as read, computed once here because the bytes are
    #: already in hand. Same construction as ``store.check_freshness``'s
    #: comparand, over the whole file rather than one section — which is why
    #: it is reported under the ``file_sha256`` basis and never compared
    #: against an indexed per-section hash.
    content_hash: str = ""


class RequestCache:
    """File reads, index lookups and the reverse-edge relation for one request.

    Files are read once per request whatever route reaches them; the reverse
    relation is built once, lazily, on the first reverse lookup. Counters are
    what the benchmark reads.
    """

    def __init__(self) -> None:
        self.files: dict[str, _Loaded | None] = {}
        self.index_hashes: dict[str, dict[str, str] | None] = {}
        self.reverse: dict[str, list[tuple[str, str]]] | None = None
        self.files_read = 0
        self.reverse_lookups = 0
        #: Support checks for this request, reusable only under the
        #: delivery-receipt contract (see :class:`~palinode.core.revalidation.SupportCache`).
        self.support = _reval.SupportCache()


# ── helpers ──────────────────────────────────────────────────────────────────


def _ref_of(rel: str) -> str:
    rel = rel.replace(os.sep, "/")
    return rel[:-3] if rel.endswith(".md") else rel


def _rel_of_ref(ref: str) -> str:
    r = ref.strip().replace(os.sep, "/").lstrip("/")
    return r if r.endswith(".md") else f"{r}.md"


def _stored_path(rel: str) -> str:
    """The absolute form the indexer stores in ``chunks.file_path``."""
    return os.path.join(config.memory_dir, rel)


def _load(cache: RequestCache, rel: str) -> _Loaded | None:
    """Read and parse one memory file (once per request). ``None`` = unreadable."""
    if rel in cache.files:
        return cache.files[rel]
    loaded: _Loaded | None = None
    try:
        _base, abs_path = resolve_memory_path(rel)
    except PathTraversalError:
        cache.files[rel] = None
        return None
    try:
        with open(abs_path, encoding="utf-8") as fh:
            raw = fh.read()
        cache.files_read += 1
        meta, sections = _parser.parse_markdown(raw)
        projected = project_current_text(raw).text
        _head, body = _parser.split_frontmatter(projected)
        loaded = _Loaded(
            rel=rel,
            ref=_ref_of(rel),
            abs_path=abs_path,
            meta=meta if isinstance(meta, dict) else {},
            sections=sections,
            projected_body=body,
            # The bytes are already in hand; hashing them here is what lets a
            # receipt name this record's exact revision without a second read.
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )
    except (OSError, UnicodeDecodeError, ValueError):
        loaded = None
    cache.files[rel] = loaded
    return loaded


def _index_hashes(cache: RequestCache, db: Any, loaded: _Loaded) -> dict[str, str] | None:
    """``{section_id: content_hash}`` the index holds for this file, or ``None``."""
    if loaded.rel in cache.index_hashes:
        return cache.index_hashes[loaded.rel]
    rows = db.execute(
        "SELECT section_id, content_hash FROM chunks WHERE file_path IN (?, ?)",
        (_stored_path(loaded.rel), loaded.abs_path),
    ).fetchall()
    out: dict[str, str] | None = None
    if rows:
        out = {}
        for r in rows:
            if r["content_hash"]:
                out[r["section_id"] or "root"] = r["content_hash"]
    cache.index_hashes[loaded.rel] = out
    return out


def _freshness(cache: RequestCache, db: Any, loaded: _Loaded) -> str:
    """Raw-hash agreement between the live sections and the index (``check_freshness``'s comparand)."""
    stored = _index_hashes(cache, db, loaded)
    if not stored:
        return "unknown"
    live = {
        s["section_id"]: hashlib.sha256(s["content"].encode()).hexdigest()
        for s in loaded.sections
    }
    for section_id, stored_hash in stored.items():
        current = live.get(section_id)
        if current is None:
            return "stale"
        if (current if len(stored_hash) > 16 else current[:16]) != stored_hash:
            return "stale"
    return "valid"


def _visibility_reason(chain: ScopeChain | None, loaded: _Loaded) -> str | None:
    """``None`` when visible, else the coverage reason — never the record."""
    if is_visible(chain, loaded.abs_path, metadata=loaded.meta):
        return None
    # The gate above is the decision; this only labels why. An explicitly
    # scoped ``inherited`` memory off the chain is a scope mismatch; anything
    # ``private`` / ``restricted`` (or unevaluable) is hidden.
    if chain is not None and _parser.parse_scope(loaded.meta, file_path=loaded.rel)[
        "visibility"
    ] == "inherited" and not chain_allows(chain, loaded.meta):
        return SCOPE_MISMATCH
    return TARGET_HIDDEN


def _title(meta: dict[str, Any], body: str) -> str | None:
    for key in ("title", "name"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return withhold_agent_directed(val.strip())[0]
    m = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    return withhold_agent_directed(m.group(1).strip())[0] if m else None


_WS_RE = re.compile(r"\s+")


def _excerpt(body: str) -> str:
    # Withheld on the whole body, before it is squashed and cut: the sentence
    # structure the detector reads is gone afterwards, and a cut can split an
    # address from the directive it governs.
    body, _ = withhold_agent_directed(body)
    text = re.sub(r"^#{1,6}\s+.*$", "", body, flags=re.MULTILINE)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) <= EXCERPT_CHARS:
        return text
    return text[:EXCERPT_CHARS].rstrip() + "…"


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _record(
    cache: RequestCache,
    db: Any,
    loaded: _Loaded,
    *,
    relation: str,
    direction: str,
    depth: int,
    via: str,
    now: datetime | None,
) -> EvidenceRecord:
    elig: Eligibility = eligibility(loaded.meta, path=loaded.rel, now=now)
    currency, reason = store.currency_of(loaded.meta, loaded.rel, loaded.projected_body, now=now)
    return EvidenceRecord(
        ref=loaded.ref,
        rel_path=loaded.rel,
        relation=relation,
        direction=direction,
        depth=depth,
        via=via,
        title=_title(loaded.meta, loaded.projected_body),
        currency=currency,
        currency_reason=reason,
        freshness=_freshness(cache, db, loaded),
        effective_at=_iso(elig.effective_at),
        epistemic=elig.epistemic,
        excerpt=_excerpt(loaded.projected_body),
        content_hash=loaded.content_hash or None,
        meta=loaded.meta,
    )


def _link_refs(meta: dict[str, Any], field_name: str) -> list[str]:
    refs = parse_link_refs(meta, field_name)
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        ref = _ref_of(r)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _aliases_of(ref: str) -> set[str]:
    """Refs a record may be cited by: itself, and its base ref for a ``-status`` layer."""
    out = {ref}
    if ref.endswith("-status"):
        out.add(ref[: -len("-status")])
    return out


_REVERSE_SQL = """
SELECT DISTINCT c.file_path AS file_path, 'superseded_by' AS field, j.value AS value
  FROM chunks c, json_each(c.metadata, '$.superseded_by') j
 WHERE c.metadata LIKE '%"superseded_by"%' AND j.type = 'text'
UNION ALL
SELECT DISTINCT c.file_path, 'contradicts', j.value
  FROM chunks c, json_each(c.metadata, '$.contradicts') j
 WHERE c.metadata LIKE '%"contradicts"%' AND j.type = 'text'
UNION ALL
SELECT DISTINCT c.file_path, 'backed_by', j.value
  FROM chunks c, json_each(c.metadata, '$.backed_by') j
 WHERE c.metadata LIKE '%"backed_by"%' AND j.type = 'text'
"""


def _reverse_index(cache: RequestCache, db: Any) -> dict[str, list[tuple[str, str]]]:
    """``{target_ref: [(citing_rel, field), ...]}`` from the index, built once."""
    if cache.reverse is not None:
        return cache.reverse
    cache.reverse_lookups += 1
    index: dict[str, list[tuple[str, str]]] = {}
    try:
        rows = db.execute(_REVERSE_SQL).fetchall()
    except Exception as exc:  # noqa: BLE001 — a broken index is index lag, not a crash
        logger.warning("reverse-link scan failed; reverse edges unavailable: %s", exc)
        rows = []
    for row in rows:
        rel = normalize_memory_path(row["file_path"])
        value = row["value"]
        if not rel or not isinstance(value, str) or not value.strip():
            continue
        target = _ref_of(value.strip())
        index.setdefault(target, []).append((rel, row["field"]))
    for entries in index.values():
        entries.sort()
    cache.reverse = index
    return index


# ── the resolver ─────────────────────────────────────────────────────────────


class _Run:
    """State for one ``resolve_evidence`` call."""

    def __init__(
        self,
        *,
        mode: str,
        budget: EvidenceBudget,
        chain: ScopeChain | None,
        now: datetime | None,
        cache: RequestCache,
        db: Any,
        seed_refs: set[str],
    ) -> None:
        self.mode = mode
        #: Refs of every seed in the request: never reported as evidence of
        #: another seed — they are already in the result list.
        self.seed_refs = seed_refs
        self._recorded: set[tuple[str, str]] = set()
        self.budget = budget
        self.chain = chain
        self.now = now
        self.cache = cache
        self.db = db
        self.edges_used = 0
        self.files_used = 0
        self.fallback_queries = 0
        self.fallback_reads = 0
        #: rels loaded through link traversal — count against ``max_files``.
        self._charged: set[str] = set()

    # -- budgeted file access ------------------------------------------------

    def _open(self, ref: str, seed: SeedEvidence, *, fallback: bool) -> _Loaded | None:
        """Load a ref under the relevant file budget; ``None`` with the reason recorded."""
        rel = _rel_of_ref(ref)
        cached = rel in self.cache.files
        if not cached:
            if fallback:
                if self.fallback_reads >= self.budget.fallback_max_reads:
                    seed.reasons.add(BUDGET_FALLBACK_READS)
                    return None
                self.fallback_reads += 1
            else:
                if self.files_used >= self.budget.max_files:
                    seed.reasons.add(BUDGET_FILES)
                    return None
                self.files_used += 1
        loaded = _load(self.cache, rel)
        if loaded is None:
            seed.reasons.add(TARGET_MISSING)
            return None
        why = _visibility_reason(self.chain, loaded)
        if why is not None:
            seed.reasons.add(why)
            return None
        return loaded

    # -- read-time support check --------------------------------------------

    def _support_reader(self, seed: SeedEvidence) -> _reval.Reader:
        """A reader for the support walk: this request's cache, budget and gate.

        Reads are charged against ``max_files`` like every other expansion, so
        the check cannot spend more file reads than the request allowed, and a
        source the requester may not see is reported as hidden — never as a
        retirement, and never with anything of its own attached.
        """

        def _read(ref: str) -> _reval.SourceView | None:
            rel = _rel_of_ref(ref)
            if rel not in self.cache.files:
                if self.files_used >= self.budget.max_files:
                    seed.reasons.add(BUDGET_FILES)
                    return None
                self.files_used += 1
            loaded = _load(self.cache, rel)
            if loaded is None:
                return None  # missing: coverage, never a withdrawal
            why = _visibility_reason(self.chain, loaded)
            if why is not None:
                seed.reasons.add(why)
                return _reval.SourceView(ref=_ref_of(rel), hidden=True)
            return _reval.SourceView.of(
                loaded.ref, loaded.meta, revision=loaded.content_hash,
                path=loaded.rel, now=self.now,
            )

        return _read

    def support(self, seed: SeedEvidence, seed_loaded: _Loaded) -> None:
        """Check the seed's backing — and any replacement's — for this delivery.

        The seed is what the request returned; a replacement at the end of a
        ``superseded_by`` chain is what may stand in its place, so both are
        checked. Nothing else is: a conflicting or discovered record is
        context, not an answer, and checking every expanded record's backing
        would multiply the request's reads for evidence no outcome rests on.
        """
        read = self._support_reader(seed)
        clock = self.now or datetime.now(UTC)
        targets: list[tuple[str, dict[str, Any], str | None]] = [
            (seed_loaded.ref, seed_loaded.meta, seed_loaded.content_hash or None)
        ]
        for rec in seed.replacements:
            if rec.ref not in seed.support_checks and parse_link_refs(rec.meta, "backed_by"):
                targets.append((rec.ref, rec.meta, rec.content_hash))
        for ref, meta, revision in targets:
            check = _reval.resolve_support(
                ref, meta, read=read, now=clock, revision=revision,
                max_hops=self.budget.max_support_hops,
                cache=self.cache.support,
                scope=tuple(self.chain.as_list()) if self.chain is not None else (),
            )
            seed.support_checks[check.ref] = check
            # The hidden/scope reasons are already recorded, precisely, by the
            # reader; taking the check's generic one too would blunt them.
            seed.reasons |= set(check.reasons) - {TARGET_HIDDEN}

    def _edge(self, seed: SeedEvidence) -> bool:
        if self.edges_used >= self.budget.max_edges:
            seed.reasons.add(BUDGET_EDGES)
            return False
        self.edges_used += 1
        return True

    def _confirmed_reverse(
        self, node: _Loaded, field_name: str
    ) -> list[str]:
        """Refs whose *indexed* frontmatter names ``node`` under ``field_name``."""
        index = _reverse_index(self.cache, self.db)
        out: list[str] = []
        for alias in sorted(_aliases_of(node.ref)):
            for rel, fld in index.get(alias, []):
                if fld == field_name and _ref_of(rel) != node.ref and _ref_of(rel) not in out:
                    out.append(_ref_of(rel))
        return out

    def _live_names(self, citing: _Loaded, field_name: str, node: _Loaded) -> bool:
        """Does the live file still carry the edge the index reported?"""
        return bool(_aliases_of(node.ref) & set(_link_refs(citing.meta, field_name)))

    # -- link traversal ------------------------------------------------------

    def _skip(self, seed: SeedEvidence, ref: str, relation: str) -> bool:
        """Already reported for this seed under this relation, or a seed itself."""
        return ref == seed.seed_ref or (ref, relation) in self._recorded

    def _add(self, bucket: list[EvidenceRecord], rec: EvidenceRecord) -> None:
        self._recorded.add((rec.ref, rec.relation))
        bucket.append(rec)

    def traverse(self, seed: SeedEvidence, seed_loaded: _Loaded) -> None:
        seen_nodes: set[str] = {seed_loaded.ref}
        seen_edges: set[tuple[str, str, str]] = set()
        self._recorded = set()
        frontier: list[tuple[_Loaded, int]] = [(seed_loaded, 0)]

        while frontier:
            node, depth = frontier.pop(0)
            self._replacement_chain(seed, node, seen_edges)
            if depth >= self.budget.max_depth:
                # Partial only if this leaf has an edge to something not yet
                # reported — an edge back to the seed or to a record already
                # in the block is closed, not cut off.
                for f in ("contradicts", "backed_by"):
                    pending = [*_link_refs(node.meta, f), *self._confirmed_reverse(node, f)]
                    if any(not self._skip(seed, t, f) for t in pending):
                        seed.reasons.add(BUDGET_DEPTH)
                        break
                continue
            for field_name, bucket in (("contradicts", seed.conflicts), ("backed_by", seed.support)):
                # Forward: what this node names.
                for target in _link_refs(node.meta, field_name):
                    key = (node.ref, field_name, target)
                    if key in seen_edges or self._skip(seed, target, field_name):
                        continue
                    seen_edges.add(key)
                    if not self._edge(seed):
                        return
                    loaded = self._open(target, seed, fallback=False)
                    if loaded is None:
                        continue
                    self._add(bucket, _record(
                        self.cache, self.db, loaded, relation=field_name,
                        direction="forward", depth=depth + 1, via=node.ref, now=self.now,
                    ))
                    if loaded.ref not in seen_nodes:
                        seen_nodes.add(loaded.ref)
                        frontier.append((loaded, depth + 1))
                # Reverse: what names this node.
                for citing_ref in self._confirmed_reverse(node, field_name):
                    key = (citing_ref, field_name, node.ref)
                    if key in seen_edges or self._skip(seed, citing_ref, field_name):
                        continue
                    seen_edges.add(key)
                    if not self._edge(seed):
                        return
                    loaded = self._open(citing_ref, seed, fallback=False)
                    if loaded is None:
                        continue
                    if not self._live_names(loaded, field_name, node):
                        seed.reasons.add(INDEX_LAG)
                        continue
                    self._add(bucket, _record(
                        self.cache, self.db, loaded, relation=field_name,
                        direction="reverse", depth=depth + 1, via=node.ref, now=self.now,
                    ))
                    if loaded.ref not in seen_nodes:
                        seen_nodes.add(loaded.ref)
                        frontier.append((loaded, depth + 1))

    def _replacement_chain(
        self, seed: SeedEvidence, node: _Loaded, seen_edges: set[tuple[str, str, str]]
    ) -> None:
        """Walk ``superseded_by`` forward (successors) and reverse (predecessors)."""
        # Forward: node → its replacement → its replacement's replacement …
        cur = node
        walked: set[str] = {node.ref}
        for hop in range(1, self.budget.max_replacement_chain + 1):
            targets = _link_refs(cur.meta, "superseded_by")
            if not targets:
                break
            target = targets[0]
            key = (cur.ref, "superseded_by", target)
            if key in seen_edges or target in walked or self._skip(seed, target, "superseded_by"):
                break  # cycle, or already followed from another node
            seen_edges.add(key)
            if not self._edge(seed):
                return
            loaded = self._open(target, seed, fallback=False)
            if loaded is None:
                break
            walked.add(loaded.ref)
            self._add(seed.replacements, _record(
                self.cache, self.db, loaded, relation="superseded_by",
                direction="forward", depth=hop, via=cur.ref, now=self.now,
            ))
            cur = loaded
        else:
            if _link_refs(cur.meta, "superseded_by"):
                seed.reasons.add(BUDGET_CHAIN)
        # Reverse: what this node replaced, one predecessor level per hop.
        level: list[_Loaded] = [node]
        walked_back: set[str] = {node.ref}
        for hop in range(1, self.budget.max_replacement_chain + 1):
            next_level: list[_Loaded] = []
            for cur in level:
                for citing_ref in self._confirmed_reverse(cur, "superseded_by"):
                    key = (citing_ref, "superseded_by", cur.ref)
                    if (key in seen_edges or citing_ref in walked_back
                            or self._skip(seed, citing_ref, "superseded_by")):
                        continue
                    seen_edges.add(key)
                    if not self._edge(seed):
                        return
                    loaded = self._open(citing_ref, seed, fallback=False)
                    if loaded is None:
                        continue
                    if not self._live_names(loaded, "superseded_by", cur):
                        seed.reasons.add(INDEX_LAG)
                        continue
                    walked_back.add(loaded.ref)
                    self._add(seed.replacements, _record(
                        self.cache, self.db, loaded, relation="superseded_by",
                        direction="reverse", depth=hop, via=cur.ref, now=self.now,
                    ))
                    next_level.append(loaded)
            if not next_level:
                break
            level = next_level
        else:
            if any(self._confirmed_reverse(cur, "superseded_by") for cur in level):
                seed.reasons.add(BUDGET_CHAIN)

    # -- unlinked discovery --------------------------------------------------

    def _query(self, seed: SeedEvidence) -> bool:
        if self.fallback_queries >= self.budget.fallback_max_queries:
            seed.reasons.add(BUDGET_FALLBACK_QUERIES)
            return False
        self.fallback_queries += 1
        return True

    def discover(self, seed: SeedEvidence, seed_loaded: _Loaded, seed_row: dict[str, Any]) -> None:
        known = self.seed_refs | {seed_loaded.ref} | {r.ref for r in seed.records()}

        def _take(candidates: Iterable[str], relation: str) -> None:
            for ref in candidates:
                if ref in known:
                    continue
                known.add(ref)
                loaded = self._open(ref, seed, fallback=True)
                if loaded is None:
                    if BUDGET_FALLBACK_READS in seed.reasons:
                        return
                    continue
                seed.discovered.append(_record(
                    self.cache, self.db, loaded, relation=relation,
                    direction="discovered", depth=1, via=seed_loaded.ref, now=self.now,
                ))

        # Stage 1a — exact refs the seed itself cites (sources / claim anchors).
        cited: list[str] = []
        for src in _parser.parse_sources(seed_loaded.meta):
            ref = src.get("ref") if isinstance(src, dict) else None
            if isinstance(ref, str) and ref.strip():
                cited.append(_ref_of(ref.strip()))
        raw_claims = seed_loaded.meta.get("claims")
        for entry in raw_claims if isinstance(raw_claims, list) else []:
            sid = entry.get("source_id") if isinstance(entry, dict) else None
            if isinstance(sid, str) and sid.strip():
                cited.append(_ref_of(sid.strip()))
        if cited:
            _take(sorted(set(cited)), "sources")

        # Stage 1b — exact entity lookup against the entity index.
        raw_entities = seed_loaded.meta.get("entities")
        entities = sorted({
            str(e).strip() for e in (raw_entities if isinstance(raw_entities, list) else [])
            if str(e).strip()
        })
        if entities:
            if not self._query(seed):
                return
            found: list[str] = []
            for ent in entities[:_FALLBACK_TOP_K]:
                for row in store.get_entity_files(ent):
                    rel = normalize_memory_path(row["file_path"])
                    if rel:
                        found.append(_ref_of(rel))
            # ``get_entity_files`` orders by last_seen DESC — the most recently
            # indexed file sharing the subject is the likeliest correction.
            _take(dict.fromkeys(found), "entity")
            if BUDGET_FALLBACK_READS in seed.reasons:
                return

        # Stage 2 — keyword retrieval on the seed's identifiers.
        terms = [seed_loaded.ref.rsplit("/", 1)[-1].replace("-", " ")]
        title = _title(seed_loaded.meta, seed_loaded.projected_body)
        if title:
            terms.append(title)
        terms.extend(e.rsplit("/", 1)[-1].replace("-", " ") for e in entities[:_FALLBACK_TOP_K])
        query = " ".join(dict.fromkeys(t for t in terms if t)).strip()
        if query:
            if not self._query(seed):
                return
            try:
                hits = store.search_fts(query, top_k=_FALLBACK_TOP_K)
            except Exception as exc:  # noqa: BLE001 — a keyword-arm failure is not evidence
                logger.warning("evidence keyword stage failed: %s", exc)
                hits = []
            # The keyword arm's own relative floor (``search.fts_threshold``),
            # against the best match in this set — usually the seed itself —
            # so an OR-joined query cannot return every record sharing one word.
            top = max((float(h.get("score") or 0.0) for h in hits), default=0.0)
            floor = config.search.fts_threshold * top
            hits = [h for h in hits if float(h.get("score") or 0.0) >= floor]
            refs = [_ref_of(r) for r in (normalize_memory_path(h["file_path"]) for h in hits) if r]
            _take(dict.fromkeys(refs), "keyword")
            if BUDGET_FALLBACK_READS in seed.reasons:
                return

        # Stage 3 — the seed's own stored vector as a neighbour query, under
        # the same cosine floor the REST search applies to its vector arm.
        chunk_id = seed_row.get("id")
        if chunk_id:
            if not self._query(seed):
                return
            vec = _stored_vector(self.db, str(chunk_id))
            if vec:
                hits = store.search_internal(
                    vec, top_k=_FALLBACK_TOP_K, threshold=config.search.api_threshold
                )
                refs = [_ref_of(r) for r in (normalize_memory_path(h["file_path"]) for h in hits) if r]
                _take(dict.fromkeys(refs), "neighbor")


def _stored_vector(db: Any, chunk_id: str) -> list[float] | None:
    try:
        row = db.execute(
            "SELECT vec_to_json(embedding) AS v FROM chunks_vec WHERE id = ?", (chunk_id,)
        ).fetchone()
    except Exception:  # noqa: BLE001 — no vector, no neighbour stage
        return None
    if not row or not row["v"]:
        return None
    try:
        vec = json.loads(row["v"])
    except ValueError:
        return None
    return vec if isinstance(vec, list) and vec else None


def _seed_text(seed_row: dict[str, Any], loaded: _Loaded) -> str:
    """The seed's own section as the *file* has it, projected to current text.

    Section-scoped, because that is what the seed row is: a hit on one section
    of a file, and answering with the whole file would change what a
    multi-section record says for every request, lagging or not. A row whose
    section is gone from the live file (or that names none, the shape an exact
    ref read produces) falls back to the projected body.
    """
    section_id = seed_row.get("section_id")
    if section_id:
        match = next(
            (s for s in loaded.sections if s["section_id"] == section_id), None
        )
        if match is not None:
            return project_current_text(match["content"]).text
    return loaded.projected_body


def _seed_freshness(seed_row: dict[str, Any], loaded: _Loaded) -> str:
    """Does the seed's indexed section still match the file *now*?"""
    stored_hash = seed_row.get("content_hash")
    if not stored_hash:
        return "unknown"
    section_id = seed_row.get("section_id") or "root"
    matching = next((s for s in loaded.sections if s["section_id"] == section_id), None)
    if matching is None:
        return "stale"
    current = hashlib.sha256(matching["content"].encode()).hexdigest()
    return "valid" if (current if len(stored_hash) > 16 else current[:16]) == stored_hash else "stale"


def resolve_evidence(
    seeds: list[dict[str, Any]],
    *,
    mode: str = "linked",
    budget: EvidenceBudget | None = None,
    chain: ScopeChain | None = None,
    now: datetime | None = None,
    request_cache: RequestCache | None = None,
) -> EvidenceResult:
    """Gather bounded evidence around ``seeds`` (search result rows). Read-only.

    ``seeds`` are result dicts as ``/search`` produces them — ``file_path``
    (absolute), ``section_id``, ``content_hash`` and ``id`` are used; the
    seed's *live* frontmatter is re-read for its links, never the row's cached
    metadata. ``chain`` is the requester's scope chain exactly as the search
    resolved it (``None`` = access control only), applied to every expanded
    record. ``mode`` is one of :data:`palinode.core.parity.RESOLVE_MODES`;
    ``none`` returns empty evidence for each seed without touching disk.
    """
    if mode not in RESOLVE_MODES:
        raise ValueError(f"resolve mode must be one of {RESOLVE_MODES}, got {mode!r}")
    budget = budget or EvidenceBudget.from_config()
    cache = request_cache or RequestCache()
    out = [SeedEvidence(seed_ref=None) for _ in seeds]
    stats = {"files_read": 0, "edges_followed": 0, "fallback_queries": 0,
             "fallback_reads": 0, "reverse_lookups": 0}
    if mode == "none" or not seeds:
        return EvidenceResult(seeds=out, mode=mode, budget=budget, stats=stats)

    for row, seed in zip(seeds, out, strict=True):
        rel = normalize_memory_path(str(row.get("file_path") or ""))
        seed.seed_ref = _ref_of(rel) if rel else None
    seed_refs = {s.seed_ref for s in out if s.seed_ref}

    db = store.get_db()
    try:
        run = _Run(mode=mode, budget=budget, chain=chain, now=now, cache=cache, db=db,
                   seed_refs=seed_refs)
        loaded_seeds: list[_Loaded | None] = []
        for row, seed in zip(seeds, out, strict=True):
            rel = _rel_of_ref(seed.seed_ref) if seed.seed_ref else None
            if not rel:
                seed.reasons.add(TARGET_MISSING)
                loaded_seeds.append(None)
                continue
            # The seed already passed the search's own visibility gate; it is
            # loaded outside the file budget (it is the request, not evidence).
            loaded = _load(cache, rel)
            if loaded is None:
                seed.reasons.add(TARGET_MISSING)
                loaded_seeds.append(None)
                continue
            seed.seed_meta = loaded.meta
            seed.seed_text = _seed_text(row, loaded)
            seed.seed_content_hash = loaded.content_hash or None
            seed.seed_freshness = _seed_freshness(row, loaded)
            if seed.seed_freshness == "stale":
                seed.reasons.add(INDEX_LAG)
            loaded_seeds.append(loaded)

        # Closure over links for every seed first, then discovery in seed
        # order: a linked correction is never displaced by a discovered one.
        for seed, loaded in zip(out, loaded_seeds, strict=True):
            if loaded is not None:
                run.traverse(seed, loaded)
                # After the closure, so the replacements that could stand are
                # known and their frontmatter is already in the request cache.
                run.support(seed, loaded)
        for row, seed, loaded in zip(seeds, out, loaded_seeds, strict=True):
            if loaded is None:
                continue
            if mode == "full":
                run.discover(seed, loaded, row)
            else:
                seed.reasons.add(FALLBACK_DISABLED)
            for rec in seed.records():
                if rec.freshness == "stale":
                    seed.reasons.add(INDEX_LAG)
        stats.update(
            files_read=cache.files_read,
            edges_followed=run.edges_used,
            fallback_queries=run.fallback_queries,
            fallback_reads=run.fallback_reads,
            reverse_lookups=cache.reverse_lookups,
        )
    finally:
        db.close()
    return EvidenceResult(seeds=out, mode=mode, budget=budget, stats=stats)


def attach_evidence(
    results: list[dict[str, Any]],
    *,
    mode: str,
    chain: ScopeChain | None = None,
    budget: EvidenceBudget | None = None,
    now: datetime | None = None,
) -> EvidenceResult:
    """Run :func:`resolve_evidence` over ``results`` and set ``evidence`` on each row.

    Additive: no existing key is touched, and with ``mode="none"`` nothing is
    attached at all, so a request that did not ask stays byte-identical.
    """
    result = resolve_evidence(results, mode=mode, budget=budget, chain=chain, now=now)
    if mode != "none":
        for row, seed in zip(results, result.seeds, strict=True):
            row["evidence"] = seed.to_dict()
    return result


#: The row-level evidence buckets a record can reach a reader through.
_EVIDENCE_BUCKETS: tuple[str, ...] = ("replacements", "conflicts", "support", "discovered")


def withhold_retired(results: list[dict[str, Any]], evidence: EvidenceResult) -> int:
    """Drop retired records from each row's ``evidence`` and ``resolution`` blocks.

    Automatic and default reads follow search: a record the lifecycle
    classifier retired is not handed to a reader beside a hit — not as an
    unlinked discovery, a replaced predecessor, a conflict or a support
    excerpt, and not by ref on the resolution's sides or support groups (a
    ref is often a slug of the value it retired). The hit itself is never
    touched; it is what the caller searched for. Each evidence block gains
    ``history_withheld``, the number of records left out.

    Presentation only, and applied *after* :func:`attach_resolution` decided
    each outcome over the whole evidence, so no outcome changes. A caller
    that wants the history asks for it (``include_retired``) and this is not
    called. Returns the total withheld across all rows.
    """
    total = 0
    for row, seed in zip(results, evidence.seeds, strict=True):
        block = row.get("evidence")
        if not isinstance(block, dict):
            continue
        own = seed.seed_ref
        hidden: set[str] = set()
        for bucket in _EVIDENCE_BUCKETS:
            kept: list[Any] = []
            for rec in block.get(bucket) or []:
                ref = rec.get("ref") if isinstance(rec, dict) else None
                if ref and ref != own and rec.get("currency") == "retired":
                    hidden.add(ref)
                    continue
                kept.append(rec)
            block[bucket] = kept
        block["history_withheld"] = len(hidden)
        total += len(hidden)
        resolution = row.get("resolution")
        if hidden and isinstance(resolution, dict):
            resolution["sides"] = [
                s for s in resolution.get("sides") or []
                if not (isinstance(s, dict) and s.get("ref") in hidden)
            ]
            groups = []
            for group in resolution.get("support") or []:
                if not isinstance(group, dict):
                    continue
                members = [
                    m for m in group.get("members") or []
                    if not (isinstance(m, dict) and m.get("ref") in hidden)
                ]
                if members:
                    groups.append({**group, "members": members})
            resolution["support"] = groups
    return total


__all__ = [
    "BUDGET_SUPPORT_HOPS",
    "COVERAGE_REASONS",
    "EvidenceBudget",
    "EvidenceRecord",
    "EvidenceResult",
    "LINK_FIELDS",
    "RequestCache",
    "SeedEvidence",
    "attach_evidence",
    "fold_coverage",
    "resolve_evidence",
    "withhold_retired",
]
