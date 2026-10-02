"""Orchestration: read nothing unless told to, propose everything, apply nothing.

This is the module that touches a harness directory, so it is the module that
carries the controls:

* **Off by default, and local by default.** ``capture.transcripts.enabled`` is
  ``False`` and the path list is empty, so a disabled scan opens no file and
  writes no queue. ``capture.transcripts.classify`` is ``False`` too, and that
  is a separate switch on purpose: enabling the source turns on *reading files
  on this machine*, and nothing else. Until classification is opted into, a scan
  makes no request to any model endpoint at all and every candidate is queued
  ``needs_review`` for a person to look at.
* **The capture policy governs it like every other source.** ``capture_paused``
  stops the whole pass before the first read; an excluded project or an excluded
  path skips that session; a scope that cannot be resolved while exclusions exist
  is a skip, not a guess.
* **Bounded, and loud about it.** A lookback window drops turns older than the
  cutoff and a candidate cap drops the oldest overflow — both counted in the
  report and logged, because a silently truncated scan looks exactly like a
  quiet week.

Nothing here applies anything. Every candidate is a proposal in a queue; the
review flow that turns one into an operation does not exist yet, and until it
does this is a dry-run report.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from palinode.core.capture_policy import evaluate_capture_policy
from palinode.core.config import config
from palinode.corrections import classify as classify_module
from palinode.corrections.detect import DetectedSpan, detect_spans
from palinode.corrections.queue import (
    CorrectionCandidate,
    append_candidates,
    load_candidates,
    utc_now_iso,
)
from palinode.corrections.readers import (
    TranscriptPathError,
    TranscriptSession,
    reader_for_harness,
    supported_harnesses,
    validate_transcript_root,
)

logger = logging.getLogger("palinode.corrections.scan")

#: Policy denials that stop the entire pass rather than one session. Both are
#: statements about the store, not about a project.
_GLOBAL_DENIALS = frozenset({"capture_paused", "invalid_policy"})

#: Nothing in this release applies a candidate. Stated in the report so a caller
#: reading JSON does not have to infer it from the absence of an apply field.
APPLIED_NOTHING = "nothing applied: every candidate is a proposal awaiting review"

#: What the report says when ``capture.transcripts.classify`` is off. Stated
#: positively, on every surface: the absence of a classification block would be
#: indistinguishable from a scan that classified everything as unusable.
DETECTION_ONLY = "detection only; nothing was sent to a model"


@dataclass
class CorrectionScanReport:
    """What one scan read, skipped and proposed."""

    enabled: bool
    harnesses: list[str] = field(default_factory=list)
    roots: int = 0
    transcripts_read: int = 0
    turns_scanned: int = 0
    candidates_detected: int = 0
    candidates_added: int = 0
    duplicates: int = 0
    classified: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    classification_ran: bool = False
    classifier: dict[str, Any] = field(default_factory=dict)
    lookback_days: int | None = None
    cutoff: str | None = None
    blocked_reason: str | None = None
    applied: str = APPLIED_NOTHING

    def skip(self, reason: str, count: int = 1) -> None:
        if count:
            self.skipped[reason] = self.skipped.get(reason, 0) + count

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "harnesses": list(self.harnesses),
            "roots": self.roots,
            "transcripts_read": self.transcripts_read,
            "turns_scanned": self.turns_scanned,
            "candidates_detected": self.candidates_detected,
            "candidates_added": self.candidates_added,
            "duplicates": self.duplicates,
            "classified": dict(self.classified),
            "skipped": dict(self.skipped),
            "classification_ran": self.classification_ran,
            "classifier": dict(self.classifier),
            "lookback_days": self.lookback_days,
            "cutoff": self.cutoff,
            "blocked_reason": self.blocked_reason,
            "applied": self.applied,
        }


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _configured_roots(report: CorrectionScanReport) -> list[tuple[str, Path]]:
    """Validate the configured per-harness path list; drop what is unsafe."""
    roots: list[tuple[str, Path]] = []
    configured = getattr(config.capture.transcripts, "harness_paths", {}) or {}
    for harness, paths in sorted(configured.items()):
        if reader_for_harness(harness) is None:
            logger.warning(
                "no reader for configured harness; skipping harness=%s supported=%s",
                harness, ",".join(supported_harnesses()),
            )
            report.skip("unsupported_harness")
            continue
        for raw in paths or []:
            try:
                roots.append((harness, validate_transcript_root(raw)))
            except TranscriptPathError as error:
                logger.warning("rejecting configured transcript path error=%r", error)
                report.skip("invalid_transcript_path")
    return roots


def _session_scope(session: TranscriptSession) -> tuple[bool, str | None, str | None]:
    """Ask the capture policy about this session. Returns (allowed, project, reason)."""
    decision = evaluate_capture_policy(
        "capture", automatic=True, cwd=session.cwd, source_path=str(session.path)
    )
    return decision.allowed, decision.project, decision.reason


def scan_transcripts(
    *,
    since_days: int | None = None,
    memory_dir: str | None = None,
    classify: bool | None = None,
    now: datetime | None = None,
) -> tuple[CorrectionScanReport, list[CorrectionCandidate]]:
    """Run one detection pass over the configured transcripts.

    ``classify`` defaults to ``capture.transcripts.classify``, which is off.
    The parameter stays for the test suite and the fixture scorer, which need to
    drive both paths in one process; **no surface exposes it**, deliberately. A
    per-call override would let a caller transmit conversation text that the
    operator's configuration said stays on this machine, and "off unless the
    config says otherwise" is not a property a request body may negotiate.

    Returns the report and the candidates that were appended to the queue.
    """
    settings = config.capture.transcripts
    if classify is None:
        classify = bool(settings.classify)
    report = CorrectionScanReport(enabled=bool(settings.enabled))
    if not settings.enabled:
        report.blocked_reason = "disabled"
        return report, []

    gate = evaluate_capture_policy("capture", automatic=True)
    if not gate.allowed and gate.reason in _GLOBAL_DENIALS:
        logger.info("transcript correction scan blocked by capture policy reason=%s", gate.reason)
        report.blocked_reason = gate.reason
        return report, []

    lookback = settings.lookback_days if since_days is None else since_days
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=lookback) if lookback and lookback > 0 else None
    report.lookback_days = lookback
    report.cutoff = cutoff.isoformat().replace("+00:00", "Z") if cutoff else None

    roots = _configured_roots(report)
    report.roots = len(roots)
    report.harnesses = sorted({harness for harness, _ in roots})

    detected: list[tuple[DetectedSpan, TranscriptSession, str | None]] = []
    for harness, root in roots:
        reader = reader_for_harness(harness)
        assert reader is not None  # nosec B101 - _configured_roots filtered these
        for session in reader.sessions(root):
            allowed, project, reason = _session_scope(session)
            if not allowed:
                report.skip(f"policy_{reason or 'denied'}")
                continue
            report.transcripts_read += 1
            if session.truncated:
                report.skip("transcript_truncated")
            eligible = []
            for turn in session.turns:
                if not turn.is_user_evidence:
                    continue
                if cutoff is not None:
                    occurred = _parse_timestamp(turn.timestamp)
                    if occurred is not None and occurred < cutoff:
                        report.skip("turn_outside_lookback")
                        continue
                eligible.append(turn)
            report.turns_scanned += len(eligible)
            for span in detect_spans(eligible):
                detected.append((span, session, project))

    report.candidates_detected = len(detected)
    cap = max(0, int(settings.max_candidates))
    if cap and len(detected) > cap:
        detected.sort(key=lambda item: item[0].timestamp or "", reverse=True)
        overflow = len(detected) - cap
        detected = detected[:cap]
        report.skip("over_max_candidates", overflow)
        logger.warning(
            "transcript correction scan hit its candidate cap; skipped %d candidate(s) "
            "max_candidates=%d", overflow, cap,
        )

    detected_at = utc_now_iso()
    candidates: list[CorrectionCandidate] = []
    dropped_none = 0
    report.classification_ran = bool(classify)
    if classify:
        model, config_role = classify_module.classifier_identity()
        classifier_record: dict[str, Any] = {
            "used": True,
            "model": model,
            "config_role": config_role,
            "summary": (
                f"classified by model={model} at the configured {config_role} "
                "endpoint; a bounded window around each span was sent to it"
            ),
        }
    else:
        classifier_record = {
            "used": False,
            "model": None,
            "config_role": None,
            "summary": DETECTION_ONLY,
        }
    for span, session, project in detected:
        if classify:
            result, turn_range = classify_module.classify_span(span, session.turns)
            label = result.label
            classifier = result.as_classifier_record()
        else:
            # No client is constructed and no request is made: the window is
            # built only to record which turns a later classification would see.
            _, turn_range = classify_module.build_window(session.turns, span.turn_index)
            label = "needs_review"
            classifier = {
                "decided": False,
                "model": None,
                "config_role": None,
                "reason": classify_module.NOT_RUN_REASON,
            }
        report.classified[label] = report.classified.get(label, 0) + 1
        if label == "none_of_these":
            # A confident negative is reported and discarded: queueing it would
            # spend a reviewer's attention on the cases the classifier exists to
            # remove. It is counted above, so the yield numbers stay honest.
            dropped_none += 1
            continue
        candidates.append(
            CorrectionCandidate(
                harness=span.harness,
                session_id=span.session_id,
                turn_index=span.turn_index,
                turn_uuid=span.turn_uuid,
                span=span.span,
                span_hash=span.span_hash,
                grep_family=span.grep_family,
                matched_rules=span.matched_rules,
                classification=label,
                classifier=classifier,
                window_turns=turn_range,
                occurred_at=span.timestamp,
                detected_at=detected_at,
                project=project,
                rationale=span.rationale,
                replaced=span.replaced,
                replacement=span.replacement,
            )
        )
    if dropped_none:
        report.skip("classified_none_of_these", dropped_none)

    result = append_candidates(candidates, memory_dir)
    report.candidates_added = result.added
    report.duplicates = result.duplicates
    report.classifier = classifier_record
    return report, candidates


def _matches_filters(
    record: dict[str, Any], project: str | None, cutoff: datetime | None
) -> bool:
    if project is not None:
        scope = record.get("project")
        wanted = project.removeprefix("project/")
        if scope is None or str(scope).removeprefix("project/") != wanted:
            return False
    if cutoff is not None:
        moment = _parse_timestamp(record.get("occurred_at")) or _parse_timestamp(
            record.get("detected_at")
        )
        if moment is None or moment < cutoff:
            return False
    return True


def corrections_report(
    *,
    project: str | None = None,
    since_days: int | None = None,
    scan: bool = False,
    classify: bool | None = None,
    memory_dir: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """List queued correction candidates, optionally running a detection pass first.

    The single entry point every surface calls, so CLI, REST, MCP and plugin
    cannot drift into different answers about what was found. ``classify`` is
    not one of the things a surface may pass: it defaults to ``None`` here and
    resolves from config inside :func:`scan_transcripts`, and only the tests and
    the scorer supply it.
    """
    settings = config.capture.transcripts
    scan_report: CorrectionScanReport | None = None
    if scan:
        scan_report, _ = scan_transcripts(
            since_days=since_days, memory_dir=memory_dir, classify=classify, now=now
        )

    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=since_days) if since_days and since_days > 0 else None
    records = [
        record
        for record in load_candidates(memory_dir)
        if _matches_filters(record, project, cutoff)
    ]
    records.sort(key=lambda record: str(record.get("occurred_at") or record.get("detected_at") or ""))
    return {
        "enabled": bool(settings.enabled),
        "classify_enabled": bool(settings.classify),
        "source": "harness session transcripts (claude-code)",
        "applied": APPLIED_NOTHING,
        "classification": (
            "enabled: a scan sends a bounded window around each candidate to the "
            "configured consolidation model endpoint"
            if settings.classify
            else DETECTION_ONLY
        ),
        "filters": {"project": project, "since_days": since_days},
        "count": len(records),
        "candidates": records,
        "scan": scan_report.as_dict() if scan_report is not None else None,
    }


__all__ = [
    "APPLIED_NOTHING",
    "DETECTION_ONLY",
    "CorrectionScanReport",
    "corrections_report",
    "scan_transcripts",
]
