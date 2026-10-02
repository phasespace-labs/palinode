"""Explain one delivery — what an agent was handed, and what cannot be known.

A delivery receipt (:mod:`palinode.core.receipt`) is written, one row per
delivered record, onto the retrieval-event log
(:mod:`palinode.core.retrieval_log`, ``.audit/retrievals.jsonl``). Rows from
one delivery join on ``bundle_id``. This module reads those rows back and
composes the human answer to *"what context did this agent receive, and why was
this memory selected or qualified?"*.

It is **composition over existing storage**. No new table or file: everything
here is either a value the log recorded, a value derived from one (the ref,
the disposition counts), a comparison between
a recorded revision and the file *today* (``store.check_freshness``), or an
explicit :func:`unavailable` marker naming why the answer does not exist.
Resolve records a receipt-only envelope in the same log; it is expanded here
for explanation without becoming a file-retrieval event.

Three honesty rules are enforced in code, not just prose
-------------------------------------------------------

**Supplied is not used.** The ``supplied`` section says what was handed over.
Whether an agent then acted on any of it is the ``G3`` terminal consuming-action
edge, which is not built; ``acted_on`` renders it as ``not_captured`` and
carries no counts, no scores and no inference. Nothing in this module infers
influence from delivery.

**Missing is named, never invented and never dropped.** Every field the log did
not record, or that is no longer available, is an :func:`unavailable` marker
carrying a reason from :data:`UNAVAILABLE_REASONS` — so the difference between
"the log holds no rows for this id" (:data:`NO_MATCHING_ROWS`) and "this surface
never writes rows" (:data:`SURFACE_WRITES_NO_ROWS`) is a difference a reader can
see. ``/context/prime`` returns receipts without persisting them; the
:data:`NON_LOGGING_SURFACES` list is reported alongside the other candidate
causes rather than left for the reader to guess.

**The past is not reconstructed from the present.** The only thing read from
the store today is whether the *recorded* revision still matches the file — a
comparison, reported as ``source_state``. Titles, bodies, current freshness,
current currency and current scope are deliberately not read: a delivery is
explained by what was recorded at delivery time, or not at all.

Bounded and visibility-filtered
-------------------------------

Output is capped at :data:`DEFAULT_MAX_RECORDS` records with an explicit "N more
not shown" count, and the log scan is bounded at :data:`MAX_LINES_SCANNED`
lines. A record the caller may not see — private, restricted, or off the
caller's scope chain per :mod:`palinode.core.visibility` — is replaced by a
redacted count, never by its ref: explaining a delivery must not become a way to
read what the delivery itself would have withheld.

The log's ``query`` field is the caller's own prose. It follows the same rule
the receipt's two views already apply: the public view never carries it, and it
appears only when a caller explicitly asks for the diagnostics view
(``include_query``), which no agent-facing surface does.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from palinode.core.retrieval_log import BUNDLE_RECEIPT, NONE_DELIVERED, _ref_of

# ── status vocabulary ────────────────────────────────────────────────────────

#: Rows were found and describe records that were supplied.
EXPLAINED = "explained"
#: One call-level row was found saying the delivery supplied nothing. A search
#: happened; it delivered no record. Never rendered as "no record of a search".
NONE_DELIVERED_STATUS = "none_delivered"
#: No rows join on this id. Why is a separate question — see ``log.candidates``.
NOT_FOUND = "not_found"

# ── unavailable reasons (closed vocabulary) ──────────────────────────────────

#: The retrieval log is switched off for this store, so nothing was written at
#: delivery time. Checked live (``PALINODE_INSTRUMENTATION_DISABLED`` first,
#: then ``instrumentation.capture_retrievals``).
INSTRUMENTATION_DISABLED = "instrumentation_disabled"
#: The log file does not exist: never written, or rotated/deleted since.
LOG_ABSENT = "log_absent"
#: The log exists and holds rows, but none carry this ``bundle_id``.
NO_MATCHING_ROWS = "no_matching_rows"
#: The delivery came from a surface that writes no retrieval-log rows at all
#: (see :data:`NON_LOGGING_SURFACES`). Distinct from :data:`NO_MATCHING_ROWS`:
#: there is nothing to find rather than nothing found.
SURFACE_WRITES_NO_ROWS = "surface_writes_no_rows"
#: Rows written before delivery receipts existed carry no ``bundle_id``, so no
#: id can address them.
PREDATES_RECEIPTS = "predates_receipts"
#: The log has never recorded this field. Not a loss — it was never captured.
NOT_RECORDED = "not_recorded"
#: The recorded revision is in a hash domain the freshness check cannot compare
#: against the index (``file_sha256`` vs ``index_section_sha256``).
REVISION_BASIS_NOT_COMPARABLE = "revision_basis_not_comparable"
#: The caller's own query prose — diagnostics only, as on the receipt.
WITHHELD_DIAGNOSTICS_ONLY = "withheld_diagnostics_only"
#: The caller may not see this record, so its identity is not disclosed here.
WITHHELD_VISIBILITY = "withheld_visibility"

#: Nothing outside this set is ever emitted as a reason.
UNAVAILABLE_REASONS: tuple[str, ...] = (
    INSTRUMENTATION_DISABLED,
    LOG_ABSENT,
    NO_MATCHING_ROWS,
    SURFACE_WRITES_NO_ROWS,
    PREDATES_RECEIPTS,
    NOT_RECORDED,
    REVISION_BASIS_NOT_COMPARABLE,
    WITHHELD_DIAGNOSTICS_ONLY,
    WITHHELD_VISIBILITY,
)

#: Delivery surfaces that build a receipt and write **no** retrieval-log rows.
#: A bundle id minted by one of these can never be looked up here; it is only
#: available in the response that carried it. Stated as data so the explanation,
#: the renderer and the docs cannot drift.
NON_LOGGING_SURFACES: tuple[str, ...] = (
    "POST /context/prime — the session-start digest",
    "POST /search with an empty query — the recency branch",
)

#: What each recorded ``source`` means in words. An unrecognised value is
#: reported verbatim rather than glossed.
_SOURCE_LABELS: dict[str, str] = {
    "resolve": "bounded resolution (API, hook, CLI, MCP or plugin)",
    "palinode_search": "search, called as an MCP tool",
    "api_search": "search, called over the REST API",
    "cli_search": "search, called from the CLI",
    "palinode_read": "a whole-file read",
    "palinode_trace": "a trace composition",
    "palinode_blame": "a blame read",
    "palinode_history": "a history read",
    "auto_inject": "automatic injection",
}

#: What the recorded ``mode`` means. This is the only demand signal the log has.
_MODE_LABELS: dict[str, str] = {
    "explicit": "the caller asked for this (an explicit call)",
    "passive": "offered by an ambient or automatic path, not asked for",
}

#: Records shown before the output is capped. Everything beyond is counted, not
#: rendered.
DEFAULT_MAX_RECORDS = 20
#: Hard ceiling on the caller-supplied record cap.
MAX_RECORDS_CEILING = 200
#: Log lines read before the scan gives up and says so.
MAX_LINES_SCANNED = 200_000

#: The log, relative to the memory dir. One definition, shared with the trace.
RETRIEVAL_LOG_REL = os.path.join(".audit", "retrievals.jsonl")

#: A bundle id is an opaque hex digest (``receipt.derive_bundle_id`` emits 16
#: chars). Validated for *shape* only, and never used to build a filesystem
#: path: it addresses rows inside one known file.
_BUNDLE_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


class InvalidBundleId(ValueError):
    """A bundle id that is not an opaque hex digest."""


# ── who may read the caller-identifying half of a delivery ───────────────────


#: Served to a caller who passes the diagnostics gate.
_DIAGNOSTICS_NOT_REQUESTED = (
    "the caller's query prose and session identifier are diagnostics-only, exactly as "
    "on the receipt; ask for the diagnostics view on a local operator surface to see them"
)

#: Refused. Deliberately ONE message for every refusal: a message that varied
#: with whether the delivery recorded a session id would disclose that fact to
#: the caller it is refusing, which is the thing being withheld.
_DIAGNOSTICS_REFUSED = (
    "the caller's query prose and session identifier are diagnostics-only and are "
    "served only to a request on a loopback bind, or to the session that made this "
    "delivery; this request is neither"
)


@dataclass(frozen=True)
class QueryAccess:
    """May this caller read the caller-identifying half of a delivery?

    Two fields on a delivery describe *the person who asked*, not the memory
    that was supplied: the ``query`` prose and the ``session_id``. The rest of
    the explanation is about records, and is governed by the visibility gate.
    These two are not, and a bundle id is not a secret — it is a short
    deterministic digest that a delivery hands back, that gets pasted into
    issues and chats, and that ``GET /trace/{file}`` already lists for every
    delivery a file was supplied in. So holding one must not be what authorises
    reading someone else's words.

    Access is granted on either of two grounds:

    ``loopback``
        The API is bound where only this machine can reach it. This is the
        load-bearing one, and it is the same bind test the local inspector
        hard-refuses on — one predicate, so the two cannot drift.
    ``session_id`` matching the delivery's own
        A remote caller reading back *its own* delivery. This is a correlation
        check, not authentication: a session id is caller-supplied and
        unverified, and ``GET /trace/{file}`` already reports the sessions a
        file was recalled in, so a caller who can reach that route can present
        one. It is offered because a legitimate remote session should be able
        to explain its own delivery; it is not what makes the gate sound. The
        bind is.

    A refusal is never an error: the response is the public view with both
    fields marked :data:`WITHHELD_DIAGNOSTICS_ONLY`. A 403 would answer a
    question about the bundle that the caller has not earned.
    """

    #: Did the caller ask for the diagnostics view at all?
    requested: bool = False
    #: Is the API bound where only this machine can reach it?
    loopback: bool = False
    #: The session identity the caller presented, if any.
    session_id: str | None = None

    def decide(self, recorded_session_id: str | None) -> tuple[bool, str]:
        """``(allowed, detail)`` for this delivery's recorded session."""
        if not self.requested:
            return False, _DIAGNOSTICS_NOT_REQUESTED
        if self.loopback:
            return True, ""
        if self.session_id and recorded_session_id and self.session_id == recorded_session_id:
            return True, ""
        return False, _DIAGNOSTICS_REFUSED


