"""Parse + normalize LLM-proposed consolidation operations.

The seam right after the proposer and right before the deterministic
executor: turn the raw LLM response into a clean list of operation dicts, and
read an op's fields through one canonical accessor instead of re-deriving the
``op``/``operation`` and ``reason``/``rationale`` aliases (and the
``isinstance(op, dict)`` / nested-list defense) at every call site.

Before this module the defensiveness was smeared across four places —
``runner._consolidate_project`` (extract + json_repair + filter),
``executor.apply_operations`` (``op.get("op", "KEEP").upper()`` + isinstance
guard), ``runner._proposed_changes`` (``op``-or-``operation`` + ``reason``-or-
``rationale``), and ``write_time._translate_ops`` (``operation``.upper()). They
now share these helpers.

``parse_operations`` is a faithful extraction of the prior runner logic: a clean
``json.loads`` is returned as-is (the executor still guards each op with its own
isinstance check), and only the ``json_repair`` recovery path filters to
well-formed dict-ops — preserving the existing behaviour exactly.

What it could *not* express was the difference between "the model looked at the
facts and proposed nothing" and "the response was garbage" — both were ``[]``,
and the runner counted both as a quiet week. :func:`parse_result` is the
same parse with that distinction returned: an empty *list* is a no-op, an
*absent* or unparseable array is a failure with a reason. ``parse_operations``
stays as the list-returning shorthand for callers that only want the ops.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("palinode.consolidation")

# An LLM op may carry its kind under "op" (consolidation/executor convention) or
# "operation" (the write-time contradiction-check convention); its rationale
# under "reason" or "rationale". These accessors are the single place that knows.
_KIND_KEYS = ("op", "operation")
_REASON_KEYS = ("reason", "rationale")


def op_kind(op: dict) -> str:
    """Canonical, upper-cased operation kind ("" when absent).

    Coalesces the ``op`` / ``operation`` aliases. Callers that want a default
    (the executor treats a missing kind as ``KEEP``) apply it themselves:
    ``op_kind(op) or "KEEP"``.
    """
    for key in _KIND_KEYS:
        val = op.get(key)
        if val:
            return str(val).upper()
    return ""


def op_reason(op: dict) -> str:
    """Operation rationale ("" when absent), coalescing ``reason`` / ``rationale``."""
    for key in _REASON_KEYS:
        val = op.get(key)
        if val:
            return str(val)
    return ""


@dataclass(frozen=True)
class ParseResult:
    """The outcome of reading an LLM response for operations.

    ``ok`` is the field that did not exist: a parse that produced no operations
    because the model proposed none (``ok=True, operations=[]``) is a no-op, and
    a parse that produced none because there was nothing parseable to read
    (``ok=False``) is a failure the run must report. ``reason`` is the
    operator-facing sentence for the failure, empty when ``ok``.
    """

    operations: list[dict] = field(default_factory=list)
    ok: bool = True
    reason: str = ""


def parse_result(raw_text: str) -> ParseResult:
    """Extract the operations JSON array from an LLM response, honestly.

    Finds the first ``[...]`` array in ``raw_text`` and parses it. On malformed
    JSON, falls back to ``json_repair`` and keeps only well-formed dict-ops (the
    model sometimes nests lists). Never raises.

    Three ways it fails, each with its own reason, because they mean different
    things to whoever reads the log:

    * **no array** — the model answered with prose (a refusal, a preamble it
      never finished). Nothing was proposed and nothing can be.
    * **unterminated array** — an opening ``[`` with no closing ``]`` anywhere
      after it. This is the shape of a response cut off at the token cap; it is
      caught here as well as at the transport, because not every
      OpenAI-compatible server reports ``finish_reason``.
    * **unparseable** — an array that neither :mod:`json` nor ``json_repair``
      could read, or that repaired to no operations at all. A genuinely empty
      proposal (``[]``) parses cleanly and never reaches the repair path, so a
      repair that salvages nothing salvaged nothing.
    """
    json_match = re.search(r'\[[\s\S]*\]', raw_text)
    if not json_match:
        if "[" in raw_text:
            # An opening bracket with no closer: the array started and the
            # response ended. Truncation, near-certainly.
            logger.warning("LLM response opened an operations array but never closed it")
            return ParseResult(
                ok=False,
                reason=(
                    "the operations array was never closed — the response looks "
                    f"truncated ({len(raw_text)} chars returned)"
                ),
            )
        logger.warning("Could not parse operations from LLM response")
        return ParseResult(
            ok=False,
            reason=f"no JSON array in the response ({len(raw_text)} chars returned)",
        )
    try:
        return ParseResult(operations=json.loads(json_match.group()))
    except json.JSONDecodeError:
        # LLM often outputs malformed JSON — use json_repair
        try:
            from json_repair import repair_json
            repaired = repair_json(json_match.group(), return_objects=True)
            if isinstance(repaired, list):
                # Filter out any non-dict entries (LLM sometimes nests lists)
                valid_ops = [op for op in repaired if isinstance(op, dict) and "op" in op]
                if valid_ops:
                    logger.info(
                        f"Repaired malformed LLM JSON ({len(valid_ops)} valid ops "
                        f"from {len(repaired)} entries)"
                    )
                    return ParseResult(operations=valid_ops)
                # Recovering *nothing* from malformed text is not the same as a
                # model that proposed nothing: `[]` parses cleanly and never
                # reaches this branch, so an empty repair means the text was
                # unreadable. Returning ok here is how `[ prose ]` became a
                # silent no-op.
                repair_detail = (
                    f"json_repair salvaged no operations from {len(repaired)} entr"
                    f"{'y' if len(repaired) == 1 else 'ies'}"
                )
            else:
                repair_detail = f"json_repair returned {type(repaired).__name__}, not a list"
        except Exception as repair_err:  # noqa: BLE001
            logger.error(f"json_repair also failed: {repair_err}")
            repair_detail = f"json_repair also failed: {repair_err}"
        logger.error("Could not parse LLM JSON for compaction")
        logger.debug(f"Raw LLM output: {json_match.group()[:500]}")
        return ParseResult(ok=False, reason=f"unparseable operations JSON — {repair_detail}")


def parse_operations(raw_text: str) -> list[dict]:
    """The operations in ``raw_text``, or ``[]`` — :func:`parse_result` without
    the outcome.

    Behaviour matches the prior inline ``_consolidate_project`` logic: a clean
    ``json.loads`` is returned verbatim (the executor isinstance-guards each op);
    only the repair path applies the dict/"op" filter. Callers that must tell a
    failed parse from an empty proposal use :func:`parse_result` instead.
    """
    return parse_result(raw_text).operations
