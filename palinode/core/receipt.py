"""Context delivery receipts — what was supplied, at which revision, under which policy.

A delivery (a ``/search`` response, a ``/context/prime`` digest) hands an agent
a set of records. The receipt is the qualified record of *that* hand-off: which
refs were supplied, the exact source revision of each one, how each was
disposed (selected, replaced, a side of a conflict, insufficient, or carried
only as evidence), the known origin lineage behind them, the caller scope and
policy the server resolved, the clock it evaluated against, and the next known
temporal transition among the delivered records.

It records **supplied context, not causal influence**. Nothing here claims a
delivered record shaped what the agent did next; the terminal consuming-action
edge is a separate, unbuilt provenance gap (``G3`` in
:mod:`palinode.core.trace`).

Every field is derived from what the delivery already computed — the result
rows, their indexed metadata, and the ``evidence`` / ``resolution`` blocks that
were attached when the caller asked for them. Building a receipt performs no
search, no index query and no traversal of its own. Where the delivery never
computed something (an evidence record's raw hash, an unanchored record's
origin, a record with no parseable dates), the receipt says *unknown* rather
than inventing it.

Two views, and the difference is load-bearing
---------------------------------------------

:meth:`Receipt.public` is the disclosure-safe view: refs, hashes, dispositions,
coverage, scope, times. **No memory body text, no titles, no excerpts, and not
the caller's query.** It is what the delivery surfaces return.

:meth:`Receipt.diagnostics` is the internal view: everything public, plus the
request fingerprint (which includes the caller's query prose), the surface, the
resolve mode, the reuse key, and per-record roles. It is for operators reading
their own store — it is never returned by a delivery surface.

What is persisted, and where
----------------------------

The receipt is written through the **existing** retrieval-event path
(:mod:`palinode.core.retrieval_log`, ``.audit/retrievals.jsonl``), not a
parallel ledger: the per-file rows that path already writes gain
``bundle_id``, ``policy_version``, ``scope``, ``revision``, ``disposition``,
``lineage_group``, ``coverage`` and ``next_transition``, so a logged retrieval
is self-describing and rows from one delivery join on ``bundle_id``. Raw prose
stays out: refs, hashes and dispositions are written; content never is. The log
keeps the visibility and retention regime it already had (telemetry, excluded
from semantic recall).

``/resolve`` persists the public receipt in a receipt-only envelope on that
same log after rendering, including evidence refs, qualifiers and output budget.
It creates no file-retrieval events or recall metadata.

``/context/prime`` returns its receipt but writes no retrieval rows: a
session-start injection ledger is a separate, larger contract and is not built
here.

.. _reuse-contract:

The cross-request reuse contract
--------------------------------

Palinode caches **per request** (:class:`palinode.core.evidence.RequestCache`)
and nothing here changes that. But a receipt is precisely the evidence a future
cross-request cache would need, so the eligibility contract is recorded now,
next to the data that satisfies it, rather than invented later by whoever
writes the cache.

A previously delivered bundle may be reused for a new request **only if every
one of these still holds at the moment of the new delivery**:

1. **Server-resolved caller access.** The scope chain the *server* resolves for
   the new caller equals the one on the receipt. Never the caller's claim about
   who they are — the same resolution the delivery itself performs.
2. **Query scope.** The normalized request (query text, filters, limits,
   tiering, resolve mode) is identical. Telemetry-only fields (``session_id``)
   are excluded: they do not change what is selected.
3. **Policy version.** :class:`PolicyVersion` — package version, projection
   version, and the fingerprint of the policy-relevant configuration — is
   unchanged. A config edit that widens an evidence budget is a policy change.
4. **Source revisions.** Every ``(ref, revision)`` pair on the receipt still
   matches the store. One changed file invalidates the bundle; an
   ``unknown`` revision can never be shown to still match, so a bundle carrying
   one is not reusable.
5. **Time-sensitive applicability.** The delivery must still fall inside the
   same temporal window — the interval bounded by the nearest known transition
   behind and ahead of the evaluation clock. A file that did not change is not
   evidence that its *acting state* did not change: a record with
   ``expires_at`` at noon is current at 11:59 and expired at 12:01 with no
   write in between, and the key must differ across that boundary.

:func:`reuse_key` derives one string from exactly those five inputs. Equal keys
are a *necessary* condition for reuse, not a sufficient one: a cache must still
re-check caller access and applicable time at delivery, because both can change
without any request or file changing. Claim-validity transitions beyond
``expires_at`` and a declared future ``date`` are not modelled here; richer
temporal semantics are a separate piece of work.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence

from palinode.core.lifecycle import parse_moment
from palinode.core.projection import PROJECTION_VERSION
from palinode.core.resolution import ClaimFacts, claim_facts
from palinode.core.resolution import _origin as _resolution_origin

# ── vocabularies ─────────────────────────────────────────────────────────────

#: How one supplied record was disposed in this delivery.
SELECTED = "selected"
REPLACED = "replaced"
CONFLICT_SIDE = "conflict_side"
INSUFFICIENT = "insufficient"
EVIDENCE_ONLY = "evidence_only"

#: The closed disposition vocabulary. Nothing outside it is ever emitted.
DISPOSITIONS: tuple[str, ...] = (
    SELECTED, REPLACED, CONFLICT_SIDE, INSUFFICIENT, EVIDENCE_ONLY,
)

#: What kind of identifier a ``revision`` is. The domains are different hashes
#: and must never be compared across surfaces: ``index_section_sha256`` is the
#: raw per-section hash the indexer stores and ``store.check_freshness``
#: compares against; ``file_sha256`` is the hash of the whole file as delivered.
REVISION_INDEX_SECTION = "index_section_sha256"
REVISION_FILE = "file_sha256"
REVISION_UNKNOWN = "unknown"

#: The closed basis vocabulary. Nothing outside it is ever emitted.
REVISION_BASES: tuple[str, ...] = (
    REVISION_INDEX_SECTION, REVISION_FILE, REVISION_UNKNOWN,
)

#: Coverage status when the delivery gathered no evidence at all. Distinct from
#: ``complete``: nothing was looked for, so nothing may be concluded about what
#: was missed. ``complete`` / ``partial`` come from
#: :mod:`palinode.core.evidence` unchanged, reasons and all.
COVERAGE_NOT_REQUESTED = "not_requested"

#: Origin kind for a record that named no anchor. Unknown lineage stays
#: unknown — it is never promoted to "independent".
ORIGIN_UNKNOWN = "unknown"

#: Buckets an ``evidence`` block carries, in the order they are supplied.
_EVIDENCE_BUCKETS: tuple[str, ...] = ("replacements", "conflicts", "support", "discovered")

#: Request keys that never change what is selected and so never enter a
#: fingerprint: pure telemetry and the receipt transport flag itself.
_NON_SELECTING_REQUEST_KEYS: frozenset[str] = frozenset({"session_id", "receipt"})


# ── policy version ───────────────────────────────────────────────────────────

#: Config paths whose values decide what a delivery selects and how far it
#: looks. Deliberately small: the smallest honest set, so the fingerprint
#: changes when policy changes and not when an unrelated knob moves.
_POLICY_CONFIG_KEYS: tuple[str, ...] = (
    "scope.enabled",
    "scope.prime_mode",
    "search.exclude_status",
    "search.evidence.max_files",
    "search.evidence.max_edges",
    "search.evidence.max_depth",
    "search.evidence.max_replacement_chain",
    "search.evidence.fallback_max_queries",
    "search.evidence.fallback_max_reads",
)


def _canonical(obj: Any) -> str:
    """Deterministic JSON for hashing — sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _digest(obj: Any, *, length: int = 16) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()[:length]


