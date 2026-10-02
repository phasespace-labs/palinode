"""Bounded resolution — one operation that answers "what stands right now?".

Search returns hits and leaves the reader to decide. ``resolve`` composes the
three layers that already exist — seeds (:mod:`palinode.core.store`), bounded
evidence around each seed (:mod:`palinode.core.evidence`), and the resolution
policy over that evidence (:mod:`palinode.core.resolution`) — into one compact,
qualified bundle an agent can consume without a second round trip.

It decides nothing of its own. Every outcome in the bundle came from
:func:`palinode.core.resolution.resolve`; this module groups those outcomes,
budgets the result, and renders it. Read-only throughout: no file write, no
link write, no git commit, and no recall write-back — seeds come from the same
``store.search_hybrid`` ``/search`` uses, called with ``record_access=False``,
because an operation a per-turn hook runs on every prompt must not inflate
``recall_count`` or nudge ``importance``.

What the bundle holds
---------------------

``selected``
    The assertions that stand, each with its currency, index freshness, source
    revision (the raw ``content_hash``), qualifiers, and the refs a follow-up
    read would use — its linked backing on a ``support:`` line and, one line
    each, the unlinked records discovery found beside it. An unlinked
    correction is rendered where the assertion it bears on is, so the packer
    keeps the two together and the budget is charged for both.
``replaced``
    Records that were superseded or retired, each with its successor when one
    resolved — so a reader can see what moved, not just what is current.
``conflicts``
    Groups of sides that cannot both hold. **Always whole**: a group carries
    every visible side together with the reasons, or it is not carried at all.
``insufficient``
    Seeds the policy refused to answer for, with the closed reason that says
    why. Unknown is a result, never a fallback to the older value.
``history_withheld``
    How many retired records this bundle left out. Automatic delivery follows
    search: a record the lifecycle classifier retired (archived, deprecated,
    superseded, retracted, expired, under ``archive/``) is not delivered by
    default — not as a replacement, not as an unknown, not as an unlinked
    discovery, and not by ref under the record that replaced it, whose
    ``replaces:`` line then counts it instead of naming it. A label beside a
    retired value does not stop a reader answering with it. The request opts
    in with ``include_retired`` (history, labelled as such), and a record the
    caller named itself (``ref`` / ``context``) is always reported: telling a
    caller that what it holds was replaced is the point of naming it.
``coverage``
    Folded from the evidence layer's coverage plus this layer's own budget and
    cold-start reasons. ``partial`` always names its reasons.
``source_revisions``
    ``ref → raw content_hash`` for everything the bundle mentions, so a caller
    can tell whether the record it acted on has changed underneath it.
``other_projects_withheld``
    How many records tagged to another project this bundle left out. A request
    whose scope carries a project is isolated to it by the visibility choke
    point (:mod:`palinode.core.visibility`): a record naming one or more
    ``project/*`` entities, none of them the request's, is not a seed and is
    not reached by evidence expansion. A record naming no project is global
    and stays. ``include_other_projects`` opts in, and each such record then
    carries its own projects in ``other_project`` and in its stamp. A record
    the caller names itself (``ref`` / ``context``) is always reported.
    Presentation only: no resolution outcome is decided differently.
``receipt_ref`` / ``receipt``
    The per-delivery receipt (:mod:`palinode.core.receipt`) over the records
    this bundle actually delivered: each one's exact source revision and the
    hash domain that revision came from, how it was disposed, what lineage is
    known behind it, and under which policy, scope and clock. ``receipt_ref``
    is its ``bundle_id`` — the correlation key the retrieval log and
    ``palinode trace`` share. Building it writes nothing; the delivery surface
    persists receipt metadata after rendering, without creating recall events.

Conflict-preserving packing
---------------------------

The output budget drops **whole units** in priority order: selected first,
then conflict groups, then the explicit unknowns, then replacements. A
conflict group is one unit — its sides are never split and a kept item never
loses a qualifier. When a group cannot fit, the bundle says so out loud:
``coverage`` gains :data:`BUDGET_CONFLICTS`, ``omitted_conflicts`` counts the
dropped groups and ``omitted_conflict_refs`` lists their refs. Budget pressure
can make an answer smaller; it can never make a contested answer look settled.

``max_chars`` bounds the **rendered** bundle — frame and section labels
included — so a consumer with a hard injection budget can pass it through and
trust the result to fit, instead of slicing the text afterwards and cutting a
conflict in half. ``max_tokens`` bounds the same payload in estimated tokens
(``context.recall_max_tokens`` when the caller names no number). There is one
floor: a bundle never drops the notice naming an omitted conflict group, so a
budget smaller than frame-plus-notice (~300 characters) is overrun rather than
met. That is the deliberate direction to fail in — a bundle that fit by
dropping the notice would read as settled.

:func:`_apply_budget` is the one place the cap lives, and it holds no packing
rule of its own: it renders each outcome into a
:class:`palinode.core.packing.Unit` and hands the lot to
:func:`palinode.core.packing.pack` — the same function the session-start
digest packs through, so the two injection surfaces cannot disagree about what
"never split a unit" means. The item cap stays here, because the shared packer
bounds characters and tokens, not units.

Cold start
----------

No embedder (Ollama cold, unreachable, or a per-input rejection) degrades the
seed stage to BM25 keyword search and says so through the
:data:`DEGRADED_KEYWORD_ONLY` coverage reason. Nothing else in the pipeline
needs a model: evidence traversal and the resolution policy are deterministic,
so the operation still answers — it just names the narrower way it looked.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from palinode.core import store
from palinode.core.config import config
# The digest's own qualifier labelling, imported rather than re-written: a
# contested side and a digest row say ``⚠ contradicts:`` / ``⚠ stale backing:``
# in one vocabulary or an agent reads two. Private by name because nothing
# outside this pair should be labelling qualifiers at all;
# ``tests/test_resolve_bundle.py`` pins the two renderings together.
from palinode.core.context_prime import _row_qualifiers as _digest_labels
from palinode.core.evidence import (
    EXCERPT_CHARS,
    EvidenceBudget,
    EvidenceRecord,
    SeedEvidence,
    resolve_evidence,
)
from palinode.core.packing import (
    CHARS_PER_TOKEN,
    KIND_ASSERTION,
    KIND_CONFLICT,
    KIND_INSUFFICIENCY,
    PRIORITY_ASSERTION,
    PRIORITY_BACKGROUND,
    PRIORITY_CONFLICT,
    PRIORITY_INSUFFICIENCY,
    Budget,
    Unit,
    pack,
)
from palinode.core.parity import RESOLVE_INTENTS
from palinode.core.path_guard import PathTraversalError, resolve_memory_path
from palinode.core.projection import project_current_text
from palinode.core.receipt import (
    CONFLICT_SIDE,
    EVIDENCE_ONLY,
    INSUFFICIENT,
    REPLACED,
    REVISION_FILE,
    REVISION_INDEX_SECTION,
    REVISION_UNKNOWN,
    SELECTED,
    build_receipt,
)
from palinode.core.resolution import (
    EXPLICIT_REPLACEMENT,
    OUTCOME_CONFLICT,
    OUTCOME_INSUFFICIENT,
    OUTCOME_SUPPORTED,
    REPLACEMENT_SCHEDULED,
    SUPERSEDED_FROM,
    Resolution,
    Side,
    resolve,
)
from palinode.core.scoring import describe_other_projects_withheld
from palinode.core.scope import (
    ScopeChain,
    other_project,
    project_entities,
    with_other_projects,
)
from palinode.core.visibility import filter_visible, normalize_memory_path
from palinode.core.agent_directed import withhold_agent_directed
from palinode.core.framing import MEMORY_IS_DATA

logger = logging.getLogger("palinode.core.bundle")

# ── coverage vocabulary (this layer's own additions) ─────────────────────────

#: A conflict group did not fit the output budget and was dropped whole.
BUDGET_CONFLICTS = "budget_exhausted:conflicts"
#: Units were dropped because the item count ran out.
BUDGET_ITEMS = "budget_exhausted:items"
#: Units were dropped because the character budget ran out.
BUDGET_CHARS = "budget_exhausted:chars"
#: Units were dropped because the estimated-token budget ran out. A separate
#: reason from :data:`BUDGET_CHARS` because the two caps are separate: a
#: bundle can be well inside its character budget and over its token one.
BUDGET_TOKENS = "budget_exhausted:tokens"
#: No embedder was available, so seeds came from BM25 keyword search alone.
DEGRADED_KEYWORD_ONLY = "degraded:keyword_only"
#: The request named a ref that could not be read, or that the requester may
#: not see. Mirrors the evidence layer's reason of the same name.
TARGET_MISSING = "target_missing"

#: The closed set of coverage reasons this layer adds on top of
#: ``palinode.core.evidence.COVERAGE_REASONS``. A consumer matches on the
#: union; nothing outside it is ever emitted.
BUNDLE_COVERAGE_REASONS: frozenset[str] = frozenset({
    BUDGET_CONFLICTS, BUDGET_ITEMS, BUDGET_CHARS, BUDGET_TOKENS,
    DEGRADED_KEYWORD_ONLY,
})

#: Hard ceiling on seeds, whatever the caller's item budget says. Evidence
#: traversal is per-seed, so this is what keeps one request's file reads bounded.
MAX_SEEDS = 12

#: How much wider the seed query looks when the visibility gate hid seeds
#: from a full window. Only paid when something was actually hidden.
_SEED_OVERFETCH = 5

# ── request / budget ─────────────────────────────────────────────────────────

#: Defaults for the output budget. Small on purpose: the first consumer is a
#: per-turn recall hook, and injected bytes stay in the conversation.
DEFAULT_MAX_ITEMS = 8
DEFAULT_MAX_CHARS = 2000

#: Characters of a discovered record's statement carried on its one line.
#: Shorter than the evidence layer's own excerpt on purpose: this is a pointer
#: with enough text to recognise the claim, not the record.
DISCOVERY_EXCERPT_CHARS = 120


@dataclass(frozen=True)
class BundleBudget:
    """The output budget for one request: units, characters, estimated tokens.

    ``max_tokens`` is the shared packer's second cap. ``None`` — the default —
    means "the configured per-turn ceiling" (``context.recall_max_tokens``),
    read at pack time rather than frozen here: the bundle's first consumer is
    the per-turn recall block, and it should answer to the same configured
    number that surface does rather than to a second one that could disagree.
    ``0`` turns the token cap off and leaves ``max_chars`` the only size bound.
    """

    max_items: int = DEFAULT_MAX_ITEMS
    max_chars: int = DEFAULT_MAX_CHARS
    max_tokens: int | None = None


@dataclass(frozen=True)
class BundleRequest:
    """One resolve request.

    ``query`` (natural language) and ``ref`` (an exact memory ref / fact id)
    are the two ways in; at least one is required, and both may be given.
    ``context`` are refs the caller already holds — each is resolved as an
    additional seed so what the caller is carrying is checked too, rather than
    assumed current. ``intent`` is reserved: ``current_state`` is the only
    value, and as-of / known-at questions are deliberately not offered until
    the temporal-assertion work defines them.
    """

    query: str | None = None
    ref: str | None = None
    context: tuple[str, ...] = ()
    intent: str = "current_state"
    budget: BundleBudget = field(default_factory=BundleBudget)
    #: Deliver records tagged to a project other than the request's, labelled.
    #: Off by default so an automatic caller (the per-turn hook) scoped to a
    #: project is never handed another project's decision.
    include_other_projects: bool = False
    #: Deliver retired records too, labelled as history. Off by default so
    #: an automatic caller (the per-turn hook) gets what search would give it.
    include_retired: bool = False

    def __post_init__(self) -> None:
        if not (self.query and self.query.strip()) and not (self.ref and self.ref.strip()):
            raise ValueError("resolve requires a query or a ref")
        if self.intent not in RESOLVE_INTENTS:
            raise ValueError(
                f"intent must be one of {RESOLVE_INTENTS}, got {self.intent!r}"
            )


# ── result types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Assertion:
    """One record as the bundle presents it. Built from a resolution side."""

    ref: str | None
    rel_path: str | None
    title: str | None
    statement: str
    currency: str
    currency_reason: str
    freshness: str
    effective_at: str | None
    epistemic: str | None
    kind: str
    qualifiers: tuple[str, ...]
    revision: str | None
    #: The record's own projects when they are not the request's — set only on
    #: a record delivered across projects (opted in, or named by the caller).
    other_project: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = {
            "ref": self.ref,
            "rel_path": self.rel_path,
            "title": self.title,
            "statement": self.statement,
            "currency": self.currency,
            "currency_reason": self.currency_reason,
            "freshness": self.freshness,
            "effective_at": self.effective_at,
            "epistemic": self.epistemic,
            "kind": self.kind,
            "qualifiers": list(self.qualifiers),
            "revision": self.revision,
        }
        # Only on a cross-project record, so the default payload keeps its shape.
        if self.other_project:
            out["other_project"] = list(self.other_project)
        return out


@dataclass(frozen=True)
class Discovery:
    """An unlinked record the fallback discovery found beside a standing one.

    Ref, currency and a short excerpt — enough for a reader to see that a
    correction nobody linked exists and to go read it, and no more. The full
    record is one ``palinode_read`` away and the bundle is a per-turn payload.
    """

    ref: str
    currency: str
    statement: str
    other_project: tuple[str, ...] = ()


@dataclass(frozen=True)
class Selected:
    """An assertion that stands, with what it replaced and what it stood against."""

    assertion: Assertion
    reasons: tuple[str, ...]
    #: ``replaces`` / ``support`` / ``seeds`` — where a follow-up read goes.
    #: ``support`` carries the linked backing *and* the unlinked records
    #: discovery found, which is the shape this payload shipped with. A record
    #: standing until a dated replacement arrives also carries
    #: ``superseded_by``: the successor that takes over, present only for that
    #: case and pointing forward, never at history.
    refs: dict[str, list[str]]
    #: Sides considered that did not stand and were not retired — an unaccepted
    #: proposal beside the decision it did not displace. Refs and kind only.
    alternatives: tuple[dict[str, Any], ...] = ()
    #: The unlinked half of ``refs["support"]``, with each record's currency
    #: and a short excerpt so the renderer can say what was found rather than
    #: only that something was. Carried in-process and deliberately **not**
    #: serialized: every ref here is already in ``refs["support"]``, and the
    #: structured payload keeps the shape it shipped with.
    discovered: tuple[Discovery, ...] = field(default=(), repr=False)
    #: Retired records this one replaced that the request did not ask to see.
    #: Rendered as a count on the ``replaces:`` line and deliberately **not**
    #: serialized: a ref is often a slug of the very value it retired.
    withheld_replaces: tuple[str, ...] = field(default=(), repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.assertion.to_dict(),
            "reasons": list(self.reasons),
            "refs": {k: list(v) for k, v in self.refs.items()},
            "alternatives": [dict(a) for a in self.alternatives],
        }


@dataclass(frozen=True)
class Replaced:
    """A record that no longer stands, and the successor that replaced it."""

    assertion: Assertion
    successor: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.assertion.to_dict(),
            "successor": self.successor,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ConflictGroup:
    """Sides that cannot both hold. Carried whole or not at all."""

    sides: tuple[Assertion, ...]
    reasons: tuple[str, ...]
    seeds: tuple[str, ...]

    @property
    def refs(self) -> list[str]:
        return [s.ref for s in self.sides if s.ref]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sides": [s.to_dict() for s in self.sides],
            "reasons": list(self.reasons),
            "seeds": list(self.seeds),
        }


@dataclass(frozen=True)
class Insufficient:
    """A seed the policy would not answer for, and the reasons why."""

    ref: str | None
    rel_path: str | None
    title: str | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "rel_path": self.rel_path,
            "title": self.title,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Bundle:
    """What :func:`build_bundle` returns — the whole agent-facing contract."""

    intent: str
    query: str | None
    ref: str | None
    selected: tuple[Selected, ...]
    replaced: tuple[Replaced, ...]
    conflicts: tuple[ConflictGroup, ...]
    insufficient: tuple[Insufficient, ...]
    coverage: dict[str, Any]
    source_revisions: dict[str, str]
    #: The delivery receipt's identifier — the correlation key shared with the
    #: receipt below, the retrieval log and ``palinode trace``.
    receipt_ref: str | None
    #: The receipt's **public** view: every record this bundle delivered, at
    #: the exact revision it was delivered at, with its disposition, lineage,
    #: coverage, scope and evaluation clock. No memory text and no query —
    #: that is :meth:`palinode.core.receipt.Receipt.diagnostics`, which no
    #: surface returns.
    receipt: dict[str, Any] | None
    omitted_conflicts: int
    omitted_conflict_refs: tuple[tuple[str, ...], ...]
    budget: dict[str, int]
    #: Records tagged to another project left out because the request did not
    #: ask for them (``include_other_projects``).
    other_projects_withheld: int = 0
    #: The request's project, for the withheld line the text carries when
    #: isolation left the bundle (nearly) empty. In-process only.
    isolation_project: str | None = field(default=None, repr=False)
    #: Retired records left out because the request did not ask for history.
    history_withheld: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "query": self.query,
            "ref": self.ref,
            "selected": [s.to_dict() for s in self.selected],
            "replaced": [r.to_dict() for r in self.replaced],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "insufficient": [i.to_dict() for i in self.insufficient],
            "coverage": {
                "status": self.coverage.get("status", "complete"),
                "reasons": list(self.coverage.get("reasons", [])),
            },
            "source_revisions": dict(self.source_revisions),
            "receipt_ref": self.receipt_ref,
            "receipt": dict(self.receipt) if self.receipt is not None else None,
            "omitted_conflicts": self.omitted_conflicts,
            "omitted_conflict_refs": [list(g) for g in self.omitted_conflict_refs],
            "budget": dict(self.budget),
            "other_projects_withheld": self.other_projects_withheld,
            "history_withheld": self.history_withheld,
            "text": render_bundle(self),
        }


# ── helpers ──────────────────────────────────────────────────────────────────


def _ref_of(rel: str) -> str:
    rel = rel.replace(os.sep, "/")
    return rel[:-3] if rel.endswith(".md") else rel


def _rel_of_ref(ref: str) -> str:
    r = ref.strip().replace(os.sep, "/").lstrip("/")
    return r if r.endswith(".md") else f"{r}.md"


def _squash(text: str, limit: int = EXCERPT_CHARS) -> str:
    out = " ".join(text.split())
    return out[:limit]


def _statement(text: str, title: str | None) -> str:
    """The head of a record's text, minus the heading its title already says.

    Takes text in either shape the layers below produce — a raw body, or the
    evidence layer's already-squashed excerpt — because both arrive here and
    both otherwise render the title twice in one line.

    Text addressed to AI agents is withheld here, before squashing, so the
    statement every unit renders — and the packer measures — already carries
    the marker in its place.
    """
    out = _squash(withhold_agent_directed(text)[0])
    if title:
        for prefix in (f"# {title}", f"## {title}", f"### {title}", title):
            if out.startswith(prefix):
                return out[len(prefix):].strip()
    return out


def _title_of(meta: dict[str, Any], body: str) -> str | None:
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return withhold_agent_directed(title.strip())[0]
    for line in body.splitlines():
        if line.startswith("# "):
            return withhold_agent_directed(line[2:].strip())[0] or None
    return None


def _revisions_for(rels: Iterable[str]) -> dict[str, str]:
    """``rel_path → raw content_hash`` for the files this bundle mentions.

    The raw section hash is the revision comparand the index already keeps
    (``store.check_freshness``). A multi-section file is identified by its
    ``root`` section when it has one, else by its first section in id order,
    so one ref maps to exactly one revision deterministically. Bounded by the
    refs asked for — never a scan of the whole index.
    """
    wanted = [rel for rel in dict.fromkeys(rels) if rel]
    if not wanted:
        return {}
    paths: list[str] = []
    for rel in wanted:
        paths.append(rel)
        paths.append(os.path.join(config.memory_dir, rel))
    placeholders = ",".join("?" for _ in paths)
    best: dict[str, tuple[str, str]] = {}
    db = store.get_db()
    try:
        # B608: placeholders is "?,?,..." built from the count of paths; every
        # value is bound, nothing user-supplied is interpolated.
        rows = db.execute(
            "SELECT file_path, section_id, content_hash FROM chunks "  # nosec B608
            f"WHERE file_path IN ({placeholders}) AND content_hash IS NOT NULL",
            tuple(paths),
        ).fetchall()
    except Exception:  # pragma: no cover - a store without the column
        return {}
    finally:
        db.close()
    for row in rows:
        rel = normalize_memory_path(str(row["file_path"] or ""))
        if not rel:
            continue
        section = row["section_id"] or "root"
        prev = best.get(rel)
        if prev is None or (prev[0] != "root" and (section == "root" or section < prev[0])):
            best[rel] = (section, str(row["content_hash"]))
    return {rel: value[1] for rel, value in best.items()}


@dataclass
class _View:
    """Everything the renderer needs about one ref, gathered once."""

    ref: str | None
    rel_path: str | None
    title: str | None
    excerpt: str
    freshness: str
    other_project: tuple[str, ...] = ()


def _assertion(side: Side, views: dict[str, _View], revisions: dict[str, str]) -> Assertion:
    view = views.get(side.ref or "")
    rel = view.rel_path if view else (_rel_of_ref(side.ref) if side.ref else None)
    return Assertion(
        ref=side.ref,
        rel_path=rel,
        title=view.title if view else None,
        statement=view.excerpt if view else "",
        currency=side.currency,
        currency_reason=side.currency_reason,
        freshness=view.freshness if view else "unknown",
        effective_at=side.effective_at,
        epistemic=side.epistemic,
        kind=side.kind,
        qualifiers=side.qualifiers,
        revision=revisions.get(rel) if rel else None,
        other_project=view.other_project if view else (),
    )


# ── seeds ────────────────────────────────────────────────────────────────────


def _seed_from_ref(ref: str) -> dict[str, Any] | None:
    """A seed row for an exact ref, read from disk. ``None`` when unreadable."""
    rel = _rel_of_ref(ref)
    try:
        _base, abs_path = resolve_memory_path(rel)
    except PathTraversalError:
        return None
    if not os.path.isfile(abs_path):
        return None
    try:
        with open(abs_path, encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    # ``content_hash`` is deliberately absent: nothing was fetched from the
    # index, so there is no indexed revision to claim agreement with, and the
    # evidence layer reports this seed's freshness as ``unknown``.
    return {"id": None, "file_path": abs_path, "section_id": None,
            "content": raw, "metadata": {}, "content_hash": None}


def _query_seeds(query: str, limit: int) -> tuple[list[dict[str, Any]], set[str]]:
    """Seeds for a natural-language query, and any coverage reasons earned.

    The same retrieval ``/search`` performs — ``store.search_hybrid`` with the
    same floor (``config.search.api_threshold``), the same
    ``hybrid_enabled`` switch and the same telemetry exclusion — because the
    two arms fail on opposite query shapes: the vector arm blurs exact
    identifiers, and a per-turn resolve that misses the fact id the user just
    typed is the worst version of that. Vector-only seeds were the pre-v0.18
    recall bug re-introduced one layer up; ``store.search``'s own docstring
    says every ranked caller goes through ``search_hybrid``.

    ``record_access=False`` keeps the read-only property: the merged write is
    gated on that flag and the inner vector pass suppresses its own, so no
    ``recall_count`` moves. The retrieval log is written by the ``/search``
    router, not by the store, so calling the store directly writes no row
    either — this operation is not a retrieval event, it is an analysis of one.

    With no embedder, BM25 alone, which the caller reports as
    :data:`DEGRADED_KEYWORD_ONLY`.
    """
    from palinode.core import embedder

    reasons: set[str] = set()
    if config.search.retrieval_mode == "lexical":
        reasons.add(DEGRADED_KEYWORD_ONLY)
        return store.search_hybrid(query, None, top_k=limit, record_access=False), reasons
    try:
        embedding = embedder.embed(query)
    except (embedder.EmbeddingUnavailable, embedder.EmbeddingInputError) as exc:
        logger.info("resolve: embedder unavailable, keyword-only seeds (%r)", exc)
        embedding = None
    if not embedding:
        reasons.add(DEGRADED_KEYWORD_ONLY)
        return store.search_fts(query, top_k=limit), reasons
    return (
        store.search_hybrid(
            query_text=query,
            query_embedding=embedding,
            top_k=limit,
            threshold=config.search.api_threshold,
            hybrid_weight=config.search.hybrid_weight,
            use_fts=config.search.hybrid_enabled,
            record_access=False,
        ),
        reasons,
    )


def _collect_seeds(
    request: BundleRequest, chain: ScopeChain | None
) -> tuple[list[dict[str, Any]], set[str], int]:
    """Seed rows for this request, visibility-gated and de-duplicated by file.

    Also returns how many query seeds project isolation withheld. A ref the
    caller named is its own: it passes the gate with other projects let
    through (access control still applies), exactly as a named retired
    record is still reported.
    """
    reasons: set[str] = set()
    exact_rows: list[dict[str, Any]] = []

    exact = [r for r in (request.ref, *request.context) if r and r.strip()]
    for ref in exact:
        row = _seed_from_ref(ref)
        if row is None:
            reasons.add(TARGET_MISSING)
            continue
        exact_rows.append(row)

    # ADR-009 Layer 2: the same choke point search uses. An exact ref the
    # requester may not see is reported as a missing target, never named.
    visible = filter_visible(with_other_projects(chain), exact_rows)
    if len(visible) != len(exact_rows):
        reasons.add(TARGET_MISSING)

    other_projects: set[str] = set()
    seed_limit = max(1, min(request.budget.max_items, MAX_SEEDS))
    if request.query and request.query.strip():
        found, query_reasons = _query_seeds(request.query, seed_limit)
        reasons |= query_reasons
        kept = filter_visible(chain, found, other_projects=other_projects)
        if len(kept) != len(found) and len(found) >= seed_limit:
            # Hidden seeds consumed the window; look once more, wider, as
            # ``/search`` does, so isolation cannot starve the request of the
            # records it may see ranked just below another project's.
            found, _ = _query_seeds(request.query, seed_limit * _SEED_OVERFETCH)
            other_projects.clear()
            kept = filter_visible(chain, found, other_projects=other_projects)
        # Another project's record is withheld and counted, not a missing
        # target; anything else hidden is reported as before.
        if len(kept) + len(other_projects) != len(found):
            reasons.add(TARGET_MISSING)
        visible.extend(kept)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in visible:
        rel = normalize_memory_path(str(row.get("file_path") or ""))
        if not rel or rel in seen:
            continue
        seen.add(rel)
        deduped.append(row)
        if len(deduped) >= MAX_SEEDS:
            break
    return deduped, reasons, len(other_projects)


# ── assembly ─────────────────────────────────────────────────────────────────


def _views(
    rows: list[dict[str, Any]],
    seeds: list[SeedEvidence],
    chain: ScopeChain | None = None,
) -> dict[str, _View]:
    """One view per ref the bundle can mention: seeds first, then records.

    Every excerpt is the **file's** current text. The index chooses which
    records this bundle is about; it does not supply their wording, because
    when the index lags its source the two are different text and only one of
    them is what the record says. A stamp saying ``index stale`` beside the
    superseded wording is an honest label on the wrong answer, so
    the seed reads from the same live, projected load every other record's
    excerpt already came from. The row's own indexed text is the fallback for
    a seed whose file could not be read at all.

    A record tagged to a project other than the chain's carries those projects
    as its label; by default no such record reaches this far.
    """

    def label(meta: dict[str, Any] | None) -> tuple[str, ...]:
        if not meta or not other_project(chain, meta):
            return ()
        return tuple(project_entities(meta))

    out: dict[str, _View] = {}
    for row, seed in zip(rows, seeds, strict=True):
        if not seed.seed_ref:
            continue
        rel = _rel_of_ref(seed.seed_ref)
        body = (
            seed.seed_text
            if seed.seed_text is not None
            else project_current_text(str(row.get("content") or "")).text
        )
        title = _title_of(seed.seed_meta or {}, body)
        out[seed.seed_ref] = _View(
            ref=seed.seed_ref,
            rel_path=rel,
            title=title,
            excerpt=_statement(body, title),
            freshness=seed.seed_freshness,
            other_project=label(seed.seed_meta),
        )
    for seed in seeds:
        for rec in seed.records():
            if rec.ref in out:
                continue
            out[rec.ref] = _View(
                ref=rec.ref,
                rel_path=rec.rel_path,
                title=rec.title,
                excerpt=_statement(rec.excerpt, rec.title),
                freshness=rec.freshness,
                other_project=label(rec.meta),
            )
    return out


def _support_refs(seed: SeedEvidence, hidden: set[str]) -> list[str]:
    return [
        rec.ref for rec in (*seed.support, *seed.discovered)
        if rec.ref not in hidden
    ]


def _discoveries(
    seed: SeedEvidence,
    views: dict[str, _View],
    hide: Callable[[str | None, str], bool],
) -> tuple[Discovery, ...]:
    """The unlinked records found around one seed, in a renderable shape.

    ``direction="discovered"`` is the evidence layer's own word for "no link
    said this was related; a fallback stage found it anyway" — which is
    exactly the claim the rendered line makes, so nothing is re-derived here.
    """
    out: list[Discovery] = []
    for rec in seed.discovered:
        if hide(rec.ref, rec.currency):
            continue
        view = views.get(rec.ref)
        out.append(Discovery(
            ref=rec.ref,
            currency=rec.currency,
            statement=_squash(
                view.excerpt if view else _statement(rec.excerpt, rec.title),
                DISCOVERY_EXCERPT_CHARS,
            ),
            other_project=view.other_project if view else (),
        ))
    return tuple(out)


def _replacement_records(seed: SeedEvidence) -> dict[str, EvidenceRecord]:
    return {rec.ref: rec for rec in seed.replacements}


def _assemble(
    seeds: list[SeedEvidence],
    resolutions: list[Resolution],
    views: dict[str, _View],
    revisions: dict[str, str],
    *,
    shown_retired: frozenset[str] | None = None,
) -> tuple[
    list[Selected], list[Replaced], list[ConflictGroup], list[Insufficient], set[str]
]:
    """Group per-seed outcomes into the four buckets, de-duplicated by ref.

    Two seeds that resolve to the same standing record produce one ``selected``
    entry (their seed refs merge); two seeds on either side of one conflict
    produce one group. Nothing here re-decides an outcome.

    ``shown_retired`` is which retired records may be delivered: ``None`` for
    all of them (the request asked for history), otherwise only the refs the
    caller named. Every other retired record is left out of every bucket and
    returned in the withheld set; the record standing in its place is still
    delivered. What is withheld is presentation only — no outcome changes.
    """
    selected: dict[str, Selected] = {}
    replaced: dict[str, Replaced] = {}
    conflicts: dict[tuple[str, ...], ConflictGroup] = {}
    insufficient: dict[str, Insufficient] = {}
    withheld: set[str] = set()

    def hide(ref: str | None, currency: str) -> bool:
        if shown_retired is None or currency != "retired" or not ref:
            return False
        if ref in shown_retired:
            return False
        withheld.add(ref)
        return True

    for seed, resolution in zip(seeds, resolutions, strict=True):
        seed_ref = seed.seed_ref or ""
        chain_records = _replacement_records(seed)
        seed_side = next((s for s in resolution.sides if s.ref == seed_ref), None)

        if resolution.outcome == OUTCOME_SUPPORTED and resolution.current is not None:
            current = resolution.current
            key = current.ref or seed_ref
            # Sides other than the one standing: those on the replacement
            # chain are history (`replaces`), the rest stood against it.
            history = [s for s in resolution.sides if s.ref in chain_records or s.ref == seed_ref]
            others = [
                s for s in resolution.sides
                if s.ref != current.ref and s not in history
            ]
            # A scheduled replacement runs the other way: the record standing
            # is the *predecessor*, and the linked side is what will replace
            # it on the date in its stamp. Calling that "replaces" would read
            # backwards — the successor is a pointer forward, not history.
            scheduled_by: list[str] = []
            if REPLACEMENT_SCHEDULED in resolution.reasons:
                scheduled_by = [s.ref for s in history if s.ref and s.ref != current.ref]
                history = []
            # History the request did not ask for: counted, never named.
            withheld_here = tuple(
                s.ref for s in history
                if s.ref and s.ref != current.ref and hide(s.ref, s.currency)
            )
            history = [s for s in history if s.ref not in withheld_here]
            seed_hidden = seed_ref != current.ref and seed_ref in withheld
            if EXPLICIT_REPLACEMENT in resolution.reasons:
                for side in history:
                    if side.ref and side.ref not in replaced:
                        replaced[side.ref] = Replaced(
                            assertion=_assertion(side, views, revisions),
                            successor=current.ref,
                            reason=EXPLICIT_REPLACEMENT,
                        )
            existing = selected.get(key)
            found = _discoveries(seed, views, hide)
            refs = {
                "replaces": [s.ref for s in history if s.ref],
                "support": _support_refs(seed, withheld),
                "seeds": [seed_ref] if seed_ref and not seed_hidden else [],
            }
            if scheduled_by:
                refs["superseded_by"] = scheduled_by
            if existing is None:
                selected[key] = Selected(
                    assertion=_assertion(current, views, revisions),
                    reasons=resolution.reasons,
                    refs=refs,
                    alternatives=tuple(
                        {"ref": s.ref, "kind": s.kind, "currency": s.currency}
                        for s in others if s.ref and not hide(s.ref, s.currency)
                    ),
                    discovered=found,
                    withheld_replaces=withheld_here,
                )
            else:
                merged = {
                    k: list(dict.fromkeys(
                        [*existing.refs.get(k, []), *refs.get(k, [])]
                    ))
                    for k in dict.fromkeys([*existing.refs, *refs])
                }
                by_ref = {d.ref: d for d in (*existing.discovered, *found)}
                selected[key] = Selected(
                    assertion=existing.assertion,
                    reasons=tuple(dict.fromkeys([*existing.reasons, *resolution.reasons])),
                    refs=merged,
                    alternatives=existing.alternatives,
                    discovered=tuple(by_ref.values()),
                    withheld_replaces=tuple(dict.fromkeys(
                        [*existing.withheld_replaces, *withheld_here]
                    )),
                )
            continue

        if resolution.outcome == OUTCOME_CONFLICT:
            sides = tuple(
                _assertion(side, views, revisions) for side in resolution.sides
            )
            key = tuple(sorted(s.ref or "" for s in sides))
            existing_group = conflicts.get(key)
            if existing_group is None:
                conflicts[key] = ConflictGroup(
                    sides=sides,
                    reasons=resolution.reasons,
                    seeds=(seed_ref,) if seed_ref else (),
                )
            else:
                conflicts[key] = ConflictGroup(
                    sides=existing_group.sides,
                    reasons=tuple(
                        dict.fromkeys([*existing_group.reasons, *resolution.reasons])
                    ),
                    seeds=tuple(dict.fromkeys([*existing_group.seeds, seed_ref])),
                )
            continue

        # insufficient_evidence — the seed itself is what could not be
        # answered for. A seed retired in favour of a successor nobody can
        # see is reported as replaced *and* unknown: the record is out, and
        # what replaced it is not available to stand in its place. Both only
        # when history was asked for or the caller named the record.
        if resolution.outcome == OUTCOME_INSUFFICIENT:
            # A retired seed is the record itself, not a question about one:
            # withheld whole, exactly as search would not have returned it.
            if seed_side is not None and hide(seed_ref, seed_side.currency):
                continue
            view = views.get(seed_ref)
            if seed_ref and seed_ref not in insufficient:
                insufficient[seed_ref] = Insufficient(
                    ref=seed_ref,
                    rel_path=view.rel_path if view else None,
                    title=view.title if view else None,
                    reasons=resolution.reasons,
                )
            if seed_side is not None and seed_side.currency == "retired" and seed_ref not in replaced:
                successor = None
                reason = seed_side.currency_reason
                if reason.startswith("superseded_by: "):
                    successor = reason.split(": ", 1)[1] or None
                replaced[seed_ref] = Replaced(
                    assertion=_assertion(seed_side, views, revisions),
                    successor=successor,
                    reason=resolution.reasons[0] if resolution.reasons else "",
                )

    # A record that stands is never also listed as replaced (a later seed can
    # reach it as the middle of someone else's chain).
    for ref in list(replaced):
        if ref in selected:
            del replaced[ref]

    # A record withheld on one seed's account and standing on another's is
    # delivered: standing wins, as it does over ``replaced`` above.
    withheld -= set(selected)

    return (
        list(selected.values()),
        list(replaced.values()),
        list(conflicts.values()),
        list(insufficient.values()),
        withheld,
    )


# ── budget ───────────────────────────────────────────────────────────────────


#: Each section of the bundle as the shared packer sees it: the unit kind that
#: decides how it degrades, and the priority rung that decides who gets first
#: refusal on the remaining space. The order is the bundle's: what stands, then
#: what is contested, then the explicit unknowns, and history last.
#:
#: ``replaced`` packs at :data:`~palinode.core.packing.PRIORITY_BACKGROUND`
#: because the shared rungs have nothing between conflict and insufficiency,
#: and background is what a replacement *is* — a record that no longer stands
#: is the least load-bearing thing in the bundle, and an explicit "unknown"
#: outranks it. Rendering order is fixed by :func:`render_bundle` and does not
#: follow this; only what a scarce budget drops first does.
_SECTIONS: tuple[tuple[str, str, int], ...] = (
    ("selected", KIND_ASSERTION, PRIORITY_ASSERTION),
    ("conflict", KIND_CONFLICT, PRIORITY_CONFLICT),
    ("insufficient", KIND_INSUFFICIENCY, PRIORITY_INSUFFICIENCY),
    ("replaced", KIND_ASSERTION, PRIORITY_BACKGROUND),
)


@dataclass
class _Packed:
    selected: list[Selected]
    replaced: list[Replaced]
    conflicts: list[ConflictGroup]
    insufficient: list[Insufficient]
    omitted_conflicts: list[ConflictGroup]
    reasons: set[str]
    items: int
    chars: int
    tokens: int = 0


def _token_cap(budget: BundleBudget) -> int:
    """The estimated-token ceiling for one bundle. ``0`` = no token cap."""
    if budget.max_tokens is not None:
        return max(0, budget.max_tokens)
    return max(0, int(config.context.recall_max_tokens))


def _reserved_tokens(chars: int) -> int:
    """What a character reservation is worth in estimated tokens.

    The reservation is a byte count (:func:`_frame_chars` plus the section
    labels), not text, so it is converted with the packer's own ratio rather
    than measured a second way — one estimate, one place.
    """
    return -(-chars // CHARS_PER_TOKEN)


def _unit_refs(kind: str, unit: Any) -> tuple[str, ...]:
    """The source pointers a unit carries into the packer's contested stub."""
    if kind == "selected":
        return tuple(
            r for r in (
                unit.assertion.ref,
                *(unit.refs.get("replaces") or ()),
                *(unit.refs.get("superseded_by") or ()),
            ) if r
        )
    if kind == "conflict":
        return tuple(unit.refs)
    if kind == "replaced":
        return tuple(r for r in (unit.assertion.ref, unit.successor) if r)
    return tuple(r for r in (unit.ref,) if r)


