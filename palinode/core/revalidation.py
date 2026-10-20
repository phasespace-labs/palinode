"""Revision-aware support checks — is what this record rests on still standing?

``backed_by`` propagation (:mod:`palinode.consolidation.propagate`) flags the
*direct* dependents of a source when a retirement fires on it, and the flag
clears when the dependent is re-saved. That leaves three gaps this module
closes, all of them read-time and none of them a rewrite of anybody's prose:

**Second-hop conclusions.** Propagation walks one hop. A record resting on a
record resting on a retired source carried no qualification at all until some
later maintenance pass reached it. :func:`check_support` walks the backing
graph to :data:`DEFAULT_MAX_HOPS` hops (budgeted, cycle-safe) and reports the
second-hop root by name, with the hop it was found at.

**Withdrawn support is not a disproven conclusion.** A source that was
archived, superseded, expired, retired by location — or retracted with nothing
named as falsifying it — took its support with it: the dependent is
*uncertain* (:data:`SUPPORT_WITHDRAWN`). Only a source that names its evidence
(``falsified_by``, ADR-020's evidence-gated RETRACT) says the stronger thing
about the dependent (:data:`SUPPORT_DISPROVEN`) — and still not that the
opposite claim is true. Neither reason ever elects a value: a retraction
without a replacement yields uncertainty, exactly as a withdrawn *replacement*
does in :mod:`palinode.core.resolution` (``replacement_withdrawn``).

**A re-save is not a re-verification.** The convention that re-saving a
dependent clears its ``stale_backing`` flag reads a formatting-only edit as
"I checked this against its sources". Explicit evidence replaces the
convention: a record may carry ``revalidated:`` entries naming the source and
the exact revision checked, and a backing counts as revalidated only when the
recorded revision equals the source's **current** whole-file SHA-256 (the
delivery receipt's ``file_sha256`` revision basis). Re-saving the dependent changes the
dependent's bytes and certifies nothing about the source.

Frontmatter this module reads
-----------------------------

``backed_by: [<ref>, …]``
    Unchanged — the declared support. Read through
    :func:`palinode.core.typed_links.parse_link_refs`.
``backing_policy: all-of | any-of``
    Optional, and the only thing that lets a checker draw a conclusion from
    more than one source. ``all-of``: the record stands only while *every*
    named source stands. ``any-of``: it stands while *at least one* does. A
    list with no declared policy is :data:`POLICY_ADVISORY` — findings are
    reported per source and nothing is concluded automatically, which is what
    every legacy ``backed_by`` list means.
``revalidated: [{ref, revision, at}, …]``
    Optional revalidation receipts. ``revision`` is the source's whole-file
    SHA-256 at the moment it was checked; ``at`` is a UTC ISO-8601 stamp.
    One entry per source ref: re-recording the same ``(ref, revision)`` is a
    no-op, and a new revision replaces the entry (git holds the history).
``stale_backing: [{ref, op, at, …}, …]``
    Unchanged — what the retirement path persisted. Read here only to decide
    what a revalidation receipt may clear.

What this module never does
---------------------------

It never decides truth, never rewrites a body, and never derives a replacement
value: a dependent whose source was retired is qualified, not re-worded, and
the only replacement that may ever be followed is an explicit ``superseded_by``
chain, which is :mod:`palinode.core.resolution`'s job and not this module's.
:func:`check_support` is pure — frontmatter, an injected clock and an injected
reader in, a frozen :class:`SupportCheck` out — so the same store state and the
same ``now`` produce the same answer on every surface.

Caching
-------

:class:`SupportCache` is **per request** and nothing here caches across one.
Its reuse test is the delivery-receipt contract
(:ref:`the five conditions <reuse-contract>`) applied to a support check
rather than to a bundle: a warm entry is served only when the caller scope,
the policy version, every input ``(ref, revision)`` pair and the temporal
window bracketing the clock all still hold. Two of those cannot be seen in the
files at all — **an unchanged file is not evidence of an unchanged answer**: a
source with ``expires_at`` at noon is current at 11:59 and expired at 12:01
with nothing written in between, and a caller whose scope changed must not be
served the previous caller's evidence. Any cross-request reuse built later
must re-check caller access and applicable time *at delivery*, not only at the
key, for exactly that reason.

Persistent work
---------------

Read-time findings qualify a delivery; they do not touch disk. Where a finding
should become durable state, it goes through the paths that already exist:
:func:`enqueue_revalidation` writes the write-time marker
(:func:`palinode.consolidation.write_time._write_marker`) with a ``revalidate``
item, and :func:`apply_revalidation` — run by the same sweep/worker that drains
every other marker — re-checks against live disk, appends one ``stale_backing``
entry per new finding, removes entries a matching-revision receipt has
cleared, re-indexes and commits through ``git_tools``. Markdown and git
reconstruct the state; a marker that fails is renamed ``.failed.json`` for an
operator, as every other marker is.
"""
from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence

from palinode.core.lifecycle import eligibility, parse_moment
from palinode.core.receipt import PolicyVersion, _transitions, _window, reuse_key
from palinode.core.typed_links import parse_link_refs

