"""
Retrieval Event Logger — Issue the retrieval-event instrumentation

Append-only JSONL log of every search delivery, including empty ones, and
every memory-file read, distinguishing explicit tool calls from passive
auto-injection. Pure observability; no ranker behavior change.

A search that delivers nothing writes one call-level row with an empty
``file_path`` and ``disposition`` ``"none_delivered"``, so "searched and found
nothing" is distinguishable from "never searched". Readers that key on
``file_path`` skip it.

Log path: <memory_dir>/.audit/retrievals.jsonl
(parallel to the existing mcp-calls.jsonl audit log)

Retrieval events and receipt-only envelopes each occupy one JSON line.
Resolve envelopes carry event_type=bundle_receipt and no top-level file_path;
they explain analysis deliveries without inflating retrieval statistics.

Delivery receipts ride this same log rather than a parallel ledger: when a
delivery supplies a receipt (:mod:`palinode.core.receipt`), each row it writes
additionally carries that delivery's ``bundle_id``, ``policy_version``,
resolved ``scope``, the record's exact source ``revision``, its
``disposition``, its lineage group, the delivery's bounded ``coverage`` and its
next known temporal transition. Rows from one delivery join on ``bundle_id``.

Only refs, hashes and dispositions are added — never memory content. The
receipt's public view is what is written; its diagnostics view (which carries
the caller's query prose) is not, beyond the ``query`` field this log has
always had.

Schema evolution is additive and needs no migration: every receipt field is an
optional dataclass field defaulting to ``None``, so a line written before they
existed reads back exactly as it did, and a reader that does not know them is
unaffected. Re-running against an existing log is therefore idempotent — the
file is append-only and no past line is rewritten.

Disable globally: set PALINODE_INSTRUMENTATION_DISABLED=1 in env,
or set instrumentation.capture_retrievals: false in palinode.config.yaml.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from palinode.core.bundle import Bundle, BundleRequest
    from palinode.core.receipt import Receipt

logger = logging.getLogger("palinode.retrieval_log")

_LOG_FILENAME = "retrievals.jsonl"

# Disposition of the single call-level row an empty search delivery writes.
NONE_DELIVERED = "none_delivered"
BUNDLE_RECEIPT = "bundle_receipt"
MAX_RECEIPT_BYTES = 256 * 1024

#: Values of ``PALINODE_INSTRUMENTATION_DISABLED`` that switch capture off.
_DISABLED_VALUES = ("1", "true", "yes")


def instrumentation_disabled_by_env() -> bool:
    """Is the runtime kill-switch set?

    Exposed as a function because a reader of the log has to answer the same
    question a writer does — "was anything being written at that time?" — and a
    second hand-copy of the precedence rule is exactly how the two would come to
    disagree. :class:`RetrievalLogger` and
    :func:`palinode.core.explain.explain_delivery` both call this.
    """
    return os.environ.get("PALINODE_INSTRUMENTATION_DISABLED", "").strip() in _DISABLED_VALUES


def _ref_of(path: str, memory_dir: str | None = None) -> str:
    """Reduce any spelling of a memory path to the ref a receipt keys on.

    Mirrors ``trace._canonical_ref``: absolute paths are made relative to the
    memory dir when one is known, the result is normalised, and ``.md`` is
    dropped. Local rather than imported so this module keeps its narrow
    dependency surface (it is imported at API start-up).
    """
    p = str(path or "")
    if memory_dir and os.path.isabs(p):
        try:
            p = os.path.relpath(p, memory_dir)
        except ValueError:
            pass
    p = os.path.normpath(p) if p else ""
    return p[:-3] if p.endswith(".md") else p


# ── Event schema ──────────────────────────────────────────────────────────────


@dataclass
class RetrievalEvent:
    """One retrieval of a memory file or chunk."""

    timestamp: str          # ISO-8601 UTC
    file_path: str          # relative to memory_dir (or absolute — logged as-is)
    chunk_id: str | None    # chunk PK if chunk-level (search returns chunks)
    mode: Literal["explicit", "passive"]  # explicit = tool call; passive = auto-inject / scope-chain
    source: str             # e.g. "palinode_search", "palinode_read", "auto_inject"
    query: str | None       # search query if applicable
    rank: int | None        # 0-based rank in result list
    score: float | None     # RRF / cosine score
    session_id: str | None  # MCP/HTTP session identifier when available

    # ── delivery receipt (palinode.core.receipt) ─────────────────────────
    # All optional and defaulted: a row written without a receipt is exactly
    # the row this log wrote before receipts existed.
    bundle_id: str | None = None        # correlation key for one delivery
    policy_version: str | None = None   # package + projection + config policy
    scope: list[str] | None = None      # caller scope as the SERVER resolved it
    revision: str | None = None         # exact source revision of this record
    revision_basis: str | None = None   # which hash domain `revision` is in
    disposition: str | None = None      # selected / replaced / conflict_side / …
    lineage_group: str | None = None    # origin anchor; None = lineage unknown
    coverage: dict[str, Any] | None = None    # delivery-level bounded coverage
    next_transition: str | None = None  # nearest known transition ahead
    #: The delivery's confidence verdict (palinode.core.confidence):
    #: confident / weak / none, or None for a mode that ranked nothing
    #: against a query. Written on the empty-slate row too — a delivery that
    #: returned nothing is exactly where a later reader wants to know
    #: whether the store had no answer or the caller had no query.
    confidence: str | None = None


# ── Logger class ──────────────────────────────────────────────────────────────


class RetrievalLogger:
    """Append-only JSONL logger for retrieval events.

    Writes are best-effort: any I/O error is logged at WARNING level and
    swallowed so retrieval performance is never affected.

    Disable flag precedence:
      1. PALINODE_INSTRUMENTATION_DISABLED=1 env var (runtime kill-switch)
      2. instrumentation.capture_retrievals config key (default True)
    """

    def __init__(self, memory_dir: str, *, enabled: bool = True) -> None:
        self._memory_dir = memory_dir
        # Env-var kill-switch overrides config
        if instrumentation_disabled_by_env():
            self._enabled = False
            self._path: Path | None = None
            return

        self._enabled = enabled
        if not self._enabled:
            self._path = None
            return

        self._path = Path(memory_dir) / ".audit" / _LOG_FILENAME
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create retrieval-log directory %s: %s", self._path.parent, exc)
            self._enabled = False
            self._path = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def log_path(self) -> Path | None:
        return self._path

    def record(self, event: RetrievalEvent) -> None:
        """Append *event* to the JSONL log.  Never raises."""
        if not self._enabled or self._path is None:
            return
        entry = asdict(event)
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
        except OSError as exc:
            logger.warning("Retrieval-log write failed: %s", exc)

    def record_bundle_receipt(
        self, bundle: "Bundle", request: "BundleRequest", *,
        cwd: str | None = None, project: str | None = None,
    ) -> None:
        """Persist an analysis receipt, never a retrieval or recall event.

        One bounded append after rendering; no query, session, titles or body
        text. The absent top-level file_path keeps this out of file-recall
        history. It records a recall that already happened, so the recall
        controls govern it: the recall pause and automatic exclusions apply even
        when resolution was explicitly requested. The capture pause does not —
        the receipt carries no memory content, like a search row, and a paused
        capture must not leave a delivered receipt id unexplainable.
        """
        from palinode.core.capture_policy import evaluate_capture_policy
        from palinode.core.config import config

        if (not self._enabled or self._path is None or instrumentation_disabled_by_env()
                or not config.instrumentation.capture_retrievals or not bundle.receipt):
            return
        try:
            if any(not evaluate_capture_policy(
                action, automatic=True, cwd=cwd, project=project,
            ).allowed for action in ("recall",)):
                return
            details: dict[str, dict[str, Any]] = {}
            for section, assertions in (
                ("selected", [item.assertion for item in bundle.selected]),
                ("replaced", [item.assertion for item in bundle.replaced]),
                ("conflict", [side for group in bundle.conflicts for side in group.sides]),
            ):
                for assertion in assertions:
                    details[assertion.ref] = {
                        "role": section, "qualifiers": list(assertion.qualifiers),
                    }
            for item in bundle.selected:
                details[item.assertion.ref]["reasons"] = list(item.reasons)
                for discovered in item.discovered:
                    details.setdefault(discovered.ref, {"role": "unlinked_discovery"})
            for item in bundle.insufficient:
                details.setdefault(item.ref, {"role": "insufficient", "qualifiers": list(item.reasons)})
            for refs in bundle.omitted_conflict_refs:
                for ref in refs:
                    details.setdefault(ref, {"role": "omitted_conflict"})
            receipt = dict(bundle.receipt)
            receipt["supplied"] = [
                {**record, **details.get(record["ref"], {"role": "evidence_reference"})}
                for record in receipt["supplied"]
            ]
            entry = {
                "event_type": BUNDLE_RECEIPT,
                "bundle_id": bundle.receipt_ref,
                "timestamp": receipt["evaluated_at"],
                "receipt": receipt,
                "budget": bundle.budget,
                "selection_path": [name for name, value in (
                    ("query", request.query), ("ref", request.ref), ("context", request.context),
                ) if value] + ["full_evidence", "resolution", "budget_packing"],
            }
            encoded = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
            if len(encoded) > MAX_RECEIPT_BYTES:
                logger.warning("Bundle receipt exceeds audit size limit")
                return
            with open(self._path, "ab") as fh:
                fh.write(encoded)
        except Exception:
            # Audit failure must never change the already-rendered delivery.
            logger.warning("Bundle receipt write failed", exc_info=True)

    def record_search_results(
        self,
        results: list[dict],
        *,
        query: str | None,
        source: str,
        mode: Literal["explicit", "passive"],
        session_id: str | None = None,
        receipt: "Receipt | None" = None,
        confidence: str | None = None,
    ) -> None:
        """Emit one RetrievalEvent per result in *results*.

        Called after ``store.search_hybrid`` / ``store.search`` returns so we
        capture the actual file paths surfaced to the caller.

        When the delivery built a ``receipt``, each row additionally carries
        the delivery-level fields (bundle, policy, scope, coverage, next
        transition) and this record's own revision, disposition and lineage
        group. The log keeps the hit set it always had: records supplied only
        as *evidence* around a hit ride the receipt in the response, not this
        log, so recall statistics keep counting the same events they did.

        An empty *results* writes exactly one call-level row: the query, source,
        session and the receipt's delivery-level fields, with no memory ref.

        ``confidence`` is the delivery's verdict, repeated on every row of the
        delivery like the other delivery-level fields, so recall statistics can
        ask what share of deliveries had nothing confident in them without
        replaying any search.
        """
        if not self._enabled:
            return
        ts = receipt.evaluated_at if receipt else datetime.now(timezone.utc).isoformat()
        if not results:
            self.record(RetrievalEvent(
                timestamp=ts,
                file_path="",
                chunk_id=None,
                mode=mode,
                source=source,
                query=query,
                rank=None,
                score=None,
                session_id=session_id,
                bundle_id=receipt.bundle_id if receipt else None,
                policy_version=receipt.policy_version.as_str() if receipt else None,
                scope=list(receipt.scope) if receipt else None,
                disposition=NONE_DELIVERED,
                coverage=dict(receipt.coverage) if receipt else None,
                next_transition=receipt.next_transition if receipt else None,
                confidence=confidence,
            ))
            return
        supplied = {s.ref: s for s in receipt.supplied} if receipt else {}
        for rank, r in enumerate(results):
            record = supplied.get(_ref_of(
                r.get("rel_path") or r.get("file_path") or "", self._memory_dir
            ))
            self.record(RetrievalEvent(
                timestamp=ts,
                file_path=r.get("file_path", ""),
                chunk_id=r.get("section_id"),
                mode=mode,
                source=source,
                query=query,
                rank=rank,
                score=r.get("score"),
                session_id=session_id,
                bundle_id=receipt.bundle_id if receipt else None,
                policy_version=receipt.policy_version.as_str() if receipt else None,
                scope=list(receipt.scope) if receipt else None,
                revision=record.revision if record else None,
                revision_basis=record.revision_basis if record else None,
                disposition=record.disposition if record else None,
                lineage_group=record.origin if record else None,
                coverage=dict(receipt.coverage) if receipt else None,
                next_transition=receipt.next_transition if receipt else None,
                confidence=confidence,
            ))

    def record_file_read(
        self,
        file_path: str,
        *,
        source: str,
        mode: Literal["explicit", "passive"],
        session_id: str | None = None,
    ) -> None:
        """Emit a single RetrievalEvent for a whole-file read."""
        if not self._enabled:
            return
        self.record(RetrievalEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            file_path=file_path,
            chunk_id=None,
            mode=mode,
            source=source,
            query=None,
            rank=None,
            score=None,
            session_id=session_id,
        ))
