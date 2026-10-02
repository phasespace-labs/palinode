"""The candidate queue — operational state, deliberately not memory.

A correction candidate is a proposal about a memory, not a memory. It lives in a
JSONL file under the store's git-ignored ``.palinode/`` directory, beside the
consolidation gate's state file and written by the same descriptor-level atomic
writer, for the same reason: it must not go through the ``git_tools`` mutation
choke point (which writes *and commits*), and it must not read as if it had
bypassed that choke point by accident.

What a record may contain is as narrow as the record is useful:

* the bounded quoted span — never the turn, never the file, never the
  surrounding conversation;
* the source anchor (harness, session id, turn index, turn uuid, timestamp);
* the project scope;
* the classification and who made it;
* a rationale and a target/replacement relation **only when the span said so**.
  Both keys are absent otherwise. An absent relation is the honest record of
  "the user did not name a replacement"; a guessed one is a proposal to change
  the wrong memory.

There is no witness, corroboration or occurrence count anywhere in this module,
and that is a contract rather than an omission. The dedupe key is
``(session id, span hash)``: re-reading a transcript, or reading a later summary
that restates the same correction, must not make a correction look better
attested than the one time the user said it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from palinode.core.config import config

logger = logging.getLogger("palinode.corrections.queue")

#: Sits beside ``consolidation-state.json`` in the store's operational
#: directory. Never indexed, never committed as memory.
QUEUE_RELATIVE_PATH = Path(".palinode") / "correction-candidates.jsonl"

#: Record shape version. A reader that does not recognise it skips the row
#: rather than guessing at its meaning.
SCHEMA_VERSION = 1

#: The closed set a candidate may carry. The first three are the classes the
#: issue names; ``needs_review`` is everything else, including every case the
#: classifier was not confident about and every case it could not reach a model
#: to ask about.
VALID_CLASSIFICATIONS: tuple[str, ...] = (
    "explicit_decision_change",
    "rejected_approach",
    "remember_this",
    "needs_review",
)

#: A row's review state. ``proposed`` is where every candidate starts; the
#: other two are what a reviewer decided. A resolved row is **kept**, never
#: deleted: the dedupe key is read from every row, so the dismissed row is
#: precisely what stops the next scan re-proposing the same sentence.
PROPOSED = "proposed"
APPLIED = "applied"
DISMISSED = "dismissed"
RESOLVED_STATUSES: tuple[str, ...] = (APPLIED, DISMISSED)
VALID_STATUSES: tuple[str, ...] = (PROPOSED, APPLIED, DISMISSED)


@dataclass(frozen=True)
class CorrectionCandidate:
    """One proposed correction, with its source anchor and nothing more."""

    harness: str
    session_id: str
    turn_index: int
    turn_uuid: str | None
    span: str
    span_hash: str
    grep_family: str
    matched_rules: tuple[str, ...]
    classification: str
    classifier: dict[str, Any]
    window_turns: tuple[int, int]
    occurred_at: str | None
    detected_at: str
    project: str | None
    rationale: str | None = None
    replaced: str | None = None
    replacement: str | None = None

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (self.session_id, self.span_hash)

    @property
    def candidate_id(self) -> str:
        return candidate_id(self.session_id, self.span_hash)

    def to_record(self) -> dict[str, Any]:
        """Serialize to the on-disk shape, omitting what evidence did not support."""
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "status": "proposed",
            "harness": self.harness,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "turn_uuid": self.turn_uuid,
            "window_turns": list(self.window_turns),
            "occurred_at": self.occurred_at,
            "detected_at": self.detected_at,
            "project": self.project,
            "span": self.span,
            "span_hash": self.span_hash,
            "grep_family": self.grep_family,
            "matched_rules": list(self.matched_rules),
            "classification": self.classification,
            "classifier": dict(self.classifier),
        }
        if self.rationale:
            record["rationale"] = self.rationale
        if self.replaced and self.replacement:
            record["relation"] = {
                "replaced": self.replaced,
                "replacement": self.replacement,
                "evidence": "quoted from the span",
            }
        return record


@dataclass(frozen=True)
class QueueAppendResult:
    """What one append did: what landed, what was already there."""

    added: int
    duplicates: int
    total: int

    def as_dict(self) -> dict[str, int]:
        return {"added": self.added, "duplicates": self.duplicates, "total": self.total}


def candidate_id(session_id: str, span_hash_value: str) -> str:
    """A short, stable id derived from the dedupe key itself."""
    digest = hashlib.sha256(f"{session_id}:{span_hash_value}".encode("utf-8"))
    return digest.hexdigest()[:16]


def queue_path(memory_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the candidate queue for one configured memory store."""
    base = memory_dir or getattr(config, "memory_dir", None) or config.palinode_dir
    return Path(base).expanduser() / QUEUE_RELATIVE_PATH