def _unit_qualifiers(kind: str, unit: Any) -> tuple[str, ...]:
    """What qualifies a unit — the guard that keeps it from being shortened.

    Reported to the packer rather than merely rendered: a unit that carries
    qualifications may not be demoted to a gist, and the packer enforces that
    structurally (constructing one with a ``gist`` raises). The bundle offers
    no gist form at all, so nothing here is ever demoted; naming the
    qualifications keeps that true by construction rather than by omission.
    """
    if kind in ("selected", "replaced"):
        return unit.assertion.qualifiers
    if kind == "conflict":
        return tuple(dict.fromkeys(q for side in unit.sides for q in side.qualifiers))
    return unit.reasons


def _apply_budget(
    selected: list[Selected],
    replaced: list[Replaced],
    conflicts: list[ConflictGroup],
    insufficient: list[Insufficient],
    budget: BundleBudget,
    frame_chars: int = 0,
) -> _Packed:
    """Fit the bundle to ``budget`` through the shared packer.

    THE ONE PLACE THE CAP LIVES, and it no longer holds a packing rule of its
    own: every unit goes to :func:`palinode.core.packing.pack`, the same
    function the session-start digest packs through. A unit is a standing
    assertion, a conflict group (all its sides), a replacement, or one
    insufficiency — never a part of one, so nothing kept ever loses a
    qualifier and no conflict is ever reduced to one side. A dropped conflict
    group is reported rather than forgotten: the caller sees
    ``budget_exhausted:conflicts`` and the group's refs, which is the
    difference between "contested, and there was no room to show it" and
    "settled".

    The unit text is exactly what :func:`render_bundle` will emit for it
    (:func:`_render_lines` is the one function both go through), so the
    packer's character measure and the rendering cannot drift. ``max_chars``
    therefore bounds the **rendered bundle**, not just its units:
    ``frame_chars`` is the heading, the question line, the coverage line and
    the receipt line, and every section that has anything in it reserves its
    own label up front. A consumer with a hard byte budget can pass that
    budget straight through and trust the result to fit — which is what keeps
    its own final truncation from cutting a conflict in half after this
    function took care not to.

    The item cap stays here: the shared packer bounds characters and estimated
    tokens, not units. It is applied in packing order, so what a scarce item
    budget drops is what a scarce character budget would have dropped.
    """
    groups: dict[str, list[Any]] = {
        "selected": selected, "conflict": conflicts,
        "replaced": replaced, "insufficient": insufficient,
    }
    units = [
        Unit(
            kind=unit_kind,
            text="\n".join(_render_lines(name, item)),
            priority=priority,
            refs=_unit_refs(name, item),
            qualifiers=_unit_qualifiers(name, item),
            payload=(name, index),
        )
        for name, unit_kind, priority in _SECTIONS
        for index, item in enumerate(groups[name])
    ]
    reserved = frame_chars + sum(
        len(_section_label(name, len(groups[name]))) + 2
        for name, _kind, _priority in _SECTIONS if groups[name]
    )
    cap = max(0, budget.max_items)
    over_items = units[cap:]
    result = pack(units[:cap], Budget(
        # ``max_chars = 0`` means "nothing fits" here, not "no cap": a request
        # that asks for zero characters must not be answered with three
        # thousand. The packer's own convention (0 = off) is kept for the
        # token cap, where 0 is how the configuration disables it.
        max_chars=budget.max_chars if budget.max_chars > 0 else 1,
        max_tokens=_token_cap(budget),
        reserved_chars=reserved,
        reserved_tokens=_reserved_tokens(reserved),
    ))

    kept = {u.payload for u in result.units if u.payload is not None}
    dropped = {u.payload for u in (*over_items, *result.omitted) if u.payload is not None}
    packed = _Packed([], [], [], [], [], set(), len(kept), result.chars, result.tokens)
    for name, _kind, _priority in _SECTIONS:
        for index, item in enumerate(groups[name]):
            if (name, index) in kept:
                _keep(packed, name, item)
            elif (name, index) in dropped:
                _omit(packed, name, item)
    if over_items:
        packed.reasons.add(BUDGET_ITEMS)
    for limit in result.limits:
        packed.reasons.add(BUDGET_TOKENS if limit == "max_tokens" else BUDGET_CHARS)
    return packed