def _config_value(cfg: Any, dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return node


@dataclass(frozen=True)
class PolicyVersion:
    """What identifies the effective delivery policy, as a three-part tuple.

    ``package``
        The installed ``palinode`` version. Selection, lifecycle, visibility
        and resolution policy are code; the package version identifies it.
    ``projection``
        :data:`palinode.core.projection.PROJECTION_VERSION` — the version the
        current-text projection behind every delivered excerpt was produced
        under. It moves independently of the package version.
    ``config``
        A short fingerprint over :data:`_POLICY_CONFIG_KEYS`, the configured
        values that decide what is in scope and how far evidence may look.
        Configuration is edited without a release, so without this the tuple
        would claim an unchanged policy across a real policy change.

    Nothing else is in it. Prompt versions are not: no prompt runs on the
    delivery path (compaction is the LLM surface, and it is not a delivery).
    """

    package: str
    projection: int
    config: str

    @classmethod
    def current(cls, cfg: Any | None = None) -> PolicyVersion:
        from palinode import __version__
        from palinode.core.config import config as _config

        src = cfg if cfg is not None else _config
        fingerprint = _digest(
            {key: _config_value(src, key) for key in _POLICY_CONFIG_KEYS}, length=8
        )
        return cls(package=str(__version__), projection=int(PROJECTION_VERSION),
                   config=fingerprint)

    def as_str(self) -> str:
        """The compact wire form: ``palinode/0.19.3+projection/1+config/ab12cd34``."""
        return (
            f"palinode/{self.package}"
            f"+projection/{self.projection}"
            f"+config/{self.config}"
        )


# ── supplied records and lineage ─────────────────────────────────────────────


@dataclass(frozen=True)
class SuppliedRecord:
    """One record this delivery supplied, and the revision it supplied it at."""

    ref: str
    #: The exact source revision, in the domain named by ``revision_basis``.
    #: ``None`` when the delivery never computed one for this record.
    revision: str | None
    revision_basis: str
    #: Index/source agreement, assertion currency and cited-span integrity, as
    #: the delivery computed them (``store.check_freshness``). ``None`` when
    #: this surface does not compute that axis.
    freshness: str | None = None
    currency: str | None = None
    span_integrity: str | None = None
    disposition: str = SELECTED
    #: The support origin this record rests on, and how it was established.
    #: ``None`` / ``unknown`` means the record named no anchor — not that it
    #: was shown to be an independent observation.
    origin: str | None = None
    origin_kind: str = ORIGIN_UNKNOWN
    #: ``seed`` (a hit the request itself returned) or the evidence bucket the
    #: record was carried in. Diagnostics only.
    role: str = "seed"

    def public(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "revision": self.revision,
            "revision_basis": self.revision_basis,
            "freshness": self.freshness,
            "currency": self.currency,
            "span_integrity": self.span_integrity,
            "disposition": self.disposition,
            "origin": self.origin,
            "origin_kind": self.origin_kind,
        }

    def diagnostics(self) -> dict[str, Any]:
        return {**self.public(), "role": self.role}


@dataclass(frozen=True)
class LineageGroup:
    """Records that rest on one origin. Copies of one origin count once.

    A session summary and a snapshot that cite the same anchor as the
    observation they copied are three records in one group, not three
    observations. ``status`` is ``known`` when the group has a named anchor and
    ``unknown`` when it does not; an unknown group is one record's own
    un-established lineage, never a claim of independence.
    """

    origin: str | None
    origin_kind: str
    #: The revision of the origin *as this delivery supplied it* — set only
    #: when the origin itself is one of the supplied records. ``None``
    #: otherwise: the origin was named, not delivered, so its revision here
    #: would be a guess.
    origin_revision: str | None
    members: tuple[str, ...]
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "origin_kind": self.origin_kind,
            "origin_revision": self.origin_revision,
            "members": list(self.members),
            "status": self.status,
        }