logger = logging.getLogger("palinode.core.revalidation")

# ── vocabulary ───────────────────────────────────────────────────────────────

#: A backing source is retired — archived, deprecated, superseded, expired, or
#: retired by location. The support is gone; the dependent is uncertain.
SUPPORT_WITHDRAWN = "support_withdrawn"
#: A backing source was explicitly shown false (``retracted`` lifecycle, or a
#: ``falsified_by`` naming the evidence). Stronger than withdrawn, and still
#: not a claim that the opposite of the dependent holds.
SUPPORT_DISPROVEN = "support_disproven"
#: A backing source is standing, but its current revision is not the one this
#: record recorded as revalidated. A prompt to re-verify, never a withdrawal.
SUPPORT_REVISION_CHANGED = "support_revision_changed"

#: The closed finding vocabulary. Nothing outside it is ever emitted.
SUPPORT_REASONS: frozenset[str] = frozenset(
    {SUPPORT_WITHDRAWN, SUPPORT_DISPROVEN, SUPPORT_REVISION_CHANGED}
)

#: The reasons that decide an outcome. A changed revision is a qualification:
#: it says the source moved, not that it stopped supporting anything.
DECISIVE_REASONS: frozenset[str] = frozenset({SUPPORT_WITHDRAWN, SUPPORT_DISPROVEN})

#: Coverage reason: the walk stopped with backing left unexamined (the hop
#: bound, the read budget, or both).
BUDGET_SUPPORT_HOPS = "budget_exhausted:support_hops"
#: Coverage reason: a named source could not be read. Absence is not a
#: retirement — a ref with no file behind it produces no finding at all.
TARGET_MISSING = "target_missing"
#: Coverage reason: a named source exists but the requester may not see it.
TARGET_HIDDEN = "target_hidden"

#: Backing policies. ``advisory`` is the default for a list that declares
#: none: findings are reported per source and no conclusion is drawn.
POLICY_ALL_OF = "all-of"
POLICY_ANY_OF = "any-of"
POLICY_ADVISORY = "advisory"
BACKING_POLICIES: tuple[str, ...] = (POLICY_ALL_OF, POLICY_ANY_OF, POLICY_ADVISORY)

#: What the checker is willing to say about a record's declared support.
STATUS_NO_BACKING = "no_backing"
#: ``all-of`` and every named source stands.
STATUS_FULLY_SUPPORTED = "fully_supported"
#: ``any-of`` and at least one named source stands.
STATUS_STILL_SUPPORTED = "still_supported"
#: An explicit policy the current state does not satisfy.
STATUS_UNSUPPORTED = "unsupported"
#: A legacy list: findings reported, nothing concluded.
STATUS_ADVISORY = "advisory"
SUPPORT_STATUSES: tuple[str, ...] = (
    STATUS_NO_BACKING, STATUS_FULLY_SUPPORTED, STATUS_STILL_SUPPORTED,
    STATUS_UNSUPPORTED, STATUS_ADVISORY,
)

#: Frontmatter fields.
BACKING_POLICY_FIELD = "backing_policy"
REVALIDATED_FIELD = "revalidated"
STALE_BACKING_FIELD = "stale_backing"

#: The ``op`` recorded on a ``stale_backing`` entry this module persists — what
#: produced the flag, not what retired the source (that is the ``reason``).
#: Distinct from the retirement ops and from ``restore-check``.
REVALIDATE_CHECK_OP = "revalidate-check"

#: Hops of ``backed_by`` followed from a record: the record's own sources and
#: those sources' sources. Two is what the filed defect names; deeper walks
#: are budgeted the same way and cost file reads inside a request.
DEFAULT_MAX_HOPS = 2

#: Marker item kind for the deterministic revalidation job.
MARKER_KIND = "revalidate"


# ── frontmatter accessors ────────────────────────────────────────────────────


def normalize_ref(ref: str) -> str:
    """Canonical comparison form of a ``category/slug`` ref (no ``.md``)."""
    r = str(ref).strip().replace(os.sep, "/").lstrip("/")
    return r[:-3] if r.endswith(".md") else r


def backing_policy(meta: Mapping[str, Any] | None) -> str:
    """The declared :data:`BACKING_POLICIES` value, or :data:`POLICY_ADVISORY`.

    Soft-fail, like every other frontmatter read on the delivery path: an
    unrecognised value is advisory rather than an error, because an unreadable
    policy must not be able to make the checker *more* confident.
    """
    raw = (meta or {}).get(BACKING_POLICY_FIELD)
    if not isinstance(raw, str):
        return POLICY_ADVISORY
    value = raw.strip().lower().replace("_", "-")
    return value if value in (POLICY_ALL_OF, POLICY_ANY_OF) else POLICY_ADVISORY