def _keep(packed: _Packed, kind: str, unit: Any) -> None:
    if kind == "selected":
        packed.selected.append(unit)
    elif kind == "conflict":
        packed.conflicts.append(unit)
    elif kind == "replaced":
        packed.replaced.append(unit)
    else:
        packed.insufficient.append(unit)


def _omit(packed: _Packed, kind: str, unit: Any) -> None:
    """Record a dropped unit. A conflict is the only kind that is reported."""
    if kind == "conflict":
        packed.omitted_conflicts.append(unit)
        packed.reasons.add(BUDGET_CONFLICTS)


def _render_lines(kind: str, unit: Any) -> list[str]:
    """One unit's rendering — the exact lines :func:`render_bundle` will emit.

    The packer measures a unit by its text, so this is the single function
    both the cost model and the renderer go through: a unit can never cost
    one thing and print another.
    """
    if kind == "selected":
        return _render_selected(unit)
    if kind == "conflict":
        return _render_conflict(unit)
    if kind == "replaced":
        return _render_replaced(unit)
    return _render_insufficient(unit)


# ── rendering (deterministic templates; no model involved) ───────────────────


#: The heading every rendering opens with.
_HEADING = "### Resolved from memory (current state)"

#: Bytes reserved for the coverage line and for the omitted-conflict notice.
#: Upper bounds, not measurements: both lines are written after packing, and
#: under-reserving is how a rendered bundle overruns the budget a consumer
#: sized its injection against.
_COVERAGE_RESERVE = 140
_OMISSION_RESERVE = 110
#: The other-projects withheld line: count, a project ref and the option name.
_WITHHELD_LINE_RESERVE = 160
#: Bytes reserved for the ``Receipt: <bundle_id>`` line. The identifier is a
#: fixed-width digest, but it is minted after packing (it covers the records
#: packing chose), so the line is reserved rather than measured.
_RECEIPT_RESERVE = 30