def validate_bundle_id(bundle_id: str) -> str:
    """Return ``bundle_id`` if it is a well-formed opaque id, else raise.

    The id addresses rows in one known log file; it is never joined onto a
    path. Shape validation is therefore about refusing nonsense early (and
    refusing anything path-shaped outright), not about traversal defence that
    a later ``os.path.join`` would need.
    """
    candidate = str(bundle_id or "").strip().lower()
    if not _BUNDLE_ID_RE.match(candidate):
        raise InvalidBundleId("bundle_id must be a hex digest of 8–64 characters")
    return candidate


def unavailable(reason: str, detail: str) -> dict[str, Any]:
    """An explicitly missing value: ``{available: false, reason, detail}``.

    Every absent field carries one of these. An omitted key and a ``null`` are
    both silent; this is not.
    """
    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(f"unknown unavailable reason {reason!r}")
    return {"available": False, "reason": reason, "detail": detail}


def _value(raw: Any, *, reason: str, detail: str) -> Any:
    """``raw`` when the log recorded it, else an :func:`unavailable` marker."""
    return raw if raw not in (None, "") else unavailable(reason, detail)


# ── reading the log ──────────────────────────────────────────────────────────


class _Scan:
    """What one bounded pass over the log found."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.lines_scanned = 0
        self.rows_total = 0
        self.rows_without_bundle = 0
        self.oldest_timestamp: str | None = None
        self.newest_timestamp: str | None = None
        self.scan_truncated = False


def _scan_log(log_path: str, bundle_id: str) -> _Scan:
    """One bounded pass: collect this bundle's rows, and the log's own horizon.

    The horizon matters as much as the rows. When nothing matches, the oldest
    timestamp still present is what tells a reader whether the delivery could
    have been rotated out, and the count of rows carrying no ``bundle_id`` is
    what tells them whether this store predates receipts.
    """
    scan = _Scan()
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                if scan.lines_scanned >= MAX_LINES_SCANNED:
                    scan.scan_truncated = True
                    break
                scan.lines_scanned += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                scan.rows_total += 1
                timestamp = str(event.get("timestamp") or "")
                if timestamp:
                    if scan.oldest_timestamp is None or timestamp < scan.oldest_timestamp:
                        scan.oldest_timestamp = timestamp
                    if scan.newest_timestamp is None or timestamp > scan.newest_timestamp:
                        scan.newest_timestamp = timestamp
                row_bundle = event.get("bundle_id")
                if not row_bundle:
                    scan.rows_without_bundle += 1
                    continue
                if str(row_bundle) == bundle_id:
                    scan.rows.append(event)
    except OSError:
        return scan
    return scan


# ── per-record composition ───────────────────────────────────────────────────


def _links(ref: str, rel_path: str) -> dict[str, Any]:
    """Where to go next for this record: its source, its history, its trail.

    Every target already exists. The explanation is a junction, not a new view
    of the memory itself.
    """
    return {
        "memory": f"/ui/memory/{ref}",
        "history": f"/ui/history/{ref}",
        "commands": [
            f"palinode trace {rel_path}",
            f"palinode history {rel_path}",
            f"palinode blame {rel_path}",
        ],
    }


def _source_state(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Has each delivered record's source changed since it was delivered?

    The one thing read from the store today, and deliberately the only one: the
    revision the log *recorded* is compared against the file as it is now,
    through the same ``store.check_freshness`` a delivery uses. That is a
    comparison against a recorded value, not a reconstruction of the delivery.

    Only ``index_section_sha256`` revisions can be compared — a ``file_sha256``
    revision is a different hash domain and is reported as such rather than
    compared across domains, which could never match.
    """
    from palinode.core import store

    comparable: list[dict[str, Any]] = []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("chunk_id") or "") + "\x00" + str(row.get("file_path") or "")
        revision = row.get("revision")
        basis = row.get("revision_basis")
        if not revision:
            out[key] = unavailable(
                NOT_RECORDED,
                "no source revision was recorded for this record, so nothing can be compared",
            )
            continue
        if basis != "index_section_sha256":
            out[key] = unavailable(
                REVISION_BASIS_NOT_COMPARABLE,
                f"the recorded revision is a {basis or 'unknown'} hash; the freshness "
                "check compares index-section hashes and the domains are never mixed",
            )
            continue
        comparable.append({
            "__key": key,
            "file_path": str(row.get("file_path") or ""),
            "content_hash": str(revision),
            "section_id": str(row.get("chunk_id") or "root"),
        })
    if comparable:
        for checked in store.check_freshness(comparable):
            freshness = checked.get("freshness")
            out[str(checked["__key"])] = {
                "available": True,
                "status": {"valid": "unchanged", "stale": "changed"}.get(
                    str(freshness), "unknown"
                ),
                "basis": "index_section_sha256",
                "note": (
                    "compares the revision recorded at delivery against the file now; "
                    "it says nothing about what the delivery contained"
                ),
            }
    return out


