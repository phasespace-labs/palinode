"""Parse and normalize generated consolidation operations (#555).

Turn a raw model response into a clean list of operation dicts, and read an
operation's fields through one canonical accessor instead of re-deriving the
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
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("palinode.consolidation")

# A generated op may carry its kind under "op" (consolidation convention) or
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


def parse_operations(raw_text: str) -> list[dict]:
    """Extract the operations JSON array from an LLM response.

    Finds the first ``[...]`` array in ``raw_text`` and parses it. On malformed
    JSON, falls back to ``json_repair`` and keeps only well-formed dict-ops (the
    model sometimes nests lists). Returns ``[]`` when no array is present or
    parsing fails entirely. Never raises.

    Behaviour matches the prior inline ``_consolidate_project`` logic: a clean
    ``json.loads`` is returned verbatim (the executor isinstance-guards each op);
    only the repair path applies the dict/"op" filter.
    """
    json_match = re.search(r'\[[\s\S]*\]', raw_text)
    if not json_match:
        logger.warning("Could not parse operations from LLM response")
        return []
    try:
        return json.loads(json_match.group())
    except json.JSONDecodeError:
        # LLM often outputs malformed JSON — use json_repair
        try:
            from json_repair import repair_json
            repaired = repair_json(json_match.group(), return_objects=True)
            if isinstance(repaired, list):
                # Filter out any non-dict entries (LLM sometimes nests lists)
                valid_ops = [op for op in repaired if isinstance(op, dict) and "op" in op]
                logger.info(
                    f"Repaired malformed LLM JSON ({len(valid_ops)} valid ops "
                    f"from {len(repaired)} entries)"
                )
                return valid_ops
        except Exception as repair_err:  # noqa: BLE001
            logger.error(f"json_repair also failed: {repair_err}")
        logger.error("Could not parse LLM JSON for compaction")
        logger.debug(f"Raw LLM output: {json_match.group()[:500]}")
        return []