def load_candidates(memory_dir: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Read the queue, skipping rows that are unparseable or of another version."""
    path = queue_path(memory_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as error:
        logger.warning("could not read correction queue path=%s error=%r", path, error)
        return []
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("ignoring unparseable correction candidate path=%s", path)
            continue
        if isinstance(record, dict) and record.get("schema_version") == SCHEMA_VERSION:
            records.append(record)
    return records


def _write_all(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Rewrite the queue atomically.

    Descriptor-level and via ``os.replace``, matching the consolidation gate's
    writer next door: a torn queue would be read back as "no candidates", which
    silently loses proposals rather than loudly failing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    encoded = "".join(
        json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n" for record in records
    ).encode("utf-8")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written == 0:
                raise OSError("short write while recording correction candidates")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


def append_candidates(
    candidates: Iterable[CorrectionCandidate],
    memory_dir: str | os.PathLike[str] | None = None,
) -> QueueAppendResult:
    """Append new candidates, dropping any whose ``(session, span)`` is known.

    A duplicate is *dropped*, not merged and not counted: the second sighting of
    a correction is the same one sentence, and recording it as corroboration
    would let a nightly re-read manufacture confidence nobody expressed.
    """
    path = queue_path(memory_dir)
    existing = load_candidates(memory_dir)
    seen = {
        (record.get("session_id"), record.get("span_hash"))
        for record in existing
    }
    added: list[dict[str, Any]] = []
    duplicates = 0
    for candidate in candidates:
        if candidate.dedupe_key in seen:
            duplicates += 1
            continue
        seen.add(candidate.dedupe_key)
        added.append(candidate.to_record())
    if added:
        _write_all(path, [*existing, *added])
    return QueueAppendResult(
        added=len(added), duplicates=duplicates, total=len(existing) + len(added)
    )


def resolve_candidate(
    candidate_id_value: str,
    *,
    status: str,
    reason: str | None = None,
    target: str | None = None,
    memory_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Mark one candidate applied or dismissed, in place, through this writer.

    The row is rewritten, not removed. Two reasons, and both are load-bearing:

    * ``append_candidates`` reads ``(session_id, span_hash)`` from **every**
      row, so a resolved row is what makes a re-scan a no-op. Deleting it
      would resurrect the candidate on the next pass, which for a dismissal
      means asking the reviewer the same question forever.
    * A dismissal with no trace is indistinguishable from a candidate that was
      never detected. The ``resolution`` block records who decided what, and
      why, beside the span that prompted it.

    Re-resolving is refused rather than silently overwritten: the first
    decision is the record. Returns the row as it now stands, with
    ``already_resolved`` set when nothing was written.
    """
    if status not in RESOLVED_STATUSES:
        raise ValueError(f"status must be one of {', '.join(RESOLVED_STATUSES)}")
    path = queue_path(memory_dir)
    records = load_candidates(memory_dir)
    for record in records:
        if record.get("candidate_id") != candidate_id_value:
            continue
        if record.get("status") in RESOLVED_STATUSES:
            return {**record, "already_resolved": True}
        record["status"] = status
        record["resolution"] = {
            "status": status,
            "reason": reason,
            "target": target,
            "resolved_at": utc_now_iso(),
        }
        _write_all(path, records)
        logger.info(
            "correction candidate resolved id=%s status=%s target=%s",
            candidate_id_value, status, target,
        )
        return {**record, "already_resolved": False}
    raise KeyError(candidate_id_value)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "APPLIED",
    "CorrectionCandidate",
    "DISMISSED",
    "PROPOSED",
    "QUEUE_RELATIVE_PATH",
    "QueueAppendResult",
    "RESOLVED_STATUSES",
    "SCHEMA_VERSION",
    "VALID_CLASSIFICATIONS",
    "VALID_STATUSES",
    "append_candidates",
    "candidate_id",
    "load_candidates",
    "queue_path",
    "resolve_candidate",
    "utc_now_iso",
]