def _section_label(kind: str, count: int) -> str:
    """One section's label. Shared by the renderer and the packer's arithmetic,
    so the bytes charged for a section are the bytes it prints."""
    if kind == "selected":
        return f"Current ({count}):"
    if kind == "conflict":
        return f"Contested ({count}) — no winner; every visible side is shown:"
    if kind == "replaced":
        return f"Replaced ({count}):"
    return f"Unknown ({count}):"


def _frame_chars(
    query: str | None, ref: str | None, *, contested: bool, withheld_line: bool = False,
) -> int:
    """What a bundle costs before a single unit goes in it.

    Heading, the question (or record) line, the coverage line, the receipt
    line, and — when anything is contested — the line that names a group the
    budget drops. Charged up front so ``max_chars`` bounds the rendered text
    rather than just its contents.
    """
    total = len(_HEADING) + 1 + len(MEMORY_IS_DATA) + 1
    if query:
        total += len(f'Question: "{_squash(query, 200)}"') + 1
    elif ref:
        total += len(f"Record: {ref}") + 1
    # The coverage line grows with the reasons — and packing is what adds some
    # of them, so the count is not knowable here. An upper bound (the closed
    # vocabulary's longest plausible handful) is charged instead.
    total += _COVERAGE_RESERVE + _RECEIPT_RESERVE
    if contested:
        total += _OMISSION_RESERVE
    if withheld_line:
        # The other-projects line a thin scoped bundle may carry.
        total += _WITHHELD_LINE_RESERVE
    return total