# ── the receipt ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Receipt:
    """The qualified record of one context delivery."""

    bundle_id: str
    policy_version: PolicyVersion
    #: The caller scope the **server** resolved, as a chain (``[]`` = no scope
    #: identity: access control only).
    scope: tuple[str, ...]
    #: The clock the delivery's lifecycle policy used.
    requested_time: str
    #: When this result was evaluated.
    evaluated_at: str
    #: The nearest known relevant transition ahead of ``evaluated_at`` among
    #: the delivered records (earliest ``expires_at``, or a declared future
    #: effective ``date``). ``None`` when none is known — an unknown boundary
    #: stays unknown and is never defaulted to "never".
    next_transition: str | None
    supplied: tuple[SuppliedRecord, ...]
    lineage: tuple[LineageGroup, ...]
    coverage: dict[str, Any]
    dispositions: dict[str, int]
    #: Diagnostics-only context.
    surface: str = "search"
    resolve_mode: str = "none"
    request: dict[str, Any] = field(default_factory=dict, repr=False)
    #: The transition moments bracketing ``evaluated_at``; the time half of the
    #: reuse key. ``(previous, next)``, either side ``None`` when unknown.
    window: tuple[str | None, str | None] = (None, None)

    # ── views ────────────────────────────────────────────────────────────
    def reference(self) -> dict[str, Any]:
        """The two-field reference: what a delivery that gathered no evidence carries.

        Enough to correlate the response with the logged delivery and to ask
        for the rest later; nothing that would grow an ordinary response.
        """
        return {"bundle_id": self.bundle_id, "evaluated_at": self.evaluated_at}

    def public(self) -> dict[str, Any]:
        """The disclosure-safe view — refs, revisions, dispositions; no content."""
        return {
            "bundle_id": self.bundle_id,
            "policy_version": self.policy_version.as_str(),
            "scope": list(self.scope),
            "requested_time": self.requested_time,
            "evaluated_at": self.evaluated_at,
            "next_transition": self.next_transition,
            "supplied": [s.public() for s in self.supplied],
            "lineage": [g.to_dict() for g in self.lineage],
            "coverage": dict(self.coverage),
            "dispositions": dict(self.dispositions),
        }

    def diagnostics(self) -> dict[str, Any]:
        """The internal view — the public view plus request, surface and reuse key.

        Carries the caller's query prose (through ``request``), which the
        public view deliberately omits. Never returned by a delivery surface.
        """
        return {
            **self.public(),
            "supplied": [s.diagnostics() for s in self.supplied],
            "surface": self.surface,
            "resolve_mode": self.resolve_mode,
            "request": dict(self.request),
            "window": list(self.window),
            "reuse_key": self.reuse_key(),
        }

    def to_dict(self, *, view: str = "public") -> dict[str, Any]:
        """``view="public"`` (default), ``"diagnostics"`` or ``"reference"``."""
        if view == "public":
            return self.public()
        if view == "diagnostics":
            return self.diagnostics()
        if view == "reference":
            return self.reference()
        raise ValueError(f"unknown receipt view {view!r}")

    # ── reuse ────────────────────────────────────────────────────────────
    def revisions(self) -> tuple[tuple[str, str | None], ...]:
        """``(ref, revision)`` for every supplied record, in ref order."""
        return tuple(sorted((s.ref, s.revision) for s in self.supplied))

    def reuse_key(self) -> str:
        """This delivery's reuse key (see :ref:`the reuse contract <reuse-contract>`)."""
        return reuse_key(
            scope=self.scope,
            query_scope=self.request,
            policy_version=self.policy_version,
            revisions=self.revisions(),
            window=self.window,
        )