def _supplied_records(
    rows: Sequence[Mapping[str, Any]],
    *,
    memory_dir: str,
    chain: Any,
    max_records: int,
) -> tuple[list[dict[str, Any]], int, int, dict[str, int]]:
    """Visible supplied records, the redacted count, the overflow count, counts.

    Visibility is decided by :func:`palinode.core.visibility.is_visible` on the
    live file, exactly as recall decides it — a record the caller could not have
    been shown by a search is not shown by an explanation of a search either. A
    record whose file can no longer be read fails closed for the same reason
    ``is_visible`` does: nothing can prove it was not private.
    """
    from palinode.core.visibility import is_visible

    visible_rows: list[Mapping[str, Any]] = []
    redacted = 0
    cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        file_path = str(row.get("file_path") or "")
        if not file_path:
            continue
        if not is_visible(chain, file_path, cache=cache):
            redacted += 1
            continue
        visible_rows.append(row)

    # After the gate, never before: a record this caller may not see is not a
    # record this caller gets a freshness read of.
    states = _source_state(visible_rows)

    counts: dict[str, int] = {}
    for row in visible_rows:
        disposition = str(row.get("disposition") or "unrecorded")
        counts[disposition] = counts.get(disposition, 0) + 1

    shown: list[dict[str, Any]] = []
    for row in visible_rows[:max_records]:
        file_path = str(row.get("file_path") or "")
        ref = _ref_of(file_path, memory_dir)
        rel_path = f"{ref}.md"
        key = str(row.get("chunk_id") or "") + "\x00" + file_path
        shown.append({
            "ref": ref,
            "rank": _value(
                row.get("rank"),
                reason=NOT_RECORDED,
                detail="this row records no rank",
            ),
            "score": _value(
                row.get("score"),
                reason=NOT_RECORDED,
                detail="this row records no retrieval score",
            ),
            "revision": _value(
                row.get("revision"),
                reason=NOT_RECORDED,
                detail="the delivery computed no source revision for this record",
            ),
            "revision_basis": _value(
                row.get("revision_basis"),
                reason=NOT_RECORDED,
                detail="the delivery named no revision domain for this record",
            ),
            "disposition": _value(
                row.get("disposition"),
                reason=PREDATES_RECEIPTS,
                detail="this row was written before dispositions were recorded",
            ),
            "lineage_group": _value(
                row.get("lineage_group"),
                reason=NOT_RECORDED,
                detail=(
                    "the record named no origin anchor — which is unknown lineage, "
                    "not an independent observation"
                ),
            ),
            "source_state": states.get(
                key,
                unavailable(NOT_RECORDED, "nothing recorded to compare against"),
            ),
            "links": _links(ref, rel_path),
        })
        if row.get("event_type") == BUNDLE_RECEIPT:
            origin = row.get("origin")
            if origin:
                origin_ref = str(origin).split("#", 1)[0].removesuffix(".md")
                if not is_visible(chain, os.path.join(memory_dir, origin_ref + ".md"), cache=cache):
                    shown[-1]["lineage_group"] = unavailable(
                        WITHHELD_VISIBILITY, "the origin record is not visible to this caller",
                    )
            qualifiers = []
            for qualifier in row.get("qualifiers") or []:
                # Ref-bearing qualifiers obey the same live visibility gate.
                prefix, _, target = qualifier.partition(":")
                if prefix in {"contradicts", "stale_backing"}:
                    target = target.split("@", 1)[0].removesuffix(".md")
                    if not is_visible(chain, os.path.join(memory_dir, target + ".md"), cache=cache):
                        continue
                qualifiers.append(qualifier)
            shown[-1].update({
                "selection_role": row.get("role"),
                "selection_reasons": row.get("reasons") or [],
                "qualifiers": qualifiers,
                **{key: row.get(key) for key in ("freshness", "currency", "span_integrity")},
            })
    not_shown = max(0, len(visible_rows) - len(shown))
    return shown, redacted, not_shown, counts