def _stamp(a: Assertion) -> str:
    """``[currency · index stale · date]`` — what a reader sees before the text.

    A record whose supersession is dated forward carries the date in its
    currency instead of losing it: ``retired from 2027-01-01`` says the same
    word the frontmatter does *and* says that it is not true yet, which a bare
    ``retired`` under a ``Current:`` heading does not.
    """
    scheduled = next(
        (q.split(":", 1)[1] for q in a.qualifiers if q.startswith(f"{SUPERSEDED_FROM}:")),
        None,
    )
    bits = [f"{a.currency} from {scheduled}" if scheduled else a.currency]
    if a.freshness == "stale":
        bits.append("index stale")
    if a.effective_at:
        bits.append(a.effective_at[:10])
    if a.other_project:
        bits.append(f"other project: {', '.join(a.other_project)}")
    return " · ".join(bits)


def _qualifier_labels(qualifiers: Iterable[str]) -> str:
    """One side's qualifications as the digest's bracketed labels, or ``""``.

    The three families a reader must not lose — ``contradicts``,
    ``stale_backing`` (including the read-time two-hop support check's
    ``stale_backing:<ref>@<hop>:<reason>``, carried through whole because the
    hop and the reason are what say *which* backing went) and ``epistemic``
    — rendered by :func:`palinode.core.context_prime._row_qualifiers`, the
    session-start digest's own labeller. Anything else a side carries
    (``undated``, ``index_stale``, ``expires_at``) is already said elsewhere
    in the unit: the first two are promoted to the group's reasons and the
    third is in the stamp.
    """
    row: dict[str, Any] = {}
    for qualifier in qualifiers:
        key, _, value = qualifier.partition(":")
        if not value:
            continue
        if key in ("contradicts", "stale_backing"):
            row.setdefault(key, []).append(value)
        elif key == "epistemic":
            row["epistemic"] = value
    return _digest_labels(row)


