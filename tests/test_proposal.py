"""Tests for palinode.consolidation.proposal — typed op contract."""
import pytest
from palinode.consolidation.proposal import OpKind, ProposalOp, parse_op


# ---------------------------------------------------------------------------
# parse_op: happy path — one test per kind
# ---------------------------------------------------------------------------

class TestParseOpHappyPath:
    def test_keep(self):
        op = parse_op({"op": "KEEP", "id": "f1"})
        assert op.kind == OpKind.KEEP
        assert op.payload == {"id": "f1"}

    def test_update(self):
        op = parse_op({"op": "UPDATE", "id": "f2", "new_text": "updated text"})
        assert op.kind == OpKind.UPDATE
        assert op.fact_id == "f2"
        assert op.payload["new_text"] == "updated text"

    def test_merge(self):
        op = parse_op({"op": "MERGE", "ids": ["f1", "f2"], "new_text": "merged"})
        assert op.kind == OpKind.MERGE
        assert op.fact_ids == ["f1", "f2"]
        assert op.payload["new_text"] == "merged"

    def test_supersede(self):
        op = parse_op({"op": "SUPERSEDE", "id": "f3", "new_text": "new", "reason": "stale"})
        assert op.kind == OpKind.SUPERSEDE
        assert op.fact_id == "f3"
        assert op.payload["reason"] == "stale"

    def test_archive(self):
        op = parse_op({"op": "ARCHIVE", "id": "f4", "rationale": "obsolete"})
        assert op.kind == OpKind.ARCHIVE
        assert op.fact_id == "f4"
        assert op.payload["rationale"] == "obsolete"

    def test_retract(self):
        op = parse_op({"op": "RETRACT", "id": "f5", "reason": "wrong"})
        assert op.kind == OpKind.RETRACT
        assert op.fact_id == "f5"
        assert op.payload["reason"] == "wrong"


# ---------------------------------------------------------------------------
# parse_op: error cases
# ---------------------------------------------------------------------------

def test_parse_op_missing_op_key_raises():
    with pytest.raises(ValueError, match="Missing required 'op' key"):
        parse_op({"id": "f1", "new_text": "something"})


def test_parse_op_unknown_kind_raises():
    with pytest.raises(ValueError, match="Unknown operation kind"):
        parse_op({"op": "DESTROY", "id": "f1"})


def test_parse_op_not_a_dict_raises():
    with pytest.raises(TypeError, match="Expected dict"):
        parse_op(["not", "a", "dict"])  # type: ignore[arg-type]


def test_parse_op_string_raises():
    with pytest.raises(TypeError, match="Expected dict"):
        parse_op("KEEP")  # type: ignore[arg-type]


def test_parse_op_missing_subject_still_parses():
    """parse_op does not enforce payload shape — that's the executor's job.

    An op with just {"op": "UPDATE"} is structurally valid at the parse
    layer; the executor will skip it because id/new_text are missing.
    """
    op = parse_op({"op": "UPDATE"})
    assert op.kind == OpKind.UPDATE
    assert op.fact_id is None


# ---------------------------------------------------------------------------
# parse_op: case insensitivity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_kind,expected", [
    ("keep", OpKind.KEEP),
    ("Keep", OpKind.KEEP),
    ("KEEP", OpKind.KEEP),
    ("update", OpKind.UPDATE),
    ("Update", OpKind.UPDATE),
    ("merge", OpKind.MERGE),
    ("Supersede", OpKind.SUPERSEDE),
    ("archive", OpKind.ARCHIVE),
    ("retract", OpKind.RETRACT),
])
def test_parse_op_case_insensitive(raw_kind, expected):
    op = parse_op({"op": raw_kind, "id": "f1"})
    assert op.kind == expected


# ---------------------------------------------------------------------------
# parse_op: extra keys preserved
# ---------------------------------------------------------------------------

def test_parse_op_extra_keys_in_payload_preserved():
    raw = {"op": "UPDATE", "id": "f1", "new_text": "new", "custom_field": 42, "tags": ["a", "b"]}
    op = parse_op(raw)
    assert op.payload["custom_field"] == 42
    assert op.payload["tags"] == ["a", "b"]
    # "op" key should NOT be in payload
    assert "op" not in op.payload


# ---------------------------------------------------------------------------
# ProposalOp: frozen immutability
# ---------------------------------------------------------------------------

def test_proposal_op_is_frozen():
    op = ProposalOp(kind=OpKind.KEEP)
    with pytest.raises(AttributeError):
        op.kind = OpKind.UPDATE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProposalOp: convenience accessors
# ---------------------------------------------------------------------------

def test_fact_id_accessor():
    op = ProposalOp(kind=OpKind.UPDATE, payload={"id": "abc"})
    assert op.fact_id == "abc"


def test_fact_id_accessor_missing():
    op = ProposalOp(kind=OpKind.KEEP)
    assert op.fact_id is None


def test_fact_ids_accessor():
    op = ProposalOp(kind=OpKind.MERGE, payload={"ids": ["a", "b", "c"]})
    assert op.fact_ids == ["a", "b", "c"]


def test_fact_ids_accessor_missing():
    op = ProposalOp(kind=OpKind.UPDATE, payload={"id": "x"})
    assert op.fact_ids == []


# ---------------------------------------------------------------------------
# OpKind: StrEnum behaviour
# ---------------------------------------------------------------------------

def test_opkind_is_str():
    """OpKind members should be usable as plain strings."""
    assert OpKind.KEEP == "KEEP"
    assert str(OpKind.UPDATE) == "UPDATE"
    assert f"{OpKind.MERGE}" == "MERGE"