# ── the explanation ──────────────────────────────────────────────────────────


def _not_found_candidates(
    scan: _Scan, *, log_exists: bool, log_enabled: bool
) -> list[dict[str, Any]]:
    """Every reason this id could be absent, each marked checked or not.

    The point is the ``checked`` column. Two of these can be settled from here
    (the log is off; the log is gone); two can only be evidenced (rotation, and
    a store predating receipts); one cannot be checked at all, because a bundle
    id carries no surface. Presenting them as a flat list of guesses would be
    the dishonest version.
    """
    return [
        {
            "reason": INSTRUMENTATION_DISABLED,
            "checked": True,
            "holds": not log_enabled,
            "detail": (
                "retrieval capture is off for this store "
                "(PALINODE_INSTRUMENTATION_DISABLED or instrumentation.capture_retrievals)"
            ),
        },
        {
            "reason": LOG_ABSENT,
            "checked": True,
            "holds": not log_exists,
            "detail": f"{RETRIEVAL_LOG_REL} does not exist — never written, or removed since",
        },
        {
            "reason": NO_MATCHING_ROWS,
            "checked": True,
            "holds": log_exists and scan.rows_total > 0,
            "detail": (
                f"the log holds {scan.rows_total} row(s), the oldest from "
                f"{scan.oldest_timestamp or 'an unrecorded time'}; if this delivery is "
                "older than that, its rows were rotated or truncated away"
            ),
        },
        {
            "reason": PREDATES_RECEIPTS,
            "checked": True,
            "holds": scan.rows_without_bundle > 0,
            "detail": (
                f"{scan.rows_without_bundle} row(s) in this log carry no bundle_id — "
                "they were written before delivery receipts existed and no id addresses them"
            ),
        },
        {
            "reason": SURFACE_WRITES_NO_ROWS,
            "checked": False,
            "holds": None,
            "detail": (
                "a bundle id carries no surface, so this cannot be checked from the id "
                "alone. These surfaces build a receipt and write no rows: "
                + "; ".join(NON_LOGGING_SURFACES)
            ),
        },
    ]