def _line(a: Assertion, *, qualifiers: bool = False) -> str:
    """One assertion's row. ``qualifiers`` adds the digest's inline labels.

    Off for a standing assertion, which prints its qualifiers on its own
    indented ``qualifiers:`` line; on for a contested side, which has no such
    line and would otherwise read as an unqualified claim.
    """
    title = f" {a.title}" if a.title else ""
    statement = f" {a.statement}" if a.statement else ""
    labels = _qualifier_labels(a.qualifiers) if qualifiers else ""
    return f"- [{a.ref}]{title} [{_stamp(a)}]{statement}{labels}"


def _render_discovery(found: Discovery) -> str:
    """One unlinked record, under the assertion discovery found it beside.

    The ref is bracketed like every other citable record on this surface, so
    a reader — or the hook's consumer — can follow it; ``⚠`` and "(unlinked)"
    say what it is not: nothing in either record claims they are related.
    """
    statement = f" — {found.statement}" if found.statement else ""
    other = (
        f" · other project: {', '.join(found.other_project)}"
        if found.other_project else ""
    )
    return (
        f"    ⚠ also found (unlinked): [{found.ref}] [{found.currency}{other}]{statement}"
    )


def _render_selected(item: Selected) -> list[str]:
    lines = [_line(item.assertion)]
    replaces = list(item.refs.get("replaces") or [])
    if item.withheld_replaces:
        n = len(item.withheld_replaces)
        replaces.append(
            f"{n} earlier record{'' if n == 1 else 's'} (retired; withheld)"
        )
    if replaces:
        lines.append(f"    replaces: {', '.join(replaces)}")
    # The record that takes over on the date already in the stamp. Named so a
    # reader can go and see what is coming, not as history.
    scheduled_by = item.refs.get("superseded_by") or []
    if scheduled_by:
        lines.append(f"    superseded by: {', '.join(scheduled_by)}")
    for alt in item.alternatives:
        lines.append(f"    not accepted: {alt['ref']} ({alt['kind']})")
    # The linked half of ``refs["support"]`` by ref; the unlinked half gets a
    # line each below, because "something you never linked contradicts this"
    # is not something a bare ref conveys.
    unlinked = {found.ref for found in item.discovered}
    support = [r for r in (item.refs.get("support") or []) if r not in unlinked]
    if support:
        lines.append(f"    support: {', '.join(support)}")
    for found in item.discovered:
        lines.append(_render_discovery(found))
    if item.assertion.qualifiers:
        lines.append(f"    qualifiers: {', '.join(item.assertion.qualifiers)}")
    if item.reasons:
        lines.append(f"    why: {', '.join(item.reasons)}")
    if item.assertion.revision:
        lines.append(f"    revision: {item.assertion.revision[:12]}")
    return lines


