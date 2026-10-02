"""Correction candidates mined from harness session transcripts.

The highest-value memory signal Palinode does not capture is the moment a user
told an agent it was wrong. This package finds those moments and **proposes**
them; nothing here writes a memory, and nothing here is applied.

Four properties are load-bearing and are why the pieces are split the way they
are:

* **The anchor is deterministic.** :mod:`palinode.corrections.detect` finds a
  candidate span with a narrow grep over the user's own turns. The span text,
  session id, turn index and timestamp are captured with no model involved, so
  a candidate's provenance never depends on an LLM being right or even present.
* **The model only classifies.** :mod:`palinode.corrections.classify` shows a
  bounded window around a matched span to the configured chat model and asks one
  closed question. Anything it is not confident about is ``needs_review`` — never
  an operation.
* **Candidates are operational state, not memory.**
  :mod:`palinode.corrections.queue` writes a JSONL queue under ``.palinode/``
  beside the consolidation gate's state file, with the same descriptor-level
  atomic writer. It deliberately does not go through the memory write path and
  is never git-committed as memory.
* **Nothing is read unless it is switched on.** :mod:`palinode.corrections.scan`
  is off by default, honours ``core.capture_policy`` exactly as every other
  capture source does, and reads only the transcript paths named in config.
"""
from __future__ import annotations

from palinode.corrections.queue import (
    CorrectionCandidate,
    QueueAppendResult,
    append_candidates,
    load_candidates,
    queue_path,
)
from palinode.corrections.readers import (
    TranscriptTurn,
    ClaudeCodeTranscriptReader,
    reader_for_harness,
)
from palinode.corrections.scan import CorrectionScanReport, scan_transcripts

__all__ = [
    "ClaudeCodeTranscriptReader",
    "CorrectionCandidate",
    "CorrectionScanReport",
    "QueueAppendResult",
    "TranscriptTurn",
    "append_candidates",
    "load_candidates",
    "queue_path",
    "reader_for_harness",
    "scan_transcripts",
]
