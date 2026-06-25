"""Unit tests for the op-parse/normalize seam (#555).

The defensiveness that used to be smeared across runner._consolidate_project,
executor.apply_operations, runner._proposed_changes, and write_time._translate_ops
now lives in one module — so it's tested once, here.
"""

from __future__ import annotations

import pytest

from palinode.consolidation.op_parse import op_kind, op_reason, parse_operations


# ── op_kind ──────────────────────────────────────────────────────────────────

def test_op_kind_reads_op_key_uppercased():
    assert op_kind({"op": "update"}) == "UPDATE"


def test_op_kind_coalesces_operation_alias():
    # write-time contradiction ops carry "operation" instead of "op".
    assert op_kind({"operation": "delete"}) == "DELETE"


def test_op_kind_prefers_op_over_operation():
    assert op_kind({"op": "MERGE", "operation": "DELETE"}) == "MERGE"


def test_op_kind_missing_is_empty_string():
    assert op_kind({}) == ""
    assert op_kind({"id": "f1"}) == ""


# ── op_reason ────────────────────────────────────────────────────────────────

def test_op_reason_reads_reason():
    assert op_reason({"reason": "stale"}) == "stale"


def test_op_reason_coalesces_rationale_alias():
    assert op_reason({"rationale": "superseded"}) == "superseded"


def test_op_reason_prefers_reason_over_rationale():
    assert op_reason({"reason": "a", "rationale": "b"}) == "a"


def test_op_reason_missing_is_empty_string():
    assert op_reason({}) == ""


# ── parse_operations ─────────────────────────────────────────────────────────

def test_parse_clean_array_returned_verbatim():
    raw = 'noise [{"op": "KEEP", "id": "f1"}, {"op": "UPDATE", "id": "f2"}] trailer'
    ops = parse_operations(raw)
    assert ops == [{"op": "KEEP", "id": "f1"}, {"op": "UPDATE", "id": "f2"}]


def test_parse_no_array_returns_empty():
    assert parse_operations("the model refused to answer") == []
    assert parse_operations("") == []


def test_parse_clean_path_is_not_filtered():
    # Behaviour parity with the prior inline logic: a clean json.loads is
    # returned as-is (the executor isinstance-guards each op downstream), so a
    # stray non-dict entry survives parse rather than being dropped here.
    ops = parse_operations('[{"op": "KEEP", "id": "f1"}, "stray"]')
    assert ops == [{"op": "KEEP", "id": "f1"}, "stray"]


def test_parse_malformed_json_recovered_and_filtered():
    pytest.importorskip("json_repair")
    # Trailing commas → json.loads fails; json_repair recovers and the repair
    # path filters to well-formed dict-ops carrying "op".
    raw = '[{"op": "UPDATE", "id": "f1", "new_text": "x",}, {"bad": 1},]'
    ops = parse_operations(raw)
    assert {"op": "UPDATE", "id": "f1", "new_text": "x"} in ops
    assert all(isinstance(o, dict) and "op" in o for o in ops)
