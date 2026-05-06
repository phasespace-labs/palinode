"""
Typed contract between the LLM proposer and the deterministic executor.

The consolidation pipeline passes operations as ProposalOp dataclasses
instead of raw dicts. The LLM output is parsed at the boundary (in
runner.py / write_time.py) via parse_op(); the executor consumes typed
ProposalOp objects exclusively.

See executor.py for authoritative semantics of each kind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["OpKind", "ProposalOp", "parse_op"]


class OpKind(StrEnum):
    """Consolidation operation kinds.

    Must stay in sync with executor.py's op_type dispatch.
    """
    KEEP = "KEEP"
    UPDATE = "UPDATE"
    MERGE = "MERGE"
    SUPERSEDE = "SUPERSEDE"
    ARCHIVE = "ARCHIVE"
    RETRACT = "RETRACT"


@dataclass(frozen=True)
class ProposalOp:
    """A single consolidation operation, typed.

    The executor (palinode/consolidation/executor.py) is the only
    authoritative consumer; it interprets ``payload`` per ``kind``.

    Payload shapes per kind:
        KEEP:       {} — no payload needed
        UPDATE:     {"id": str, "new_text": str}
        MERGE:      {"ids": list[str], "new_text": str}
        SUPERSEDE:  {"id": str, "new_text": str, "reason": str}
        ARCHIVE:    {"id": str, "rationale"|"reason": str}
        RETRACT:    {"id": str, "reason"|"rationale": str}

    ``kind`` is authoritative for dispatch. ``payload`` carries the
    kind-specific fields verbatim from the LLM output dict (minus the
    ``op`` key which becomes ``kind``).
    """
    kind: OpKind
    payload: dict[str, Any] = field(default_factory=dict)

    # -- Convenience accessors (read-only, derived from payload) --

    @property
    def fact_id(self) -> str | None:
        """Single fact ID for UPDATE/SUPERSEDE/ARCHIVE/RETRACT."""
        return self.payload.get("id")

    @property
    def fact_ids(self) -> list[str]:
        """Multiple fact IDs for MERGE."""
        return self.payload.get("ids", [])


def parse_op(raw: dict[str, Any]) -> ProposalOp:
    """Convert an LLM-output dict into a ProposalOp.

    Required key: ``"op"`` (kind string, case-insensitive).
    All other keys are preserved in ``payload``.

    Raises:
        ValueError: Missing ``"op"`` key or unknown kind.
        TypeError: Input is not a dict.
    """
    if not isinstance(raw, dict):
        raise TypeError(
            f"Expected dict, got {type(raw).__name__}: {raw!r}"
        )

    raw_kind = raw.get("op")
    if raw_kind is None:
        raise ValueError(f"Missing required 'op' key in operation: {raw!r}")

    try:
        kind = OpKind(raw_kind.upper())
    except ValueError:
        raise ValueError(
            f"Unknown operation kind {raw_kind!r}; "
            f"valid kinds: {[k.value for k in OpKind]}"
        ) from None

    # Everything except "op" goes into payload
    payload = {k: v for k, v in raw.items() if k != "op"}

    return ProposalOp(kind=kind, payload=payload)