def parse_revalidations(meta: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The ``revalidated`` receipts of parsed frontmatter (soft-fail).

    An entry needs a non-empty ``ref`` and a non-empty ``revision`` to mean
    anything — a receipt that names no revision certifies nothing, which is
    the whole point — so entries missing either are dropped, as are non-dicts
    and a non-list field.
    """
    raw = (meta or {}).get(REVALIDATED_FIELD)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("ref") or "").strip() and str(entry.get("revision") or "").strip():
            out.append(entry)
    return out


def revalidated_revisions(meta: Mapping[str, Any] | None) -> dict[str, str]:
    """``{normalized source ref: recorded revision}`` from ``revalidated``.

    First entry per ref wins, so a hand-edited duplicate cannot silently
    upgrade an older receipt.
    """
    out: dict[str, str] = {}
    for entry in parse_revalidations(meta):
        ref = normalize_ref(str(entry["ref"]))
        if ref and ref not in out:
            out[ref] = str(entry["revision"]).strip()
    return out


def build_revalidation(ref: str, revision: str, *, at: datetime | None = None) -> dict[str, Any]:
    """One ``revalidated`` receipt, fixed key order."""
    moment = at or datetime.now(UTC)
    return {
        "ref": normalize_ref(ref),
        "revision": str(revision).strip(),
        "at": moment.isoformat(timespec="seconds"),
    }


def merge_revalidation_into_content(content: str, entry: Mapping[str, Any]) -> str:
    """Return ``content`` with ``entry`` recorded under ``revalidated``.

    Idempotent on ``(ref, revision)``: re-recording a revalidation of the same
    source revision returns ``content`` unchanged, so a caller can skip a
    no-op write and commit. A *different* revision for a ref already present
    replaces that ref's entry — one current receipt per source, with the
    previous one recoverable from git. Body preserved verbatim; frontmatter
    re-dumped with key order kept (the same shape as the typed-links and
    ``stale_backing`` mergers).
    """
    import frontmatter as _frontmatter
    import yaml

    post = _frontmatter.loads(content)
    existing = parse_revalidations(post.metadata)
    ref = normalize_ref(str(entry["ref"]))
    revision = str(entry["revision"]).strip()
    merged: list[dict[str, Any]] = []
    for item in existing:
        if normalize_ref(str(item["ref"])) != ref:
            merged.append(item)
        elif str(item["revision"]).strip() == revision:
            return content  # already recorded at this revision — no-op
    merged.append(dict(entry))

    meta = dict(post.metadata)
    meta[REVALIDATED_FIELD] = merged
    dumped = yaml.safe_dump(
        meta, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return f"---\n{dumped}---\n\n{post.content}\n"


# ── the reader seam ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceView:
    """One backing source as some reader saw it. The seam this module reads through.

    Deliberately small: the walk needs the frontmatter (for the next hop and
    the lifecycle), the revision (for the receipt comparison) and the
    lifecycle verdict. Whoever builds it owns the visibility gate — a source
    the requester may not see is never handed here as content, only as
    ``hidden``.
    """

    ref: str
    meta: Mapping[str, Any] = field(default_factory=dict)
    #: Whole-file SHA-256 as read (``file_sha256``), or ``None`` when unknown.
    revision: str | None = None
    #: :mod:`palinode.core.lifecycle` state and the signal that decided it.
    state: str = "unmarked"
    state_reason: str = "unmarked"
    hidden: bool = False

    @classmethod
    def of(
        cls,
        ref: str,
        meta: Mapping[str, Any],
        *,
        revision: str | None = None,
        path: str | None = None,
        now: datetime | None = None,
    ) -> SourceView:
        """Build a view by classifying ``meta`` through the lifecycle module."""
        elig = eligibility(dict(meta), path=path or f"{normalize_ref(ref)}.md", now=now)
        return cls(
            ref=normalize_ref(ref), meta=dict(meta), revision=revision,
            state=elig.state, state_reason=elig.reason,
        )


#: ``ref -> view``; ``None`` means the ref named nothing readable.
Reader = Callable[[str], "SourceView | None"]


def _falsified_by(meta: Mapping[str, Any]) -> bool:
    raw = meta.get("falsified_by")
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, list):
        return any(isinstance(r, str) and r.strip() for r in raw)
    return False


def support_reason(view: SourceView) -> str | None:
    """Why this source no longer supports anything, or ``None`` if it still does.

    The withdrawn/disproven split, and **evidence is what separates them**. A
    source that was archived, superseded, deprecated, expired, retired by
    location — or retracted with nothing named as falsifying it — is no longer
    in force: its support is *withdrawn* and the dependent is uncertain. Only
    a source that names what showed it false (``falsified_by``, ADR-020's
    evidence-gated RETRACT) makes the dependent's conclusion *disproven*. A
    retraction with no evidence and no replacement yields uncertainty, exactly
    like a withdrawn replacement one layer up — never the opposite claim, and
    never the older value the source used to carry.

    A source that is merely unmarked or current supports what cites it,
    exactly as it did before this module existed.
    """
    if _falsified_by(view.meta):
        return SUPPORT_DISPROVEN
    if view.state == "retired":
        return SUPPORT_WITHDRAWN
    return None


# ── findings ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SupportFinding:
    """One reason a record's support is in question, and where it was found."""

    ref: str
    #: Hops from the record: 1 = its own declared source, 2 = that source's.
    hop: int
    #: A member of :data:`SUPPORT_REASONS`.
    reason: str
    #: The ref this source was reached from (the record itself at hop 1).
    via: str
    #: The lifecycle signal behind the reason (``status:archived``,
    #: ``path:archive``, ``expired``, …). Diagnostics; never a record's text.
    detail: str = ""

    def qualifier(self) -> str:
        """The ``stale_backing:<ref>@<hop>:<reason>`` qualifier string.

        Extends the existing ``stale_backing:<ref>`` spelling the resolution
        layer already emits for a persisted flag, so a reader that matched on
        the prefix keeps matching and one that wants the hop and the reason
        can have them without a new field.
        """
        return f"stale_backing:{self.ref}@{self.hop}:{self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "hop": self.hop, "reason": self.reason,
            "via": self.via, "detail": self.detail,
        }