def _correction_pointer() -> dict[str, Any]:
    """Where a correction is made — a pointer to the reviewed flow, not a button.

    The explanation is read-only. It names the preview-then-apply correction
    commands, which write nothing until applied, and the plain write routes
    that still exist underneath them.
    """
    return {
        "status": "available",
        "note": (
            "Correct a delivered memory by previewing the change first; nothing is "
            "written until it is applied, and an applied correction can be undone."
        ),
        "routes": [
            "palinode corrections preview --target <ref> --replacement <text> --reason <why>"
            "  (POST /corrections/preview) — see the change, its blast radius and the revision",
            "palinode corrections apply ... --expect-revision <rev> --confirm"
            "  (POST /corrections/apply) — write it; `palinode corrections undo` reverses it",
            "palinode save --update-policy replace  (POST /save) — rewrite the record directly",
            "palinode archive --superseded-by <ref>  (POST /archive) — retire it, naming the successor",
        ],
        "docs": "docs/CORRECTIONS.md",
    }


def explain_delivery(
    bundle_id: str,
    *,
    memory_dir: str,
    chain: Any = None,
    query_access: QueryAccess | None = None,
    max_records: int = DEFAULT_MAX_RECORDS,
    log_enabled: bool | None = None,
) -> dict[str, Any]:
    """Compose the explanation of one delivery from the rows it wrote.

    ``bundle_id`` must already be shape-validated (:func:`validate_bundle_id`).

    Two gates, and they protect different things. ``chain`` is the caller's
    resolved scope chain (``None`` = no scope identity; access control still
    applies) and decides which supplied **records** may be named.
    ``query_access`` decides whether the caller-identifying half of the
    delivery — the query prose and the session id — may be read at all; see
    :class:`QueryAccess`. Omitted, nothing diagnostic is disclosed.

    Returns a structured object; :func:`format_explanation_text` renders it.
    Never raises on a missing or unreadable log — that is an answer, not a
    failure.
    """
    from palinode.core.config import config
    from palinode.core.retrieval_log import instrumentation_disabled_by_env

    access = query_access or QueryAccess()
    max_records = max(1, min(int(max_records), MAX_RECORDS_CEILING))
    if log_enabled is None:
        log_enabled = not instrumentation_disabled_by_env() and bool(
            config.instrumentation.capture_retrievals
        )

    log_path = os.path.join(memory_dir, RETRIEVAL_LOG_REL)
    log_exists = os.path.exists(log_path)
    scan = _scan_log(log_path, bundle_id) if log_exists else _Scan()

    log_block: dict[str, Any] = {
        "file": RETRIEVAL_LOG_REL,
        "instrumentation_enabled": bool(log_enabled),
        "rows_matched": len(scan.rows),
        "rows_in_log": scan.rows_total,
        "oldest_row": scan.oldest_timestamp,
        "newest_row": scan.newest_timestamp,
        "scan_truncated": scan.scan_truncated,
        "retention": (
            "Palinode appends to this log and never rotates or prunes it. Anything "
            "that trims it — a log rotator, a cleanup, a fresh memory dir — removes "
            "the only record of those deliveries."
        ),
    }

    if not scan.rows:
        reason = (
            INSTRUMENTATION_DISABLED if not log_enabled
            else LOG_ABSENT if not log_exists
            else NO_MATCHING_ROWS
        )
        log_block["status"] = "unavailable"
        log_block["reason"] = reason
        log_block["candidates"] = _not_found_candidates(
            scan, log_exists=log_exists, log_enabled=bool(log_enabled)
        )
        # The same keys as an explained delivery, each an explicit marker. A
        # key that disappears when there is no answer is a silent omission with
        # extra steps — and it makes every consumer guess at the shape.
        nothing = unavailable(reason, "no rows are recorded for this reference")
        return {
            "bundle_id": bundle_id,
            "status": NOT_FOUND,
            "summary": (
                "No delivery with this reference is recorded. That is not the same as "
                "no delivery having happened — see the candidate causes."
            ),
            "delivery": nothing,
            "selection_path": nothing,
            "query": nothing,
            "supplied": [],
            "not_shown": {"count": 0, "note": ""},
            "redacted": {"count": 0, "note": ""},
            "evidence_records": nothing,
            "acted_on": _acted_on(),
            "correction": _correction_pointer(),
            "log": log_block,
        }

    log_block["status"] = "present"
    rows = scan.rows
    head = rows[0]
    is_bundle = head.get("event_type") == BUNDLE_RECEIPT
    if is_bundle:
        receipt = head["receipt"]
        head = {
            **head, **receipt, "source": "resolve",
            "disposition": NONE_DELIVERED,
        }
        rows = [{
            **head, **record,
            "file_path": os.path.join(memory_dir, record["ref"] + ".md"),
            "lineage_group": record.get("origin"),
        } for record in receipt["supplied"]] or [head]
    delivered_rows = [r for r in rows if r.get("file_path")]
    empty_delivery = not delivered_rows and any(
        str(r.get("disposition")) == NONE_DELIVERED for r in rows
    )

    shown, redacted, not_shown, counts = _supplied_records(
        delivered_rows, memory_dir=memory_dir, chain=chain, max_records=max_records
    )

    # The caller-identifying half of the delivery, decided once and applied to
    # both fields it covers. `session_id` rides the same gate as `query` and
    # not for tidiness: disclosing it in the public view would hand a stranger
    # the exact value the session-match branch accepts as identity.
    recorded_session = head.get("session_id")
    diagnostics_allowed, diagnostics_detail = access.decide(
        str(recorded_session) if recorded_session else None
    )

    scope = head.get("scope")
    project_ref = next(
        (s for s in (scope or []) if isinstance(s, str) and s.startswith("project/")),
        None,
    )
    delivery = {
        "evaluated_at": _value(
            head.get("timestamp"),
            reason=NOT_RECORDED,
            detail="this row carries no timestamp",
        ),
        "policy_version": _value(
            head.get("policy_version"),
            reason=PREDATES_RECEIPTS,
            detail="written before the delivery policy version was recorded",
        ),
        "scope": scope if scope is not None else unavailable(
            PREDATES_RECEIPTS,
            "written before the server-resolved caller scope was recorded",
        ),
        "project": (
            {"value": project_ref, "resolved_by": unavailable(
                NOT_RECORDED,
                "the log records the resolved scope chain, not which source decided the "
                "project; the response that carried this delivery named it as "
                "project_resolved_by",
            )}
            if project_ref
            else unavailable(
                NOT_RECORDED,
                "no project level is present on the recorded scope chain",
            )
        ),
        "coverage": head.get("coverage") or unavailable(
            PREDATES_RECEIPTS,
            "written before delivery coverage was recorded",
        ),
        # ``None`` is a real answer here: no boundary is *known*. It is never a
        # claim that none exists, which is why the renderer spells that out
        # rather than printing an empty cell.
        "next_transition": head.get("next_transition") or None,
        "requested_time": unavailable(
            NOT_RECORDED,
            "the log records when the delivery was evaluated, not the lifecycle clock "
            "it evaluated against; the receipt in the response carried both",
        ),
        "session_id": (
            _value(
                recorded_session,
                reason=NOT_RECORDED,
                detail="the caller sent no session identifier",
            )
            if diagnostics_allowed
            else unavailable(WITHHELD_DIAGNOSTICS_ONLY, diagnostics_detail)
        ),
        "dispositions": counts,
    }

    selection_path = {
        "surface": _value(
            head.get("source"),
            reason=NOT_RECORDED,
            detail="this row records no calling surface",
        ),
        "surface_meaning": _SOURCE_LABELS.get(str(head.get("source") or ""), ""),
        "demand": _value(
            head.get("mode"),
            reason=NOT_RECORDED,
            detail="this row records no demand classification",
        ),
        "demand_meaning": _MODE_LABELS.get(str(head.get("mode") or ""), ""),
        "core_trigger_or_recall": unavailable(
            NOT_RECORDED,
            "the log records the calling surface and whether the call was explicit or "
            "passive. Whether a record arrived by core injection, by a trigger, or by "
            "associative recall is not recorded, and is not inferred here",
        ),
    }
    if is_bundle:
        delivery["requested_time"] = head["requested_time"]
        delivery["budget"] = head["budget"]
        selection_path["steps"] = head["selection_path"]

    query = (
        _value(head.get("query"), reason=NOT_RECORDED,
               detail="no query was recorded for this delivery")
        if diagnostics_allowed
        else unavailable(WITHHELD_DIAGNOSTICS_ONLY, diagnostics_detail)
    )

    return {
        "bundle_id": bundle_id,
        "status": NONE_DELIVERED_STATUS if empty_delivery else EXPLAINED,
        "summary": (
            "Resolved, and nothing was delivered. No memory was supplied."
            if empty_delivery and is_bundle else
            "Searched, and nothing was delivered. The search is recorded; no memory "
            "was supplied."
            if empty_delivery
            else f"{len(shown) + not_shown + redacted} record(s) supplied in this delivery."
        ),
        "delivery": delivery,
        "selection_path": selection_path,
        "query": query,
        "supplied": shown,
        "not_shown": {
            "count": not_shown,
            "note": f"{not_shown} more not shown" if not_shown else "",
        },
        "redacted": {
            "count": redacted,
            "note": (
                f"{redacted} supplied record(s) are not named here: they are private, "
                "restricted, off this caller's scope chain, or no longer readable"
                if redacted
                else ""
            ),
        },
        "evidence_records": "included in supplied" if is_bundle else unavailable(
            NOT_RECORDED,
            "records supplied only as evidence around a hit ride the receipt in the "
            "response, not this log, so they cannot be listed here",
        ),
        "acted_on": _acted_on(),
        "correction": _correction_pointer(),
        "log": log_block,
    }


