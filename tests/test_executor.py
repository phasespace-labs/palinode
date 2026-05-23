import os
import tempfile
import pytest
import palinode.consolidation.executor as executor_module
from palinode.consolidation.executor import apply_operations, _nightly_merge_allowed
from palinode.consolidation.proposal import OpKind, ProposalOp

@pytest.fixture
def temp_memory_file():
    content = """---
id: project-alpha
category: project
---

# Project Alpha

- [2024-01-01] The project started today <!-- fact:f1 -->
- [2024-01-02] An update occurred <!-- fact:f2 -->
- [2024-01-03] Another update <!-- fact:f3 -->
"""
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    yield path
    os.remove(path)

def test_keep_operation(temp_memory_file):
    ops = [ProposalOp(kind=OpKind.KEEP, payload={"id": "f1"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["kept"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "The project started today <!-- fact:f1 -->" in content

def test_update_operation(temp_memory_file):
    ops = [ProposalOp(kind=OpKind.UPDATE, payload={"id": "f2", "new_text": "- [2024-01-02] A significant update occurred"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["updated"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "A significant update occurred <!-- fact:f2 -->" in content
    assert "An update occurred" not in content

def test_merge_operation(temp_memory_file):
    ops = [ProposalOp(kind=OpKind.MERGE, payload={"ids": ["f2", "f3"], "new_text": "- [2024-01-02] Important combined updates"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["merged"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "Important combined updates <!-- fact:merged-f2 -->" in content
    assert "<!-- fact:f3 -->" not in content

def test_supersede_operation(temp_memory_file):
    ops = [ProposalOp(kind=OpKind.SUPERSEDE, payload={"id": "f1", "new_text": "- [2024-01-04] The project was restarted", "reason": "Change of plans"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["superseded"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "~~[2024-01-01] The project started today~~" in content
    assert "The project was restarted <!-- fact:supersedes-f1 -->" in content

    # Check history file
    history_file = temp_memory_file.replace(".md", "-history.md")
    assert os.path.exists(history_file)
    with open(history_file) as f:
        hist = f.read()
    assert "Superseded" in hist
    os.remove(history_file)

def test_archive_operation(temp_memory_file):
    ops = [ProposalOp(kind=OpKind.ARCHIVE, payload={"id": "f2", "reason": "No longer relevant"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["archived"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "An update occurred" not in content

    history_file = temp_memory_file.replace(".md", "-history.md")
    assert os.path.exists(history_file)
    with open(history_file) as f:
        hist = f.read()
    assert "Archived" in hist
    os.remove(history_file)

def test_missing_fields_are_skipped(temp_memory_file):
    # Missing new_text for UPDATE, missing new_text for MERGE,
    # missing id for SUPERSEDE, missing id for ARCHIVE
    stats = apply_operations(temp_memory_file, [
        ProposalOp(kind=OpKind.UPDATE, payload={"id": "f1"}),
        ProposalOp(kind=OpKind.MERGE, payload={"ids": ["f1", "f2"]}),
        ProposalOp(kind=OpKind.SUPERSEDE, payload={"new_text": "Replacement"}),
        ProposalOp(kind=OpKind.ARCHIVE, payload={}),
    ])
    assert stats == {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0, "retracted": 0, "merge_rejected": 0}

def test_missing_fact_id_is_noop(temp_memory_file):
    stats = apply_operations(temp_memory_file, [ProposalOp(kind=OpKind.SUPERSEDE, payload={"id": "missing", "new_text": "Replacement"})])
    assert stats["superseded"] == 0
    assert not os.path.exists(temp_memory_file.replace(".md", "-history.md"))

def test_empty_operations_leave_file_unchanged(temp_memory_file):
    with open(temp_memory_file) as f:
        before = f.read()
    stats = apply_operations(temp_memory_file, [])
    with open(temp_memory_file) as f:
        after = f.read()
    assert before == after
    assert stats == {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0, "retracted": 0, "merge_rejected": 0}


def test_atomic_main_write_failure_preserves_original_file(temp_memory_file, monkeypatch):
    with open(temp_memory_file) as f:
        before = f.read()

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(executor_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        apply_operations(
            temp_memory_file,
            [
                ProposalOp(
                    kind=OpKind.UPDATE,
                    payload={
                        "id": "f2",
                        "new_text": "- [2024-01-02] A significant update occurred",
                    },
                )
            ],
        )

    with open(temp_memory_file) as f:
        after = f.read()

    assert after == before
    temp_prefix = f".{os.path.basename(temp_memory_file)}."
    leftovers = [
        name
        for name in os.listdir(os.path.dirname(temp_memory_file))
        if name.startswith(temp_prefix)
    ]
    assert leftovers == []


def test_atomic_history_write_failure_preserves_original_file(temp_memory_file, monkeypatch):
    with open(temp_memory_file) as f:
        before = f.read()

    history_file = temp_memory_file.replace(".md", "-history.md")
    original_replace = executor_module.os.replace

    def fail_history_replace(src, dst):
        if dst == history_file:
            raise OSError("history replace failed")
        return original_replace(src, dst)

    monkeypatch.setattr(executor_module.os, "replace", fail_history_replace)

    with pytest.raises(OSError, match="history replace failed"):
        apply_operations(
            temp_memory_file,
            [
                ProposalOp(
                    kind=OpKind.SUPERSEDE,
                    payload={
                        "id": "f1",
                        "new_text": "- [2024-01-04] The project was restarted",
                        "reason": "Change of plans",
                    },
                )
            ],
        )

    with open(temp_memory_file) as f:
        after = f.read()

    assert after == before
    assert not os.path.exists(history_file)
    temp_prefixes = {
        f".{os.path.basename(temp_memory_file)}.",
        f".{os.path.basename(history_file)}.",
    }
    leftovers = [
        name
        for name in os.listdir(os.path.dirname(temp_memory_file))
        if any(name.startswith(prefix) for prefix in temp_prefixes)
    ]
    assert leftovers == []


def test_retract_operation(temp_memory_file):
    """RETRACT leaves a visible tombstone with strikethrough and reason."""
    ops = [ProposalOp(kind=OpKind.RETRACT, payload={"id": "f2", "reason": "This was never true"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["retracted"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    # Fact should be struck through with RETRACTED label
    assert "~~[2024-01-02] An update occurred~~" in content
    assert "[RETRACTED" in content
    assert "This was never true" in content
    # Fact ID should still be present (tombstone, not deleted)
    assert "<!-- fact:f2 -->" in content

    # Check history file
    history_file = temp_memory_file.replace(".md", "-history.md")
    assert os.path.exists(history_file)
    with open(history_file) as f:
        hist = f.read()
    assert "Retracted" in hist
    assert "This was never true" in hist
    os.remove(history_file)


def test_retract_without_reason(temp_memory_file):
    """RETRACT should work even without a reason."""
    ops = [ProposalOp(kind=OpKind.RETRACT, payload={"id": "f1"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["retracted"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "~~[2024-01-01] The project started today~~" in content
    assert "[RETRACTED" in content

    history_file = temp_memory_file.replace(".md", "-history.md")
    if os.path.exists(history_file):
        os.remove(history_file)


def test_retract_missing_fact_is_noop(temp_memory_file):
    """RETRACT on a non-existent fact ID should be a no-op."""
    ops = [ProposalOp(kind=OpKind.RETRACT, payload={"id": "nonexistent", "reason": "test"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["retracted"] == 0


def test_retract_missing_id_is_skipped(temp_memory_file):
    """RETRACT without an ID field should be skipped."""
    ops = [ProposalOp(kind=OpKind.RETRACT, payload={"reason": "no id"})]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["retracted"] == 0


# ---------------------------------------------------------------------------
# Nightly MERGE policy tests (#202)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_same_day_file():
    """Two facts on the same date — nightly MERGE should be allowed."""
    content = """---
id: project-beta
category: project
---

# Project Beta

- [2026-04-28] Morning session note <!-- fact:s1 -->
- [2026-04-28] Afternoon session note <!-- fact:s2 -->
- [2026-04-27] Yesterday's note <!-- fact:s3 -->
"""
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    yield path
    os.remove(path)


def test_nightly_merge_accepts_same_day(temp_same_day_file):
    """nightly_policy=True: MERGE of same-date facts is allowed."""
    ops = [ProposalOp(kind=OpKind.MERGE, payload={"ids": ["s1", "s2"], "new_text": "[2026-04-28] Combined daily note"})]
    stats = apply_operations(temp_same_day_file, ops, nightly_policy=True)
    assert stats["merged"] == 1
    assert stats["merge_rejected"] == 0
    with open(temp_same_day_file) as f:
        content = f.read()
    assert "Combined daily note" in content
    # s2 should be gone after the merge
    assert "<!-- fact:s2 -->" not in content


def test_nightly_merge_rejects_cross_date(temp_same_day_file):
    """nightly_policy=True: MERGE spanning different dates is rejected."""
    ops = [ProposalOp(kind=OpKind.MERGE, payload={"ids": ["s1", "s3"], "new_text": "[2026-04-28] Cross-date merged"})]
    stats = apply_operations(temp_same_day_file, ops, nightly_policy=True)
    assert stats["merged"] == 0
    assert stats["merge_rejected"] == 1
    # File content should be unchanged
    with open(temp_same_day_file) as f:
        content = f.read()
    assert "Morning session note" in content
    assert "Yesterday's note" in content


def test_nightly_merge_rejects_undated_fact(temp_same_day_file):
    """nightly_policy=True: MERGE involving a fact without a date is rejected."""
    # Patch in an undated fact
    with open(temp_same_day_file, "a") as f:
        f.write("- Undated note <!-- fact:s4 -->\n")
    ops = [ProposalOp(kind=OpKind.MERGE, payload={"ids": ["s1", "s4"], "new_text": "[2026-04-28] Merged"})]
    stats = apply_operations(temp_same_day_file, ops, nightly_policy=True)
    assert stats["merged"] == 0
    assert stats["merge_rejected"] == 1


def test_nightly_policy_false_allows_cross_date_merge(temp_same_day_file):
    """Without nightly_policy, cross-date MERGE goes through (weekly pass behaviour)."""
    ops = [ProposalOp(kind=OpKind.MERGE, payload={"ids": ["s1", "s3"], "new_text": "[2026-04-28] Cross-date merged"})]
    stats = apply_operations(temp_same_day_file, ops, nightly_policy=False)
    assert stats["merged"] == 1
    assert stats["merge_rejected"] == 0


def test_nightly_merge_allowed_helper_same_day():
    """Unit test for _nightly_merge_allowed: returns True for same-date facts."""
    content = (
        "- [2026-04-28] Note A <!-- fact:a -->\n"
        "- [2026-04-28] Note B <!-- fact:b -->\n"
    )
    assert _nightly_merge_allowed(content, ["a", "b"]) is True


def test_nightly_merge_allowed_helper_cross_date():
    """Unit test for _nightly_merge_allowed: returns False for cross-date facts."""
    content = (
        "- [2026-04-28] Note A <!-- fact:a -->\n"
        "- [2026-04-27] Note B <!-- fact:b -->\n"
    )
    assert _nightly_merge_allowed(content, ["a", "b"]) is False


def test_nightly_merge_allowed_helper_missing_fact():
    """Unit test for _nightly_merge_allowed: returns False when a fact ID is absent."""
    content = "- [2026-04-28] Note A <!-- fact:a -->\n"
    assert _nightly_merge_allowed(content, ["a", "missing"]) is False