def _render_replaced(item: Replaced) -> list[str]:
    successor = item.successor or "no visible successor"
    title = f" {item.assertion.title}" if item.assertion.title else ""
    return [
        f"- [{item.assertion.ref}]{title} → {successor} ({item.reason})",
    ]


def _render_conflict(group: ConflictGroup) -> list[str]:
    # Every side with its own qualifications: a contested pair where one side
    # is stale-backed is not two equally supported claims, and the group's
    # shared reasons cannot say which side carries what.
    lines = [_line(side, qualifiers=True) for side in group.sides]
    if group.reasons:
        lines.append(f"    reasons: {', '.join(group.reasons)}")
    return lines


def _render_insufficient(item: Insufficient) -> list[str]:
    title = f" {item.title}" if item.title else ""
    return [f"- [{item.ref}]{title} — {', '.join(item.reasons)}"]


def render_bundle(bundle: Bundle) -> str:
    """The bundle as text. One template, rendered identically on every surface.

    MCP, REST and CLI all emit this string, so the three readings of one
    request cannot disagree about what stands.
    """
    out: list[str] = [_HEADING, MEMORY_IS_DATA]
    if bundle.query:
        out.append(f'Question: "{_squash(bundle.query, 200)}"')
    elif bundle.ref:
        out.append(f"Record: {bundle.ref}")

    if bundle.selected:
        out.append("")
        out.append(_section_label("selected", len(bundle.selected)))
        for item in bundle.selected:
            out.extend(_render_selected(item))
    if bundle.conflicts:
        out.append("")
        out.append(_section_label("conflict", len(bundle.conflicts)))
        for group in bundle.conflicts:
            out.extend(_render_conflict(group))
    if bundle.replaced:
        out.append("")
        out.append(_section_label("replaced", len(bundle.replaced)))
        for item in bundle.replaced:
            out.extend(_render_replaced(item))
    if bundle.insufficient:
        out.append("")
        out.append(_section_label("insufficient", len(bundle.insufficient)))
        for item in bundle.insufficient:
            out.extend(_render_insufficient(item))
    if not (bundle.selected or bundle.conflicts or bundle.replaced or bundle.insufficient):
        out.append("")
        out.append("Nothing in memory answers this.")

    withheld_line = describe_other_projects_withheld(
        bundle.other_projects_withheld,
        delivered=len(bundle.selected) + len(bundle.conflicts)
        + len(bundle.replaced) + len(bundle.insufficient),
        project=bundle.isolation_project,
    )
    if withheld_line:
        out.append(withheld_line)

    if bundle.omitted_conflicts:
        out.append("")
        groups = "; ".join(
            " ↔ ".join(refs) for refs in bundle.omitted_conflict_refs
        )
        out.append(
            f"Still contested, omitted for budget ({bundle.omitted_conflicts}): {groups}"
        )

    out.append("")
    reasons = bundle.coverage.get("reasons") or []
    status = bundle.coverage.get("status", "complete")
    out.append(
        f"Coverage: {status}" + (f" ({', '.join(reasons)})" if reasons else "")
    )
    if bundle.receipt_ref:
        out.append(f"Receipt: {bundle.receipt_ref}")
    return "\n".join(out)


# ── the operation ────────────────────────────────────────────────────────────


def _coverage(reasons: Iterable[str]) -> dict[str, Any]:
    names = sorted(set(reasons))
    return {"status": "partial" if names else "complete", "reasons": names}


def _delivered(packed: _Packed) -> list[tuple[str, str, str]]:
    """``(ref, disposition, section)`` for every record this bundle delivers.

    Delivery order, first mention wins: a record that stands and is also
    somebody's support is ``selected``, not ``evidence_only``. The refs a kept
    unit names — what it replaced, what it rests on, the sides it stood
    against — are delivered too, as pointers rather than as prose, and a
    conflict group the budget dropped is delivered by ref in the omission
    notice. All of them are on the receipt, because "supplied" means supplied.
    """
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(ref: str | None, disposition: str, section: str) -> None:
        if ref and ref not in seen:
            seen.add(ref)
            out.append((ref, disposition, section))

    for item in packed.selected:
        add(item.assertion.ref, SELECTED, "selected")
    for group in packed.conflicts:
        for side in group.sides:
            add(side.ref, CONFLICT_SIDE, "conflict")
    for item in packed.replaced:
        add(item.assertion.ref, REPLACED, "replaced")
    for item in packed.insufficient:
        add(item.ref, INSUFFICIENT, "insufficient")
    for group in packed.omitted_conflicts:
        for ref in group.refs:
            add(ref, CONFLICT_SIDE, "omitted_conflict")
    for item in packed.selected:
        for ref in item.refs.get("replaces") or ():
            add(ref, REPLACED, "replaces")
        # A successor that has not taken effect yet was supplied as context,
        # not as a record that was replaced.
        for ref in item.refs.get("superseded_by") or ():
            add(ref, EVIDENCE_ONLY, "superseded_by")
        for alt in item.alternatives:
            add(alt.get("ref"), CONFLICT_SIDE, "alternative")
        for ref in item.refs.get("support") or ():
            add(ref, EVIDENCE_ONLY, "support")
    return out


