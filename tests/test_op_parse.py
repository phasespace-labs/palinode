"""Unit tests for the op-parse/normalize seam.

The defensiveness that used to be smeared across runner._consolidate_project,
executor.apply_operations, runner._proposed_changes, and write_time._translate_ops
now lives in one module — so it's tested once, here.
"""

from __future__ import annotations

from palinode.consolidation.op_parse import (
    op_kind,
    op_reason,
    parse_operations,
    parse_result,
)


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
    # Trailing commas → json.loads fails; json_repair recovers and the repair
    # path filters to well-formed dict-ops carrying "op". No importorskip:
    # `json-repair` is a declared dependency now, so a missing module
    # is a broken install, not a reason to pass this test silently — which is
    # exactly how the recovery path went two releases without ever running.
    raw = '[{"op": "UPDATE", "id": "f1", "new_text": "x",}, {"bad": 1},]'
    ops = parse_operations(raw)
    assert {"op": "UPDATE", "id": "f1", "new_text": "x"} in ops
    assert all(isinstance(o, dict) and "op" in o for o in ops)


def test_json_repair_is_importable():
    """The declared dependency, asserted directly: the recovery path above is
    only meaningful if the module it reaches for is actually installed."""
    from json_repair import repair_json

    assert repair_json('[{"op": "KEEP",}]', return_objects=True) == [{"op": "KEEP"}]


# ── parse_result — failure is failure ────────────────────────────────────────
# `parse_operations` answers "which ops?" and cannot answer "was there anything
# to read?". Both were `[]`, so a truncated 60 s LLM call and a week with
# nothing to compact produced the same run summary.

def test_result_empty_array_is_a_successful_no_op():
    result = parse_result("Nothing to change. []")

    assert result.ok is True
    assert result.operations == []
    assert result.reason == ""


def test_result_no_array_is_a_failure_with_a_reason():
    result = parse_result("I cannot help with that request.")

    assert result.ok is False
    assert result.operations == []
    assert "no JSON array" in result.reason


def test_result_unterminated_array_reads_as_truncation():
    """The dogfood shape: 4754 chars of ops that stop mid-id with no `]`."""
    raw = '```json\n[\n  {"op": "KEEP", "id": "palinode-status-65b9c4"},\n  {"op": "KEEP", "id": "palinode-st'

    result = parse_result(raw)

    assert result.ok is False
    assert result.operations == []
    assert "truncated" in result.reason
    assert "never closed" in result.reason


def test_result_unparseable_array_is_a_failure():
    """json_repair reduces this to `[]`. Recovering nothing from malformed text
    is not the same as a model that proposed nothing — `[]` parses cleanly and
    never reaches the repair path."""
    result = parse_result("[ this is not json at all ]")

    assert result.ok is False
    assert "unparseable" in result.reason


def test_result_repair_that_salvages_no_ops_is_a_failure():
    result = parse_result('[{"no_op_key": 1},]')

    assert result.ok is False
    assert "salvaged no operations" in result.reason


def test_result_repaired_array_is_a_success():
    """A recovery that works is not a failure — json_repair's whole purpose."""
    result = parse_result('[{"op": "UPDATE", "id": "f1", "new_text": "x",},]')

    assert result.ok is True
    assert result.operations == [{"op": "UPDATE", "id": "f1", "new_text": "x"}]


def test_parse_operations_is_the_result_without_the_outcome():
    for raw in ("[]", "prose", '[{"op": "KEEP", "id": "f1"}]', '[{"op": "KEEP"'):
        assert parse_operations(raw) == parse_result(raw).operations