@dataclass(frozen=True)
class SupportCheck:
    """What :func:`check_support` found for one record."""

    ref: str
    policy: str
    status: str
    findings: tuple[SupportFinding, ...] = ()
    #: Coverage reasons: what the walk could not see (a closed vocabulary —
    #: :data:`BUDGET_SUPPORT_HOPS`, :data:`TARGET_MISSING`, :data:`TARGET_HIDDEN`).
    reasons: tuple[str, ...] = ()
    #: ``(ref, revision)`` for the record and every source the walk read —
    #: the revision half of the reuse test.
    inputs: tuple[tuple[str, str | None], ...] = ()
    #: Known temporal boundaries among those records, as ISO-8601 strings.
    transitions: tuple[str, ...] = ()
    #: Declared backing refs, normalized, in declaration order.
    declared: tuple[str, ...] = ()

    def qualifiers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.qualifier() for f in self.findings))

    def refs_for(self, reasons: frozenset[str], *, hop: int | None = 1) -> frozenset[str]:
        """Refs flagged for any of ``reasons`` (at ``hop``, or any hop if ``None``)."""
        return frozenset(
            f.ref for f in self.findings
            if f.reason in reasons and (hop is None or f.hop == hop)
        )

    def decisive_refs(self) -> frozenset[str]:
        """Direct sources whose support is withdrawn or disproven."""
        return self.refs_for(DECISIVE_REASONS)

    def disproven_refs(self) -> frozenset[str]:
        return self.refs_for(frozenset({SUPPORT_DISPROVEN}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "policy": self.policy,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "coverage": {
                "status": "partial" if self.reasons else "complete",
                "reasons": list(self.reasons),
            },
        }


def _status(policy: str, declared: Sequence[str], flagged: frozenset[str]) -> str:
    """What the checker may say, given the declared policy (see the module docstring)."""
    if not declared:
        return STATUS_NO_BACKING
    if policy == POLICY_ALL_OF:
        return STATUS_FULLY_SUPPORTED if not flagged else STATUS_UNSUPPORTED
    if policy == POLICY_ANY_OF:
        return STATUS_UNSUPPORTED if set(declared) <= flagged else STATUS_STILL_SUPPORTED
    # Legacy list: report per source, conclude nothing.
    return STATUS_ADVISORY


def check_support(
    ref: str,
    meta: Mapping[str, Any] | None,
    *,
    read: Reader,
    now: datetime | None = None,
    revision: str | None = None,
    max_hops: int = DEFAULT_MAX_HOPS,
    max_reads: int | None = None,
) -> SupportCheck:
    """Walk this record's backing graph and report what no longer holds. Pure.

    Breadth-first from the record's own ``backed_by`` refs, one read per
    distinct source (``visited`` makes a cycle terminate and gives every
    source its shortest hop), stopping at ``max_hops`` hops and ``max_reads``
    reads. A source that is itself withdrawn or disproven is not walked past:
    its own backing cannot make the dependent any more or less supported than
    the withdrawal already did.

    Absence is never a retirement. A ref no file answers to contributes
    :data:`TARGET_MISSING` coverage and no finding — a partial store, or a ref
    pointing at something outside memory, must not read as "the source was
    withdrawn".
    """
    fm = dict(meta) if isinstance(meta, Mapping) else {}
    self_ref = normalize_ref(ref)
    declared = tuple(dict.fromkeys(normalize_ref(r) for r in parse_link_refs(fm, "backed_by") if r))
    policy = backing_policy(fm)
    inputs: dict[str, str | None] = {self_ref: revision}
    transitions: list[datetime] = list(_transitions(fm))
    if not declared:
        return SupportCheck(
            ref=self_ref, policy=policy, status=STATUS_NO_BACKING,
            inputs=tuple(sorted(inputs.items())),
            transitions=tuple(sorted(t.isoformat() for t in transitions)),
        )

    receipts = revalidated_revisions(fm)
    findings: list[SupportFinding] = []
    reasons: set[str] = set()
    visited: set[str] = {self_ref}
    reads = 0
    frontier: list[tuple[str, int, str]] = [(r, 1, self_ref) for r in declared]

    while frontier:
        source_ref, hop, via = frontier.pop(0)
        if source_ref in visited:
            continue
        visited.add(source_ref)
        if max_reads is not None and reads >= max_reads:
            reasons.add(BUDGET_SUPPORT_HOPS)
            break
        view = read(source_ref)
        reads += 1
        if view is None:
            reasons.add(TARGET_MISSING)
            continue
        if view.hidden:
            reasons.add(TARGET_HIDDEN)
            continue
        inputs[source_ref] = view.revision
        transitions.extend(_transitions(view.meta))

        reason = support_reason(view)
        if reason is not None:
            findings.append(SupportFinding(
                ref=source_ref, hop=hop, reason=reason, via=via,
                detail=view.state_reason if reason == SUPPORT_WITHDRAWN else "falsified",
            ))
            continue

        # The revision half: only a source the record recorded a revalidation
        # for can be *changed since* anything. No receipt, no claim.
        recorded = receipts.get(source_ref)
        if recorded and view.revision and recorded != view.revision:
            findings.append(SupportFinding(
                ref=source_ref, hop=hop, reason=SUPPORT_REVISION_CHANGED, via=via,
                detail=f"revalidated at {recorded[:12]}",
            ))

        onward = [normalize_ref(r) for r in parse_link_refs(view.meta, "backed_by")]
        pending = [r for r in onward if r and r not in visited]
        if hop >= max_hops:
            if pending:
                reasons.add(BUDGET_SUPPORT_HOPS)
            continue
        frontier.extend((r, hop + 1, source_ref) for r in pending)

    if frontier:  # broken out of on the read budget
        reasons.add(BUDGET_SUPPORT_HOPS)

    flagged = frozenset(
        f.ref for f in findings if f.hop == 1 and f.reason in DECISIVE_REASONS
    )
    return SupportCheck(
        ref=self_ref,
        policy=policy,
        status=_status(policy, declared, flagged),
        findings=tuple(findings),
        reasons=tuple(sorted(reasons)),
        inputs=tuple(sorted(inputs.items())),
        transitions=tuple(sorted({t.isoformat() for t in transitions})),
        declared=declared,
    )


# ── the per-request cache ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CacheEntry:
    check: SupportCheck
    scope: tuple[str, ...]
    policy_version: str
    window: tuple[str | None, str | None]
    key: str


class SupportCache:
    """Support checks computed in one request, reusable only under the contract.

    The five conditions of :ref:`the delivery-receipt reuse contract
    <reuse-contract>`, applied to one record's check. Scope and policy version
    are matched directly; the query scope is the record's own ref (a support
    check is not a query); every input ``(ref, revision)`` is re-read through
    the caller's reader and must still match; and the temporal window
    bracketing ``now`` — derived from the transitions the walk actually saw —
    must be the same one. An input whose revision is ``unknown`` can never be
    shown to still match, so an entry carrying one is never reused.

    Per request. Nothing here persists between requests, and a cross-request
    cache would owe the same checks *at delivery* rather than at the key:
    caller access and applicable time both move without any file changing.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _policy(policy_version: PolicyVersion | str | None) -> str:
        if policy_version is None:
            return PolicyVersion.current().as_str()
        return (
            policy_version.as_str()
            if isinstance(policy_version, PolicyVersion)
            else str(policy_version)
        )

    @staticmethod
    def _window_of(check: SupportCheck, now: datetime) -> tuple[str | None, str | None]:
        moments = [m for m in (parse_moment(t) for t in check.transitions) if m is not None]
        return _window(moments, now)

    def key_for(
        self,
        check: SupportCheck,
        *,
        now: datetime,
        scope: Sequence[str] | None,
        policy_version: PolicyVersion | str | None,
    ) -> str:
        """The reuse key for a computed check — the five contract inputs, derived once."""
        return reuse_key(
            scope=scope,
            query_scope={"ref": check.ref, "policy": check.policy},
            policy_version=self._policy(policy_version),
            revisions=check.inputs,
            window=self._window_of(check, now),
        )

    def get(
        self,
        ref: str,
        *,
        read: Reader,
        now: datetime,
        scope: Sequence[str] | None = (),
        policy_version: PolicyVersion | str | None = None,
    ) -> SupportCheck | None:
        """A warm check for ``ref``, or ``None`` when any reuse condition fails."""
        entry = self._entries.get(normalize_ref(ref))
        if entry is None:
            self.misses += 1
            return None
        if entry.scope != tuple(scope or ()) or entry.policy_version != self._policy(policy_version):
            self.misses += 1
            return None
        for input_ref, revision in entry.check.inputs:
            if input_ref == entry.check.ref:
                continue  # the record itself is re-read by the caller, not here
            if revision is None:
                self.misses += 1  # an unknown revision can never be shown to match
                return None
            view = read(input_ref)
            if view is None or view.hidden or view.revision != revision:
                self.misses += 1
                return None
        if self._window_of(entry.check, now) != entry.window:
            self.misses += 1
            return None
        self.hits += 1
        return entry.check

    def put(
        self,
        check: SupportCheck,
        *,
        now: datetime,
        scope: Sequence[str] | None = (),
        policy_version: PolicyVersion | str | None = None,
    ) -> str:
        """Store ``check`` and return the reuse key it was stored under."""
        key = self.key_for(check, now=now, scope=scope, policy_version=policy_version)
        self._entries[check.ref] = _CacheEntry(
            check=check,
            scope=tuple(scope or ()),
            policy_version=self._policy(policy_version),
            window=self._window_of(check, now),
            key=key,
        )
        return key


def resolve_support(
    ref: str,
    meta: Mapping[str, Any] | None,
    *,
    read: Reader,
    now: datetime,
    revision: str | None = None,
    max_hops: int = DEFAULT_MAX_HOPS,
    max_reads: int | None = None,
    cache: SupportCache | None = None,
    scope: Sequence[str] | None = (),
    policy_version: PolicyVersion | str | None = None,
) -> SupportCheck:
    """:func:`check_support` through ``cache``, under the reuse contract."""
    if cache is not None:
        warm = cache.get(ref, read=read, now=now, scope=scope, policy_version=policy_version)
        if warm is not None:
            return warm
    check = check_support(
        ref, meta, read=read, now=now, revision=revision,
        max_hops=max_hops, max_reads=max_reads,
    )
    if cache is not None:
        cache.put(check, now=now, scope=scope, policy_version=policy_version)
    return check


# ── a reader over the memory dir ─────────────────────────────────────────────


def _resolve_source(base: str, ref: str) -> str | None:
    """``<base>/<ref>.md``, falling back to the ``-status`` layer; ``None`` if outside."""
    for candidate in (f"{ref}.md", f"{ref}-status.md"):
        real = os.path.realpath(os.path.join(base, candidate))
        try:
            if os.path.commonpath([base, real]) != base:
                return None
        except ValueError:
            return None
        if os.path.isfile(real):
            return real
    return None


def disk_reader(base_dir: str | None = None, *, now: datetime | None = None) -> Reader:
    """A :data:`Reader` over the memory dir, reading live frontmatter and bytes.

    For the maintenance paths (lint, the marker applier) that have no request
    and no scope chain. Delivery reads through the evidence layer's reader
    instead, which applies the requester's visibility gate.
    """
    import hashlib

    from palinode.core.config import config

    base = os.path.realpath(base_dir or config.memory_dir)

    def _read(ref: str) -> SourceView | None:
        import frontmatter as _frontmatter

        path = _resolve_source(base, normalize_ref(ref))
        if path is None:
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            meta = _frontmatter.loads(raw).metadata
        except Exception:  # noqa: BLE001 — soft-fail read, like lint
            return None
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        return SourceView.of(
            ref, meta if isinstance(meta, dict) else {},
            revision=hashlib.sha256(raw.encode()).hexdigest(), path=rel, now=now,
        )

    return _read


def mapping_reader(
    records: Mapping[str, tuple[Mapping[str, Any], str | None, str]],
    *,
    now: datetime | None = None,
) -> Reader:
    """A :data:`Reader` over an in-memory ``{ref: (meta, revision, rel_path)}`` map.

    For a caller that already read every file it is about to check — the lint
    pass scans the whole store once, and re-reading each source through
    :func:`disk_reader` would double its I/O for no new information.
    """

    def _read(ref: str) -> SourceView | None:
        entry = records.get(normalize_ref(ref))
        if entry is None:
            return None
        meta, revision, rel = entry
        return SourceView.of(ref, meta, revision=revision, path=rel, now=now)

    return _read


# ── persistent state: entries, clearing, the marker path ─────────────────────


def build_stale_entry(finding: SupportFinding, *, at: datetime | None = None) -> dict[str, Any]:
    """The ``stale_backing`` entry this module persists for one finding.

    The same shape the retirement path writes (``ref`` / ``op`` / ``at`` /
    ``reason``) so every existing reader — lint, the review pass, the quality
    UI, the MCP renderer — keeps working unchanged, with ``op`` naming the
    check that produced it and ``hop`` recording how far away the cause was.
    """
    moment = at or datetime.now(UTC)
    entry: dict[str, Any] = {
        "ref": finding.ref,
        "op": REVALIDATE_CHECK_OP,
        "at": moment.isoformat(timespec="seconds"),
        "hop": finding.hop,
        "reason": finding.reason if not finding.detail else f"{finding.reason} ({finding.detail})",
    }
    if finding.via and finding.via != finding.ref:
        entry["via"] = finding.via
    return entry


def _parse_stale(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    from palinode.consolidation.propagate import parse_stale_backing

    return parse_stale_backing(dict(meta))


def new_stale_entries(
    meta: Mapping[str, Any], check: SupportCheck, *, at: datetime | None = None
) -> list[dict[str, Any]]:
    """Entries for findings the record does not already carry a flag for.

    Idempotent on ``ref``, exactly as the retirement path is: a source already
    flagged — by any op — is not flagged twice, so re-running a check is a
    no-op and a second hop that resolves to an already-flagged source adds
    nothing.
    """
    known = {normalize_ref(str(e["ref"])) for e in _parse_stale(meta)}
    out: list[dict[str, Any]] = []
    for finding in check.findings:
        if finding.ref in known:
            continue
        known.add(finding.ref)
        out.append(build_stale_entry(finding, at=at))
    return out


def cleared_refs(meta: Mapping[str, Any], check: SupportCheck, *, read: Reader) -> list[str]:
    """Flagged refs an explicit, revision-matched revalidation has cleared.

    One removal rule, and it is the point of the whole module: a
    ``stale_backing`` entry is removed only when the source it names is
    standing again **and** the record carries a ``revalidated`` receipt whose
    revision equals that source's current whole-file SHA-256. A re-save of the
    dependent is not that receipt; a formatting-only edit is certainly not.
    """
    receipts = revalidated_revisions(meta)
    if not receipts:
        return []
    live = {f.ref for f in check.findings}
    out: list[str] = []
    for entry in _parse_stale(meta):
        ref = normalize_ref(str(entry["ref"]))
        if ref in live or ref not in receipts:
            continue
        view = read(ref)
        if view is None or view.hidden or view.revision is None:
            continue
        if support_reason(view) is not None:
            continue  # still retired: no receipt revives a withdrawn source
        if receipts[ref] == view.revision:
            out.append(ref)
    return out


def remove_stale_backing_from_content(content: str, refs: Sequence[str]) -> str:
    """Return ``content`` with the ``stale_backing`` entries for ``refs`` dropped.

    Returns ``content`` unchanged when nothing matches, so a caller can skip a
    no-op write. Removing the last entry removes the field rather than leaving
    an empty list behind. Body preserved verbatim.
    """
    import frontmatter as _frontmatter
    import yaml

    drop = {normalize_ref(r) for r in refs}
    if not drop:
        return content
    post = _frontmatter.loads(content)
    existing = _parse_stale(post.metadata)
    kept = [e for e in existing if normalize_ref(str(e["ref"])) not in drop]
    if len(kept) == len(existing):
        return content

    meta = dict(post.metadata)
    if kept:
        meta[STALE_BACKING_FIELD] = kept
    else:
        meta.pop(STALE_BACKING_FIELD, None)
    dumped = yaml.safe_dump(
        meta, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return f"---\n{dumped}---\n\n{post.content}\n"


def enqueue_revalidation(
    file_path: str, check: SupportCheck, *, cleared: Sequence[str] = ()
) -> str | None:
    """Write a ``revalidate`` marker for ``file_path``; return the marker path.

    The existing write-time marker queue, with an item the deterministic
    applier understands instead of a contradiction-check item. ``None`` when
    there is nothing to record. The findings travel with the marker for
    operator forensics only: :func:`apply_revalidation` re-checks against live
    disk, because a marker can outlive the state that produced it.
    """
    from palinode.consolidation import write_time

    if not check.findings and not cleared:
        return None
    item = {
        "kind": MARKER_KIND,
        "ref": check.ref,
        "findings": [f.to_dict() for f in check.findings],
        "cleared": [normalize_ref(r) for r in cleared],
    }
    return write_time._write_marker(file_path, item)


def apply_revalidation(
    file_path: str,
    item: Mapping[str, Any] | None = None,
    *,
    base_dir: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, int]:
    """Record a record's current support state in its frontmatter. Deterministic.

    Re-checks ``file_path`` against live disk — the marker's own findings are
    forensics, not the decision — then appends one ``stale_backing`` entry per
    unflagged finding, removes the entries an explicit revalidation receipt
    cleared, re-indexes and commits through ``git_tools``. **No prose is
    touched**: the body, the status and every other frontmatter field are
    preserved, and no replacement value is ever derived from a source's
    change.

    Archived records are skipped: a ``status: archived`` memory asserts
    nothing, so there is nothing to qualify — the same rule the propagation
    path applies, and the reason the check runs again on restore.

    Returns ``{"flagged": n, "cleared": n, "skipped": n}``. Raises nothing it
    can help; an unreadable or unwritable target is reported as ``skipped``
    and logged, so one bad file cannot fail a sweep.
    """
    import frontmatter as _frontmatter

    from palinode.core import git_tools
    from palinode.core.config import config

    stats = {"flagged": 0, "cleared": 0, "skipped": 0}
    base = os.path.realpath(base_dir or config.memory_dir)
    clock = now or datetime.now(UTC)
    try:
        with open(file_path, encoding="utf-8") as fh:
            content = fh.read()
        post = _frontmatter.loads(content)
        meta = post.metadata if isinstance(post.metadata, dict) else {}
    except Exception as exc:  # noqa: BLE001 — soft-fail read, like lint
        logger.warning("revalidation: could not read %s: %s", file_path, exc)
        stats["skipped"] = 1
        return stats

    if meta.get("status") == "archived":
        logger.debug("revalidation: %s is archived; nothing to qualify", file_path)
        stats["skipped"] = 1
        return stats

    rel = os.path.relpath(os.path.realpath(file_path), base).replace(os.sep, "/")
    read = disk_reader(base, now=clock)
    check = check_support(
        normalize_ref(rel), meta, read=read, now=clock,
        revision=_file_revision(file_path),
    )
    entries = new_stale_entries(meta, check, at=clock)
    drop = cleared_refs(meta, check, read=read)

    updated = content
    if drop:
        updated = remove_stale_backing_from_content(updated, drop)
    if entries:
        from palinode.consolidation.propagate import merge_stale_backing_into_content

        for entry in entries:
            updated = merge_stale_backing_into_content(updated, entry)
    if updated == content:
        return stats

    try:
        git_tools.write_memory_file(file_path, updated)
    except Exception as exc:  # noqa: BLE001 — never fail a sweep on one file
        logger.warning("revalidation: could not write %s: %s", file_path, exc)
        stats["skipped"] = 1
        return stats

    stats["flagged"] = len(entries)
    stats["cleared"] = len(drop)
    _reindex(file_path)
    if commit:
        git_tools.commit_memory_files(
            [file_path],
            f"{config.git.commit_prefix} backing revalidation: {normalize_ref(rel)} "
            f"+{len(entries)} flagged, -{len(drop)} cleared",
        )
    logger.info(
        "revalidation: %s flagged=%d cleared=%d status=%s",
        normalize_ref(rel), stats["flagged"], stats["cleared"], check.status,
    )
    return stats


def _file_revision(path: str) -> str | None:
    import hashlib

    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _reindex(path: str) -> None:
    """Re-index a frontmatter-only change (no re-embed). Best-effort."""
    try:
        from palinode.indexer.index_file import index_file

        outcome = index_file(path)
        if outcome.get("error"):
            logger.warning("revalidation: reindex reported %s for %s", outcome["error"], path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "revalidation: reindex failed for %s: %s — the flag is on disk but not "
            "in the index until the file is next indexed", path, exc,
        )


def sweep_revalidations(
    base_dir: str | None = None, *, now: datetime | None = None, enqueue: bool = True
) -> list[str]:
    """Check every live record that declares backing; enqueue the ones needing a write.

    A frontmatter scan in sorted path order (``backed_by`` lives only in
    frontmatter — there is no links table to query), skipping the same
    never-a-dependent directories the propagation path skips and every
    ``status: archived`` record. Returns the memory-relative paths enqueued.

    Enqueues rather than writes: the marker queue is where persistent work
    already goes, so a revalidation lands through the same sweep, provenance
    and commit path as every other deferred write, and a failure is visible in
    the same place.
    """
    from palinode.core.config import config
    from palinode.core.skip_dirs import SKIP_FOR_LISTING, is_skipped_path

    import frontmatter as _frontmatter

    base = os.path.realpath(base_dir or config.memory_dir)
    clock = now or datetime.now(UTC)
    read = disk_reader(base, now=clock)
    out: list[str] = []
    for path in sorted(glob.glob(os.path.join(base, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        if is_skipped_path(rel, SKIP_FOR_LISTING) or rel.endswith("-history.md"):
            continue
        try:
            meta = _frontmatter.load(path).metadata
        except Exception:  # noqa: BLE001 — soft-fail read, like lint
            continue
        if not isinstance(meta, dict) or meta.get("status") == "archived":
            continue
        if not parse_link_refs(meta, "backed_by"):
            continue
        check = check_support(
            normalize_ref(rel), meta, read=read, now=clock, revision=_file_revision(path)
        )
        entries = new_stale_entries(meta, check, at=clock)
        drop = cleared_refs(meta, check, read=read)
        if not entries and not drop:
            continue
        out.append(rel)
        if enqueue:
            enqueue_revalidation(path, check, cleared=drop)
    if out:
        logger.info("revalidation sweep: %d record(s) enqueued: %s", len(out), ", ".join(out))
    return out


__all__ = [
    "BACKING_POLICIES",
    "BACKING_POLICY_FIELD",
    "BUDGET_SUPPORT_HOPS",
    "DECISIVE_REASONS",
    "DEFAULT_MAX_HOPS",
    "MARKER_KIND",
    "POLICY_ADVISORY",
    "POLICY_ALL_OF",
    "POLICY_ANY_OF",
    "REVALIDATED_FIELD",
    "REVALIDATE_CHECK_OP",
    "STATUS_ADVISORY",
    "STATUS_FULLY_SUPPORTED",
    "STATUS_NO_BACKING",
    "STATUS_STILL_SUPPORTED",
    "STATUS_UNSUPPORTED",
    "SUPPORT_DISPROVEN",
    "SUPPORT_REASONS",
    "SUPPORT_REVISION_CHANGED",
    "SUPPORT_STATUSES",
    "SUPPORT_WITHDRAWN",
    "SupportCache",
    "SupportCheck",
    "SupportFinding",
    "SourceView",
    "apply_revalidation",
    "backing_policy",
    "build_revalidation",
    "build_stale_entry",
    "check_support",
    "cleared_refs",
    "disk_reader",
    "enqueue_revalidation",
    "mapping_reader",
    "merge_revalidation_into_content",
    "new_stale_entries",
    "normalize_ref",
    "parse_revalidations",
    "remove_stale_backing_from_content",
    "resolve_support",
    "revalidated_revisions",
    "support_reason",
    "sweep_revalidations",
]