def _receipt_rows(
    packed: _Packed,
    views: dict[str, _View],
    revisions: dict[str, str],
    metas: dict[str, dict[str, Any]],
    file_hashes: dict[str, str],
    currencies: dict[str, str],
) -> list[dict[str, Any]]:
    """The delivered records in the row shape :func:`build_receipt` reads.

    Nothing is looked up here: every value was computed by the layers this
    bundle already ran. The revision is the indexed per-section hash when the
    record is in the index (the comparand ``store.check_freshness`` uses) and
    otherwise the whole-file hash the evidence layer took when it read the
    file — two different domains, which is exactly why each row names the one
    its revision came from. A record in neither reports ``unknown``.

    One case inverts that order: **index lag**. A row stamped ``stale`` is one
    whose indexed hash no longer describes the file, and the text this bundle
    delivered came from the file — so the receipt names the file
    revision it was read at rather than an indexed one that describes nothing
    that was supplied. ``source_revisions`` stays in the index domain
    throughout: it is a change-detection token with no basis field to say
    which domain it is in, and a silently mixed one could not be compared at
    all.
    """
    rows: list[dict[str, Any]] = []
    for ref, disposition, section in _delivered(packed):
        rel = _rel_of_ref(ref)
        view = views.get(ref)
        revision = revisions.get(rel)
        basis = REVISION_INDEX_SECTION
        if not revision or (view is not None and view.freshness == "stale"):
            from_file = file_hashes.get(ref)
            if from_file:
                revision, basis = from_file, REVISION_FILE
            elif not revision:
                basis = REVISION_UNKNOWN
        rows.append({
            "rel_path": rel,
            "content_hash": revision,
            "revision_basis": basis,
            "freshness": view.freshness if view else None,
            "currency": currencies.get(ref),
            "metadata": metas.get(ref) or {},
            "disposition": disposition,
            "role": section,
        })
    return rows


def build_bundle(
    request: BundleRequest,
    *,
    chain: ScopeChain | None = None,
    now: datetime | None = None,
    evidence_budget: EvidenceBudget | None = None,
) -> Bundle:
    """Resolve ``request`` into a bounded, qualified bundle. Read-only.

    ``chain`` is the requester's scope chain exactly as the caller resolved it
    (``None`` = access control only); ``now`` is the single clock input, so the
    same store state and the same ``now`` produce the same bundle on every
    surface and every run.
    """
    reasons: set[str] = set()
    # Project isolation is the chain's to apply, at the visibility choke
    # point every seed and every evidence expansion already passes; the
    # request only says whether other projects' records are wanted.
    if request.include_other_projects:
        chain = with_other_projects(chain)
    rows, seed_reasons, other_withheld = _collect_seeds(request, chain)
    reasons |= seed_reasons

    evidence = resolve_evidence(
        rows, mode="full", budget=evidence_budget, chain=chain, now=now
    )
    resolutions = [
        resolve(seed.seed_meta, seed, now=now) for seed in evidence.seeds
    ]
    reasons |= {r for seed in evidence.seeds for r in seed.reasons}

    rels = [
        _rel_of_ref(seed.seed_ref) for seed in evidence.seeds if seed.seed_ref
    ] + [rec.rel_path for seed in evidence.seeds for rec in seed.records()]
    revisions = _revisions_for(rels)

    views = _views(rows, evidence.seeds, chain)
    # Retired records the caller named are its own; every other one is
    # history, delivered only when the request asks for it.
    shown_retired = None if request.include_retired else frozenset(
        r.strip().replace(os.sep, "/").removesuffix(".md")
        for r in (request.ref, *request.context) if r and r.strip()
    )
    selected, replaced, conflicts, insufficient, withheld = _assemble(
        evidence.seeds, resolutions, views, revisions, shown_retired=shown_retired,
    )

    # What the receipt needs about each record, taken from the layers that
    # already computed it: live frontmatter (for lineage), the whole-file hash
    # the evidence layer took while reading, and each record's currency.
    metas: dict[str, dict[str, Any]] = {}
    file_hashes: dict[str, str] = {}
    currencies: dict[str, str] = {}
    for seed in evidence.seeds:
        if seed.seed_ref and seed.seed_meta is not None:
            metas.setdefault(seed.seed_ref, seed.seed_meta)
        if seed.seed_ref and seed.seed_content_hash:
            file_hashes.setdefault(seed.seed_ref, seed.seed_content_hash)
        for rec in seed.records():
            metas.setdefault(rec.ref, rec.meta)
            currencies.setdefault(rec.ref, rec.currency)
            if rec.content_hash:
                file_hashes.setdefault(rec.ref, rec.content_hash)
    # Every side the policy classified, including a seed it refused to answer
    # for: an insufficiency is still a record whose currency was decided.
    for resolution in resolutions:
        for side in resolution.sides:
            if side.ref:
                currencies.setdefault(side.ref, side.currency)
    for item in selected:
        currencies[item.assertion.ref or ""] = item.assertion.currency
    for item in replaced:
        currencies[item.assertion.ref or ""] = item.assertion.currency
    for group in conflicts:
        for side in group.sides:
            currencies[side.ref or ""] = side.currency

    packed = _apply_budget(
        selected, replaced, conflicts, insufficient, request.budget,
        frame_chars=_frame_chars(
            request.query, request.ref, contested=bool(conflicts),
            withheld_line=other_withheld > 0,
        ),
    )
    reasons |= packed.reasons

    mentioned: list[str] = []
    for item in packed.selected:
        mentioned.append(item.assertion.ref or "")
        mentioned.extend(item.refs.get("replaces") or [])
        mentioned.extend(item.refs.get("superseded_by") or [])
    mentioned.extend(r.assertion.ref or "" for r in packed.replaced)
    mentioned.extend(s.ref or "" for g in packed.conflicts for s in g.sides)
    mentioned.extend(i.ref or "" for i in packed.insufficient)
    source_revisions = {
        ref: revisions[_rel_of_ref(ref)]
        for ref in dict.fromkeys(mentioned)
        if ref and _rel_of_ref(ref) in revisions
    }

    coverage = _coverage(reasons)
    # The receipt is built over what packing actually delivered, not over
    # everything considered. Building it is pure: no lookup or write. The
    # delivery surface persists the receipt separately from retrieval events;
    # this operation is an analysis of one.
    receipt = build_receipt(
        _receipt_rows(packed, views, revisions, metas, file_hashes, currencies),
        request={
            "query": request.query,
            "ref": request.ref,
            "context": list(request.context),
            "intent": request.intent,
            "max_items": request.budget.max_items,
            "max_chars": request.budget.max_chars,
            # Other projects change what is selected, so they are part of the
            # query scope; unset (not False) by default keeps the default's key.
            "include_other_projects": request.include_other_projects or None,
            # History changes what is selected, so it is part of the query
            # scope; unset (not False) by default keeps the default's key.
            "include_retired": request.include_retired or None,
        },
        scope=chain.as_list() if chain is not None else (),
        resolve_mode="full",
        surface="bundle",
        now=now,
        memory_dir=config.memory_dir,
        # This delivery's own coverage: the evidence layer's reasons plus the
        # ones packing earned. Folding the rows would miss both.
        coverage=coverage,
    )

    return Bundle(
        intent=request.intent,
        query=request.query,
        ref=request.ref,
        selected=tuple(packed.selected),
        replaced=tuple(packed.replaced),
        conflicts=tuple(packed.conflicts),
        insufficient=tuple(packed.insufficient),
        coverage=coverage,
        source_revisions=source_revisions,
        receipt_ref=receipt.bundle_id,
        receipt=receipt.public(),
        omitted_conflicts=len(packed.omitted_conflicts),
        omitted_conflict_refs=tuple(
            tuple(group.refs) for group in packed.omitted_conflicts
        ),
        budget={
            "max_items": request.budget.max_items,
            "max_chars": request.budget.max_chars,
            "max_tokens": _token_cap(request.budget),
            "items": packed.items,
            "chars": packed.chars,
            "tokens": packed.tokens,
        },
        other_projects_withheld=other_withheld,
        isolation_project=chain.project if chain is not None else None,
        history_withheld=len(withheld),
    )


__all__ = [
    "BUDGET_CHARS",
    "BUDGET_CONFLICTS",
    "BUDGET_ITEMS",
    "BUDGET_TOKENS",
    "BUNDLE_COVERAGE_REASONS",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_ITEMS",
    "DEGRADED_KEYWORD_ONLY",
    "DISCOVERY_EXCERPT_CHARS",
    "MAX_SEEDS",
    "Assertion",
    "Bundle",
    "BundleBudget",
    "BundleRequest",
    "ConflictGroup",
    "Discovery",
    "Insufficient",
    "Replaced",
    "Selected",
    "build_bundle",
    "render_bundle",
]