def reuse_key(
    *,
    scope: Sequence[str] | None,
    query_scope: Mapping[str, Any],
    policy_version: PolicyVersion | str,
    revisions: Iterable[tuple[str, str | None]],
    window: tuple[str | None, str | None],
) -> str:
    """Derive the cross-request reuse key from the five contract inputs.

    Nothing caches on this yet — it is the contract a cache must satisfy,
    pinned as a function so the eventual cache consumes a derivation that was
    reviewed here rather than inventing its own. Equal keys are necessary, not
    sufficient: caller access and applicable time are re-checked at delivery.

    ``window`` is the ``(previous, next)`` transition pair bracketing the
    evaluation clock — the reason an unchanged file does not imply an
    unchanged answer across an ``expires_at`` boundary.
    """
    policy = policy_version.as_str() if isinstance(policy_version, PolicyVersion) else str(policy_version)
    return _digest(
        {
            "scope": sorted(scope or ()),
            "query_scope": request_scope(query_scope),
            "policy_version": policy,
            "revisions": sorted([list(pair) for pair in revisions]),
            "window": list(window),
        },
        length=32,
    )


def request_scope(request: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a request into the part of it that decides what is selected.

    Drops unset values and the keys that are telemetry or transport rather
    than selection (:data:`_NON_SELECTING_REQUEST_KEYS`), so two requests that
    differ only in ``session_id`` share a query scope.
    """
    return {
        k: v
        for k, v in sorted(request.items())
        if v is not None and k not in _NON_SELECTING_REQUEST_KEYS
    }


def derive_bundle_id(
    *,
    request: Mapping[str, Any],
    refs: Sequence[tuple[str, str | None]],
    evaluated_at: str,
) -> str:
    """The delivery's identity: request + result refs/revisions + evaluation time.

    Deterministic for a given delivery and different for the next one, which is
    what makes it usable as the correlation key between the response, the
    retrieval log and (later) a bundle.
    """
    return _digest({
        "request": request_scope(request),
        "refs": sorted([list(pair) for pair in refs]),
        "evaluated_at": evaluated_at,
    })


# ── building ─────────────────────────────────────────────────────────────────


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _ref_of(path: str) -> str:
    """A memory path in any spelling reduced to its ref (``decisions/x``)."""
    p = os.path.normpath(str(path or ""))
    return p[:-3] if p.endswith(".md") else p


def _rel_of(row: Mapping[str, Any], memory_dir: str | None) -> str:
    rel = str(row.get("rel_path") or "")
    if rel:
        return rel
    raw = str(row.get("file_path") or row.get("file") or "")
    if memory_dir and os.path.isabs(raw):
        try:
            return os.path.relpath(raw, memory_dir)
        except ValueError:
            return raw
    return raw


def _declared(row: Mapping[str, Any], key: str, allowed: Sequence[str]) -> str | None:
    """A value the delivery already decided, when it named one in the row.

    Two deliveries know something the inference below can only guess at: the
    bundle (:mod:`palinode.core.bundle`) grouped its own records into the
    dispositions before it rendered them, and it knows which hash domain each
    revision came from. When a row states either, it is taken — inference is
    for rows that say nothing. Anything outside the closed vocabulary is
    ignored rather than emitted.
    """
    value = row.get(key)
    return str(value) if isinstance(value, str) and value in allowed else None


def _disposition(
    *,
    ref: str,
    currency: str | None,
    bucket: str,
    outcome: str | None,
    current_ref: str | None,
) -> str:
    """How one record was disposed, from what the delivery already decided.

    Order matters: the record that stands is ``selected`` whatever bucket it
    arrived in, a retired record is ``replaced`` before it can be read as a
    live side, and a bucket-less unknown falls to ``evidence_only`` rather than
    to anything that sounds settled.
    """
    if current_ref and ref == current_ref:
        return SELECTED
    if currency == "retired":
        return REPLACED
    if bucket == "conflicts":
        return CONFLICT_SIDE
    if bucket == "seed":
        if outcome == "unresolved_conflict":
            return CONFLICT_SIDE
        if outcome == "insufficient_evidence":
            return INSUFFICIENT
        if outcome is None:
            # No resolution was asked for: the record's own currency is the
            # only thing the delivery decided about it.
            return CONFLICT_SIDE if currency == "contested" else SELECTED
        # A record stands and it is not this one, and this one is not retired:
        # it lost on a policy that retires nothing (a proposal against a
        # decision), so it stays a visible side rather than a replacement.
        return CONFLICT_SIDE
    return EVIDENCE_ONLY


def _origin_of(facts: ClaimFacts) -> tuple[str | None, str]:
    """The origin anchor a record names, or unknown. One definition, shared.

    Delegates to :mod:`palinode.core.resolution` so the receipt's lineage and
    the resolution block's support groups can never disagree about what counts
    as an origin.
    """
    origin, kind = _resolution_origin(facts)
    return (origin or None, kind) if kind != ORIGIN_UNKNOWN else (None, ORIGIN_UNKNOWN)


def _transitions(meta: Mapping[str, Any] | None) -> list[datetime]:
    """Known temporal boundaries a record declares. Unparseable → nothing.

    The two moments the delivery's policy actually gates on: ``expires_at``
    (the record stops applying) and a declared ``date`` (a future one means it
    does not apply yet). A malformed or absent date contributes no transition
    at all rather than a defaulted one.
    """
    if not isinstance(meta, Mapping):
        return []
    out: list[datetime] = []
    for key in ("expires_at", "date"):
        moment = parse_moment(meta.get(key))
        if moment is not None:
            out.append(moment)
    return out


def _qualifier_transitions(side: Mapping[str, Any] | None) -> list[datetime]:
    """``expires_at:<value>`` transitions carried on a resolution side's qualifiers.

    An evidence record's frontmatter is not serialized into the response, but
    the resolution layer already projected its ``expires_at`` into a qualifier
    string — so this reads a boundary the delivery computed rather than
    re-reading the file.
    """
    if not isinstance(side, Mapping):
        return []
    out: list[datetime] = []
    for qualifier in side.get("qualifiers") or []:
        text = str(qualifier)
        if text.startswith("expires_at:"):
            moment = parse_moment(text.split(":", 1)[1].strip())
            if moment is not None:
                out.append(moment)
    return out


def _window(transitions: Iterable[datetime], evaluated: datetime) -> tuple[str | None, str | None]:
    """The transition pair bracketing ``evaluated``: ``(previous, next)``."""
    before = [t for t in transitions if t <= evaluated]
    after = [t for t in transitions if t > evaluated]
    return (_iso(max(before)) if before else None, _iso(min(after)) if after else None)


def _lineage(supplied: Sequence[SuppliedRecord]) -> tuple[LineageGroup, ...]:
    """Group supplied records by the origin each names; copies count once.

    A record that names no anchor forms its own group with ``status:
    unknown``: its lineage was never established, which is not the same as
    having been shown independent.
    """
    revision_by_ref = {s.ref: s.revision for s in supplied}
    groups: dict[str, dict[str, Any]] = {}
    for rec in supplied:
        key = rec.origin or f"\x00unknown:{rec.ref}"
        group = groups.setdefault(key, {
            "origin": rec.origin,
            "kind": rec.origin_kind,
            "members": [],
            "status": "known" if rec.origin else "unknown",
        })
        # The first member that established a kind names the group; a side
        # that only carried the origin string does not downgrade it.
        if group["kind"] == ORIGIN_UNKNOWN and rec.origin_kind != ORIGIN_UNKNOWN:
            group["kind"] = rec.origin_kind
        if rec.ref not in group["members"]:
            group["members"].append(rec.ref)
    out: list[LineageGroup] = []
    for key in sorted(groups):
        g = groups[key]
        origin = g["origin"]
        out.append(LineageGroup(
            origin=origin,
            origin_kind=g["kind"],
            # Only when the origin is itself one of the delivered records:
            # anything else would be a revision this delivery never saw.
            origin_revision=revision_by_ref.get(_ref_of(origin)) if origin else None,
            members=tuple(g["members"]),
            status=g["status"],
        ))
    return tuple(out)


def _counts(supplied: Sequence[SuppliedRecord]) -> dict[str, int]:
    counts = {d: 0 for d in DISPOSITIONS}
    for rec in supplied:
        counts[rec.disposition] = counts.get(rec.disposition, 0) + 1
    return counts


def _coverage(results: Sequence[Mapping[str, Any]], resolve_mode: str) -> dict[str, Any]:
    if resolve_mode in ("", "none"):
        return {"status": COVERAGE_NOT_REQUESTED, "reasons": []}
    from palinode.core.evidence import fold_coverage

    return fold_coverage([r.get("evidence") for r in results])


def build_receipt(
    results: Sequence[Mapping[str, Any]],
    *,
    request: Mapping[str, Any],
    scope: Sequence[str] | None = None,
    resolve_mode: str = "none",
    surface: str = "search",
    now: datetime | None = None,
    evaluated_at: datetime | None = None,
    memory_dir: str | None = None,
    coverage: Mapping[str, Any] | None = None,
) -> Receipt:
    """Build the receipt for a search-shaped delivery. Pure; no lookups.

    ``results`` are the rows exactly as the delivery is about to return them —
    ``rel_path`` / ``file_path``, ``content_hash`` (the raw section hash
    ``store.check_freshness`` compares), ``freshness`` / ``currency`` /
    ``span_integrity``, indexed ``metadata``, and the ``evidence`` /
    ``resolution`` blocks when the caller asked for them. ``scope`` is the
    chain the server resolved. ``now`` is the lifecycle clock the delivery
    used (``None`` = the wall clock, which is then also the evaluation time).

    Records carried in an ``evidence`` block are supplied context too, so they
    are on the receipt, at the whole-file revision the evidence layer hashed
    when it read them (``file_sha256`` — never comparable with a search row's
    indexed per-section hash). A record that reaches here without one is
    reported ``unknown``; unknown stays unknown.

    ``coverage`` is the delivery's own coverage when it computed one that the
    rows cannot reproduce — the bundle folds its packing reasons into it, and
    an empty bundle has no row to fold from. Omitted, coverage comes from the
    rows' evidence blocks as before.
    """
    evaluated = evaluated_at or datetime.now(UTC)
    requested = now or evaluated

    supplied: list[SuppliedRecord] = []
    transitions: list[datetime] = []
    seen: set[str] = set()

    for row in results:
        rel = _rel_of(row, memory_dir)
        ref = _ref_of(rel)
        if not ref:
            continue
        meta = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        resolution = row.get("resolution") if isinstance(row.get("resolution"), Mapping) else None
        outcome = str(resolution.get("outcome")) if resolution else None
        current = resolution.get("current") if resolution else None
        current_ref = str(current.get("ref")) if isinstance(current, Mapping) and current.get("ref") else None
        transitions.extend(_transitions(meta))

        # Origins and temporal boundaries the resolution layer already
        # projected, keyed by ref. These come from live frontmatter, so they
        # win over the indexed metadata below wherever both exist.
        origins: dict[str, tuple[str | None, str]] = {}
        if resolution:
            for group in resolution.get("support") or []:
                if not isinstance(group, Mapping):
                    continue
                kind = str(group.get("origin_kind") or ORIGIN_UNKNOWN)
                anchor = group.get("origin")
                for member in group.get("members") or []:
                    if isinstance(member, Mapping) and member.get("ref"):
                        transitions.extend(_qualifier_transitions(member))
                        origins[str(member["ref"])] = (
                            (str(anchor) if anchor and kind != ORIGIN_UNKNOWN else None), kind,
                        )
            for side in [current, *(resolution.get("sides") or [])]:
                if not isinstance(side, Mapping) or not side.get("ref"):
                    continue
                transitions.extend(_qualifier_transitions(side))
                anchor = side.get("origin")
                # A side carries the origin it rests on but not how that origin
                # was established; the group takes its kind from whichever
                # member does know (see :func:`_lineage`).
                if anchor and not origins.get(str(side["ref"]), (None, ""))[0]:
                    origins[str(side["ref"])] = (str(anchor), ORIGIN_UNKNOWN)

        if ref not in seen:
            seen.add(ref)
            origin, origin_kind = origins.get(ref, (None, ORIGIN_UNKNOWN))
            if origin is None:
                origin, origin_kind = _origin_of(claim_facts(meta, ref=ref, now=now))
            revision = str(row.get("content_hash")) if row.get("content_hash") else None
            supplied.append(SuppliedRecord(
                ref=ref,
                revision=revision,
                revision_basis=(
                    _declared(row, "revision_basis", REVISION_BASES)
                    or (REVISION_INDEX_SECTION if revision else REVISION_UNKNOWN)
                ),
                freshness=row.get("freshness"),
                currency=row.get("currency"),
                span_integrity=row.get("span_integrity"),
                disposition=_declared(row, "disposition", DISPOSITIONS) or _disposition(
                    ref=ref, currency=row.get("currency"), bucket="seed",
                    outcome=outcome, current_ref=current_ref,
                ),
                origin=origin,
                origin_kind=origin_kind,
                role=str(row.get("role") or "seed"),
            ))

        evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else None
        if not evidence:
            continue
        for bucket in _EVIDENCE_BUCKETS:
            for rec in evidence.get(bucket) or []:
                if not isinstance(rec, Mapping) or not rec.get("ref"):
                    continue
                rec_ref = _ref_of(str(rec["ref"]))
                if rec_ref in seen:
                    continue
                seen.add(rec_ref)
                rec_origin, rec_kind = origins.get(rec_ref, (None, ORIGIN_UNKNOWN))
                rec_revision = str(rec["content_hash"]) if rec.get("content_hash") else None
                supplied.append(SuppliedRecord(
                    ref=rec_ref,
                    # The whole file as the evidence layer read it, hashed
                    # there because the bytes were already in hand. A
                    # different domain from a search row's indexed section
                    # hash, which is why the basis is named on every record.
                    revision=rec_revision,
                    revision_basis=REVISION_FILE if rec_revision else REVISION_UNKNOWN,
                    freshness=rec.get("freshness"),
                    currency=rec.get("currency"),
                    span_integrity=None,
                    disposition=_disposition(
                        ref=rec_ref, currency=rec.get("currency"), bucket=bucket,
                        outcome=outcome, current_ref=current_ref,
                    ),
                    origin=rec_origin,
                    origin_kind=rec_kind,
                    role=bucket,
                ))

    return _assemble(
        supplied=supplied,
        transitions=transitions,
        coverage=dict(coverage) if coverage is not None else _coverage(results, resolve_mode),
        request=request,
        scope=scope,
        resolve_mode=resolve_mode,
        surface=surface,
        requested=requested,
        evaluated=evaluated,
    )


#: Digest sections whose rows are delivered records, in delivery order.
_DIGEST_SECTIONS: tuple[str, ...] = (
    "recent_snapshots", "core_memories", "recent_decisions", "open_action_items",
)


def build_digest_receipt(
    digest: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    scope: Sequence[str] | None = None,
    memory_dir: str,
    now: datetime | None = None,
    evaluated_at: datetime | None = None,
) -> Receipt:
    """Build the receipt for a ``/context/prime`` digest delivery.

    The digest rows name files but carry no revision: the prime path reads
    frontmatter and keeps neither the bytes nor a hash, and it deliberately
    never touches the index. So the revision here is the SHA-256 of the file as
    delivered (``file_sha256``) — a different domain from the indexed
    per-section hash a search row carries, which is why the basis is named on
    every record and the two are never compared. The re-read is bounded by the
    digest's own caps (a couple of dozen files at most) and reaches only files
    this delivery already read.

    A file that cannot be read contributes an ``unknown`` revision rather than
    dropping out of the receipt: it was still supplied.
    """
    evaluated = evaluated_at or datetime.now(UTC)
    requested = now or evaluated
    supplied: list[SuppliedRecord] = []
    transitions: list[datetime] = []
    seen: set[str] = set()

    for section in _DIGEST_SECTIONS:
        for row in digest.get(section) or []:
            if not isinstance(row, Mapping) or not row.get("file"):
                continue
            rel = str(row["file"])
            ref = _ref_of(rel)
            if ref in seen:
                continue
            seen.add(ref)
            revision, meta = _read_revision(os.path.join(memory_dir, rel))
            facts = claim_facts(meta, ref=ref, now=now)
            origin, origin_kind = _origin_of(facts)
            transitions.extend(_transitions(meta))
            supplied.append(SuppliedRecord(
                ref=ref,
                revision=revision,
                revision_basis=REVISION_FILE if revision else REVISION_UNKNOWN,
                # The digest performs no index comparison, so index/source
                # agreement is not something this surface knows.
                freshness=None,
                currency="contested" if row.get("contradicts") else "current",
                span_integrity=None,
                disposition=CONFLICT_SIDE if row.get("contradicts") else SELECTED,
                origin=origin,
                origin_kind=origin_kind,
                role=section,
            ))

    return _assemble(
        supplied=supplied,
        transitions=transitions,
        coverage={"status": COVERAGE_NOT_REQUESTED, "reasons": []},
        request=request,
        scope=scope,
        resolve_mode="none",
        surface="context_prime",
        requested=requested,
        evaluated=evaluated,
    )


def _read_revision(path: str) -> tuple[str | None, dict[str, Any]]:
    """``(sha256-of-file, frontmatter)`` for a delivered digest row."""
    from palinode.core import parser

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None, {}
    try:
        meta, _ = parser.parse_frontmatter(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        meta = {}
    return hashlib.sha256(raw).hexdigest(), meta if isinstance(meta, dict) else {}


def _assemble(
    *,
    supplied: list[SuppliedRecord],
    transitions: list[datetime],
    coverage: dict[str, Any],
    request: Mapping[str, Any],
    scope: Sequence[str] | None,
    resolve_mode: str,
    surface: str,
    requested: datetime,
    evaluated: datetime,
) -> Receipt:
    """Shared tail: window, lineage, counts, bundle id."""
    window = _window(transitions, evaluated)
    records = tuple(supplied)
    evaluated_iso = evaluated.isoformat()
    fingerprint = request_scope(request)
    return Receipt(
        bundle_id=derive_bundle_id(
            request=fingerprint,
            refs=[(s.ref, s.revision) for s in records],
            evaluated_at=evaluated_iso,
        ),
        policy_version=PolicyVersion.current(),
        scope=tuple(scope or ()),
        requested_time=requested.isoformat(),
        evaluated_at=evaluated_iso,
        next_transition=window[1],
        supplied=records,
        lineage=_lineage(records),
        coverage=coverage,
        dispositions=_counts(records),
        surface=surface,
        resolve_mode=resolve_mode,
        request=fingerprint,
        window=window,
    )


__all__ = [
    "COVERAGE_NOT_REQUESTED",
    "DISPOSITIONS",
    "LineageGroup",
    "PolicyVersion",
    "REVISION_BASES",
    "REVISION_FILE",
    "REVISION_INDEX_SECTION",
    "REVISION_UNKNOWN",
    "Receipt",
    "SuppliedRecord",
    "build_digest_receipt",
    "build_receipt",
    "derive_bundle_id",
    "request_scope",
    "reuse_key",
]