def _acted_on() -> dict[str, Any]:
    """The supplied/used separation, stated rather than implied.

    Mirrors ``trace``'s ``used_in`` row and its stable public gap label: the
    terminal consuming-action edge is ``G3`` and it is not built, so there is no
    evidence that an agent acted on any of this — and none is implied.
    """
    return {
        "status": "not_captured",
        "gap": "G3",
        "note": (
            "This is supplied context only. Palinode records no evidence that an agent "
            "read, used, or acted on any of it — the consuming-action edge is not built."
        ),
    }


# ── text rendering (shared by CLI + MCP) ─────────────────────────────────────


def format_field(value: Any) -> str:
    """One field as a line fragment: the value, or ``unavailable (reason)``."""
    if isinstance(value, dict) and value.get("available") is False:
        return f"unavailable ({value['reason']})"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "—"
    return "—" if value in (None, "") else str(value)


def format_explanation_text(explanation: Mapping[str, Any]) -> str:
    """Render an explanation as human-readable text.

    Shared by the CLI and MCP surfaces so neither can develop its own reading of
    what was delivered. Carries ``[...]`` brackets, so terminal callers must
    print it with Rich markup disabled — the same guard ``trace`` needs.
    """
    lines = [f"## Delivery: {explanation['bundle_id']}", ""]
    lines.append(explanation.get("summary", ""))
    lines.append("")

    if explanation["status"] == NOT_FOUND:
        log = explanation["log"]
        lines.append(f"Looked in:    {log['file']}  ({log['rows_in_log']} row(s))")
        lines.append(f"Result:       {log['reason']}")
        lines.append("")
        lines.append("Why this reference may not be here:")
        for candidate in log.get("candidates", []):
            if candidate["checked"]:
                verdict = "YES" if candidate["holds"] else "no"
            else:
                verdict = "cannot be checked"
            lines.append(f"  [{verdict}] {candidate['reason']} — {candidate['detail']}")
        lines.append("")
        lines.append(_fmt_correction(explanation["correction"]))
        return "\n".join(lines)

    delivery = explanation["delivery"]
    selection = explanation["selection_path"]
    lines.append(f"Evaluated:    {format_field(delivery['evaluated_at'])}")
    lines.append(f"Policy:       {format_field(delivery['policy_version'])}")
    scope = delivery["scope"]
    # An empty chain is a resolved answer — "no scope identity, access control
    # only" — and rendering it as a blank would read as a missing one.
    lines.append(
        "Scope:        "
        + ("no scope identity (access control only)"
           if isinstance(scope, list) and not scope
           else format_field(scope))
    )
    project = delivery["project"]
    if isinstance(project, dict) and project.get("available") is False:
        lines.append(f"Project:      {format_field(project)}")
    else:
        lines.append(
            f"Project:      {project['value']}  "
            f"(resolved by: {format_field(project['resolved_by'])})"
        )
    lines.append(
        f"Selected by:  {format_field(selection['surface'])}"
        + (f" — {selection['surface_meaning']}" if selection["surface_meaning"] else "")
    )
    lines.append(
        f"Demand:       {format_field(selection['demand'])}"
        + (f" — {selection['demand_meaning']}" if selection["demand_meaning"] else "")
    )
    lines.append(f"Path:         {format_field(selection['core_trigger_or_recall'])}")
    if selection.get("steps"):
        lines.append(f"Resolution:   {' → '.join(selection['steps'])}")
    if delivery.get("budget"):
        lines.append(f"Budget:       {format_field(delivery['budget'])}")
    # The one field whose *reason* is the whole answer: a caller pointed at a
    # remote API must see that the query was withheld and why, rather than a
    # bare token they could mistake for "nothing was recorded".
    query = explanation["query"]
    lines.append(
        "Query:        "
        + (
            f"{format_field(query)} — {query['detail']}"
            if isinstance(query, dict) and query.get("available") is False
            else format_field(query)
        )
    )
    coverage = delivery["coverage"]
    if isinstance(coverage, dict) and coverage.get("available") is False:
        lines.append(f"Coverage:     {format_field(coverage)}")
    else:
        reasons = ", ".join(coverage.get("reasons") or []) or "no qualifiers"
        lines.append(f"Coverage:     {coverage.get('status', '?')} — {reasons}")
    lines.append(
        "Next change:  "
        + (
            str(delivery["next_transition"])
            if delivery["next_transition"]
            else "no known boundary ahead — which never means 'never'"
        )
    )
    lines.append("")

    if explanation["status"] == NONE_DELIVERED_STATUS:
        lines.append("Supplied:     nothing was delivered.")
    else:
        counts = delivery.get("dispositions") or {}
        summary = ", ".join(f"{name}×{count}" for name, count in sorted(counts.items()))
        lines.append(f"Supplied:     {summary or 'none'}")
        for record in explanation["supplied"]:
            state = record["source_state"]
            state_text = (
                format_field(state)
                if state.get("available") is False
                else f"source {state['status']} since"
            )
            lines.append(
                f"  - {record['ref']}  [{format_field(record['disposition'])}]  "
                f"rev {format_field(record['revision'])}  ({state_text})"
            )
            lines.append(f"      inspect: {record['links']['commands'][0]}")
            if record.get("selection_role"):
                lines.append(f"      role: {record['selection_role']}")
                if record.get("selection_reasons"):
                    lines.append("      selected because: " + ", ".join(record["selection_reasons"]))
                qualifiers = record.get("qualifiers") or []
                axes = [f"{key}: {record[key]}" for key in ("freshness", "currency", "span_integrity")
                        if record.get(key) is not None]
                lines.append("      qualifiers: " + ", ".join(qualifiers + axes))
    if explanation["not_shown"]["count"]:
        lines.append(f"              … {explanation['not_shown']['note']}")
    if explanation["redacted"]["count"]:
        lines.append(f"              {explanation['redacted']['note']}")
    lines.append(f"Evidence:     {format_field(explanation['evidence_records'])}")
    lines.append("")
    acted = explanation["acted_on"]
    lines.append(f"Acted on:     — not yet captured ({acted['gap']})")
    lines.append(f"              {acted['note']}")
    lines.append("")
    lines.append(_fmt_correction(explanation["correction"]))
    return "\n".join(lines)


def _fmt_correction(correction: Mapping[str, Any]) -> str:
    lines = [f"To correct a memory ({correction['status']}):", f"  {correction['note']}"]
    for route in correction["routes"]:
        lines.append(f"  · {route}")
    lines.append(f"  · see {correction['docs']}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MAX_RECORDS",
    "EXPLAINED",
    "INSTRUMENTATION_DISABLED",
    "InvalidBundleId",
    "LOG_ABSENT",
    "MAX_RECORDS_CEILING",
    "NONE_DELIVERED_STATUS",
    "NON_LOGGING_SURFACES",
    "NOT_FOUND",
    "NOT_RECORDED",
    "NO_MATCHING_ROWS",
    "PREDATES_RECEIPTS",
    "QueryAccess",
    "RETRIEVAL_LOG_REL",
    "REVISION_BASIS_NOT_COMPARABLE",
    "SURFACE_WRITES_NO_ROWS",
    "UNAVAILABLE_REASONS",
    "WITHHELD_DIAGNOSTICS_ONLY",
    "WITHHELD_VISIBILITY",
    "explain_delivery",
    "format_explanation_text",
    "format_field",
    "unavailable",
    "validate_bundle_id",
]
