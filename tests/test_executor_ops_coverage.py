"""Branch coverage for the deterministic executor's op types (#311).

Assertion philosophy (load-bearing — #311 said tests that pin current behavior
are "worse than no test"):

  * Assert the **documented contract and observable structure** — the `stats`
    counters, whether a fact is still a live/active bullet, whether an
    audit-trail entry exists in the `-history.md` sibling, and cross-op outcomes.
  * Do **not** assert incidental output formatting — the exact tombstone labels
    (`[superseded …]`, `[RETRACTED …]`, `Archived: …`), the `~~…~~` strikethrough
    markup, or the generated id schemes (`merged-<id>`, `supersedes-<id>`).
    Pinning those turns a harmless format refactor into a red suite.
  * Where the docstring/spec and the code disagree, assert the **documented**
    behavior and `xfail` (non-strict) the gap, so the discrepancy is surfaced,
    not encoded.

ARCHIVE caveat: the remove-the-line (current impl) vs. flag-`status: archived`
in-place (PROGRAM.md §"Never hard-delete") question — and whether an archived
fact is suppressed from default recall — is **unresolved (#485)**. ARCHIVE tests
below assert only the settled contract (the op runs + the audit trail is
preserved) and are explicitly flagged where they characterize current,
#485-dependent behavior. They must be re-pinned to the documented contract when
#485 lands.

Pure file-mutation layer — no DB, no Ollama.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from palinode.consolidation.executor import apply_operations

ZERO_STATS = {
    "kept": 0,
    "updated": 0,
    "merged": 0,
    "superseded": 0,
    "archived": 0,
    "retracted": 0,
    "merge_rejected": 0,
    "protected_rejected": 0,
    "contradicts_proposed": 0,
}

# The seed facts, as their plain *active* bullet lines. Asserting one of these is
# absent means "this fact is no longer a live/active fact" without coupling to
# how the executor tombstones or removes it.
F1_ACTIVE = "- [2024-01-01] First project fact <!-- fact:f1 -->"
F2_ACTIVE = "- [2024-01-02] Second project fact <!-- fact:f2 -->"
F3_ACTIVE = "- [2024-01-03] Third project fact <!-- fact:f3 -->"
DUP_F1_ACTIVE = "- [2024-01-04] Duplicate first fact text <!-- fact:f1 -->"


def _write_memory(tmp_path: Path, body: str, *, filename: str = "project-alpha.md") -> Path:
    path = tmp_path / filename
    path.write_text(
        f"""---
id: project-alpha
category: project
---

# Project Alpha

{body}
""",
        encoding="utf-8",
    )
    return path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _history_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}-history.md")


def _read_history(path: Path) -> str:
    return _history_path(path).read_text(encoding="utf-8")


@pytest.fixture()
def memory_file(tmp_path: Path) -> Path:
    return _write_memory(
        tmp_path,
        """- [2024-01-01] First project fact <!-- fact:f1 -->
- [2024-01-02] Second project fact <!-- fact:f2 -->
- [2024-01-03] Third project fact <!-- fact:f3 -->
- [2024-01-04] Duplicate first fact text <!-- fact:f1 -->
""",
    )


# ── UPDATE ───────────────────────────────────────────────────────────────────

def test_update_changes_only_first_matching_fact_and_preserves_id(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [{"op": "UPDATE", "id": "f1", "new_text": "- [2024-01-05] Updated first fact"}],
    )

    content = _read(memory_file)
    assert stats["updated"] == 1
    # Contract: the new text replaces the first match, the id is preserved, and
    # only the first matching fact changes (the duplicate f1 is untouched).
    assert "Updated first fact" in content
    assert "<!-- fact:f1 -->" in content
    assert F1_ACTIVE not in content          # first match replaced
    assert DUP_F1_ACTIVE in content          # second match untouched


# ── KEEP / no-op ─────────────────────────────────────────────────────────────

def test_keep_counts_without_mutating_even_when_id_is_missing(memory_file: Path) -> None:
    before = _read(memory_file)
    stats = apply_operations(str(memory_file), [{"op": "KEEP", "id": "missing"}])

    assert stats["kept"] == 1
    assert _read(memory_file) == before
    assert not _history_path(memory_file).exists()


@pytest.mark.parametrize(
    ("operation", "counter"),
    [
        ({"op": "UPDATE", "id": "missing", "new_text": "- Replacement"}, "updated"),
        ({"op": "MERGE", "ids": ["missing", "f2"], "new_text": "- Merged"}, "merged"),
        ({"op": "SUPERSEDE", "id": "missing", "new_text": "- Replacement"}, "superseded"),
        ({"op": "ARCHIVE", "id": "missing", "reason": "not found"}, "archived"),
        ({"op": "RETRACT", "id": "missing", "reason": "not found"}, "retracted"),
    ],
)
def test_id_not_found_operations_are_silent_noops(
    memory_file: Path,
    operation: dict[str, object],
    counter: str,
) -> None:
    before = _read(memory_file)
    stats = apply_operations(str(memory_file), [operation])

    assert stats[counter] == 0
    assert _read(memory_file) == before
    assert not _history_path(memory_file).exists()


# ── MERGE ────────────────────────────────────────────────────────────────────

def test_merge_with_missing_later_source_keeps_unrelated_facts(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [{"op": "MERGE", "ids": ["f2", "missing"], "new_text": "- [2024-01-02] Merged second"}],
    )

    content = _read(memory_file)
    assert stats["merged"] == 1
    # Contract: the merged text is present; the found source is consumed; the
    # unrelated facts survive. (The generated merged-id scheme is impl detail —
    # not asserted.)
    assert "Merged second" in content
    assert F2_ACTIVE not in content
    assert F3_ACTIVE in content
    assert F1_ACTIVE in content


def test_merge_with_missing_first_source_is_whole_op_noop(memory_file: Path) -> None:
    before = _read(memory_file)

    stats = apply_operations(
        str(memory_file),
        [{"op": "MERGE", "ids": ["missing", "f2"], "new_text": "- [2024-01-02] Merged second"}],
    )

    assert stats["merged"] == 0
    assert _read(memory_file) == before
    assert F2_ACTIVE in _read(memory_file)


def test_merge_removes_all_found_sources_and_keeps_unrelated_facts(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [
            {
                "op": "MERGE",
                "ids": ["f1", "f2", "f3"],
                "new_text": "- [2024-01-01] Consolidated project facts",
            }
        ],
    )

    content = _read(memory_file)
    assert stats["merged"] == 1
    assert "Consolidated project facts" in content
    # All named source facts are consumed; the unrelated duplicate-f1 survives
    # (it shares the f1 id but is a different line, not one of the merged facts).
    assert F1_ACTIVE not in content
    assert "<!-- fact:f2 -->" not in content
    assert "<!-- fact:f3 -->" not in content
    assert DUP_F1_ACTIVE in content


# ── SUPERSEDE ────────────────────────────────────────────────────────────────

def test_supersede_demotes_old_fact_adds_replacement_and_records_history(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [
            {
                "op": "SUPERSEDE",
                "id": "f2",
                "new_text": "- [2024-01-06] Replacement second fact",
                "reason": "newer status",
            }
        ],
    )

    content = _read(memory_file)
    assert stats["superseded"] == 1
    # Contract: the old fact is no longer a live/active fact, the replacement is
    # present, and the supersession (old fact + reason) is in the audit trail.
    assert F2_ACTIVE not in content
    assert "Replacement second fact" in content
    # The audit trail records the supersession of fact f2 with its reason.
    history = _read_history(memory_file)
    assert "newer status" in history       # the reason
    assert "fact:f2" in history            # the superseded fact id
    # Not asserted: ~~strikethrough~~, the "[superseded …]" label, the
    # generated "supersedes-f2" id — all incidental formatting.


def test_supersede_missing_id_returns_before_history_append(memory_file: Path) -> None:
    before = _read(memory_file)

    stats = apply_operations(
        str(memory_file),
        [{"op": "SUPERSEDE", "id": "missing", "new_text": "- [2024-01-06] Replacement"}],
    )

    assert stats["superseded"] == 0
    assert _read(memory_file) == before
    assert not _history_path(memory_file).exists()


# ── ARCHIVE (#485 — semantics unresolved; assert only the settled contract) ──

def test_archive_runs_and_preserves_audit_trail(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [{"op": "ARCHIVE", "id": "f2", "reason": "aged out"}],
    )

    assert stats["archived"] == 1
    # Settled contract (both #485 resolutions agree): the audit trail is
    # preserved — PROGRAM.md "Never hard-delete … the audit trail matters".
    history = _read_history(memory_file)
    assert "Second project fact" in history
    assert "aged out" in history
    # NOT asserted: whether the fact line is removed from the source file
    # (current impl) or flagged `status: archived` in place, and whether the
    # archived fact is suppressed from default recall — UNRESOLVED, see #485.
    # Re-pin to the documented contract once #485 lands.


def test_archive_is_idempotent_for_already_archived_fact(memory_file: Path) -> None:
    first = apply_operations(str(memory_file), [{"op": "ARCHIVE", "id": "f2"}])
    after_first = _read(memory_file)
    history_after_first = _read_history(memory_file)

    second = apply_operations(str(memory_file), [{"op": "ARCHIVE", "id": "f2"}])

    # Contract (mechanism-independent): re-archiving an already-archived fact is
    # a no-op — the second op changes neither the file nor the history.
    assert first["archived"] == 1
    assert second["archived"] == 0
    assert _read(memory_file) == after_first
    assert _read_history(memory_file) == history_after_first


def test_history_append_prepends_archived_frontmatter_to_legacy_plain_file(memory_file: Path) -> None:
    history_path = _history_path(memory_file)
    history_path.write_text("# History\n\n- legacy entry\n", encoding="utf-8")

    stats = apply_operations(
        str(memory_file),
        [{"op": "ARCHIVE", "id": "f2", "reason": "aged out"}],
    )

    history = _read(history_path)
    assert stats["archived"] == 1
    assert history.startswith("---\ncategory: history\ncore: false\nstatus: archived\n---\n\n")
    assert "- legacy entry" in history
    assert "aged out" in history


def test_history_append_respects_existing_status_frontmatter(memory_file: Path) -> None:
    history_path = _history_path(memory_file)
    history_path.write_text(
        "---\ncategory: history\nstatus: superseded\n---\n\n# History\n\n- legacy entry\n",
        encoding="utf-8",
    )

    stats = apply_operations(
        str(memory_file),
        [{"op": "ARCHIVE", "id": "f2", "reason": "aged out"}],
    )

    history = _read(history_path)
    assert stats["archived"] == 1
    assert "status: superseded" in history
    assert "status: archived" not in history
    assert "- legacy entry" in history
    assert "aged out" in history


def test_history_append_injects_archived_status_into_legacy_frontmatter(memory_file: Path) -> None:
    history_path = _history_path(memory_file)
    history_path.write_text(
        "---\ncategory: history\ncore: false\n---\n\n# History\n\n- legacy entry\n",
        encoding="utf-8",
    )

    stats = apply_operations(
        str(memory_file),
        [{"op": "ARCHIVE", "id": "f2", "reason": "aged out"}],
    )

    history = _read(history_path)
    assert stats["archived"] == 1
    assert "---\ncategory: history\ncore: false\nstatus: archived\n---" in history
    assert "- legacy entry" in history
    assert "aged out" in history


# ── RETRACT ──────────────────────────────────────────────────────────────────

def test_retract_demotes_fact_with_reason_and_records_history(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [{"op": "RETRACT", "id": "f2", "reason": "never happened"}],
    )

    content = _read(memory_file)
    assert stats["retracted"] == 1
    # Contract: the fact is no longer a live/active fact and the retraction
    # (reason) is recorded in the audit trail.
    assert F2_ACTIVE not in content
    # The audit trail records the retraction of fact f2 with its reason.
    history = _read_history(memory_file)
    assert "never happened" in history     # the reason
    assert "fact:f2" in history            # the retracted fact id
    # Not asserted: ~~strikethrough~~ / "[RETRACTED …]" label formatting.


def test_retract_without_reason_still_demotes_and_records(memory_file: Path) -> None:
    stats = apply_operations(str(memory_file), [{"op": "RETRACT", "id": "f2"}])

    content = _read(memory_file)
    assert stats["retracted"] == 1
    assert F2_ACTIVE not in content
    assert "fact:f2" in _read_history(memory_file)  # retraction recorded


# ── malformed / validation gaps ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "operation",
    [
        {"op": "UPDATE", "id": "f1", "new_text": ""},
        {"op": "MERGE", "ids": ["f1", "f2"], "new_text": ""},
        {"op": "SUPERSEDE", "id": "f1", "new_text": ""},
        {"op": "ARCHIVE"},
        {"op": "RETRACT"},
    ],
)
def test_malformed_or_incomplete_operations_are_skipped(
    memory_file: Path,
    operation: dict[str, object],
) -> None:
    before = _read(memory_file)
    stats = apply_operations(str(memory_file), [operation])

    assert stats == ZERO_STATS
    assert _read(memory_file) == before
    assert not _history_path(memory_file).exists()


# The next three document validation gaps the executor docstring implies but the
# code does not enforce — exactly the discrepancies #311 wanted SURFACED.
# strict=False (not strict=True): when the gap is later fixed the test simply
# XPASSes instead of breaking CI.

@pytest.mark.xfail(
    reason="Docstring requires an `op` key, but a missing op currently defaults to KEEP (#311 gap).",
    strict=False,
)
def test_missing_op_key_is_malformed_and_skipped(memory_file: Path) -> None:
    before = _read(memory_file)
    stats = apply_operations(str(memory_file), [{"id": "f1"}])

    assert stats == ZERO_STATS
    assert _read(memory_file) == before


def test_non_string_op_is_malformed_and_skipped(memory_file: Path) -> None:
    # #555: op_kind() coerces with str() before .upper(), so a non-string op no
    # longer crashes — it's stringified ("42"), matches no known op type, and is
    # skipped as a no-op. Closes the #311 gap this previously xfailed on.
    before = _read(memory_file)
    stats = apply_operations(str(memory_file), [{"op": 42, "id": "f1"}])

    assert stats == ZERO_STATS
    assert _read(memory_file) == before


@pytest.mark.xfail(
    reason="MERGE `ids` is documented as a list, but a tuple is currently accepted (#311 gap).",
    strict=False,
)
def test_merge_ids_must_be_a_list(memory_file: Path) -> None:
    before = _read(memory_file)
    stats = apply_operations(
        str(memory_file),
        [{"op": "MERGE", "ids": ("f2", "f3"), "new_text": "- [2024-01-01] Bad merge"}],
    )

    assert stats == ZERO_STATS
    assert _read(memory_file) == before


def test_invalid_fact_ids_field_is_ignored_for_single_id_ops(memory_file: Path) -> None:
    before = _read(memory_file)
    stats = apply_operations(str(memory_file), [{"op": "UPDATE", "fact_ids": "f1"}])

    assert stats == ZERO_STATS
    assert _read(memory_file) == before


def test_unknown_op_string_is_skipped_without_mutating(memory_file: Path) -> None:
    before = _read(memory_file)
    stats = apply_operations(str(memory_file), [{"op": "UNKNOWN", "id": "f1"}])

    assert stats == ZERO_STATS
    assert _read(memory_file) == before


# ── update_policy: replace guard (ADR-015 §2.2 / #476) ───────────────────────

def test_replace_policy_rejects_history_forking_ops(tmp_path: Path) -> None:
    path = tmp_path / "replace-doc.md"
    path.write_text(
        """---
id: living-doc
category: project
update_policy: replace
---

# Living Doc

- [2024-01-01] Current living fact <!-- fact:f1 -->
""",
        encoding="utf-8",
    )
    before = _read(path)

    stats = apply_operations(
        str(path),
        [
            {"op": "SUPERSEDE", "id": "f1", "new_text": "- [2024-01-02] Forked fact"},
            {"op": "ARCHIVE", "id": "f1", "reason": "stale"},
        ],
    )

    # History-forking ops are refused on a living (replace) doc; nothing mutates.
    assert stats["superseded"] == 0
    assert stats["archived"] == 0
    assert stats["protected_rejected"] == 2
    assert _read(path) == before
    assert not _history_path(path).exists()


def test_replace_policy_rejects_retract_h3_without_history_fork(tmp_path: Path) -> None:
    path = tmp_path / "replace-doc.md"
    path.write_text(
        """---
id: living-doc
category: project
update_policy: replace
---

# Living Doc

### Current State

- [2024-01-01] Current living fact <!-- fact:f1 -->
""",
        encoding="utf-8",
    )
    before = _read(path)

    stats = apply_operations(
        str(path),
        [{"op": "RETRACT", "id": "f1", "reason": "wrong current state"}],
    )

    assert stats["retracted"] == 0
    assert stats["protected_rejected"] == 1
    assert _read(path) == before
    assert not _history_path(path).exists()


# ── cross-op ordering within one batch ───────────────────────────────────────

def test_archive_then_update_same_batch_does_not_resurrect_fact(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [
            {"op": "ARCHIVE", "id": "f2", "reason": "stale"},
            {"op": "UPDATE", "id": "f2", "new_text": "- [2024-01-07] Should not return"},
        ],
    )

    # Settled contract: the archive runs and the later UPDATE's text never
    # becomes a live fact. (Whether UPDATE no-ops because the line was removed,
    # or because an archived fact is not updatable, is #485-dependent — assert
    # the observable outcome, not the mechanism.)
    assert stats["archived"] == 1
    assert "Should not return" not in _read(memory_file)


def test_update_then_archive_same_batch_archives_updated_text(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [
            {"op": "UPDATE", "id": "f2", "new_text": "- [2024-01-07] Updated before archive"},
            {"op": "ARCHIVE", "id": "f2", "reason": "then archived"},
        ],
    )

    # Contract: the UPDATE lands first, then the (updated) fact is archived — the
    # audit trail carries the updated text + the archive reason.
    assert stats["updated"] == 1
    assert stats["archived"] == 1
    history = _read_history(memory_file)
    assert "Updated before archive" in history
    assert "then archived" in history


def test_retract_then_update_same_batch_corrects_the_fact(memory_file: Path) -> None:
    stats = apply_operations(
        str(memory_file),
        [
            {"op": "RETRACT", "id": "f2", "reason": "wrong"},
            {"op": "UPDATE", "id": "f2", "new_text": "- [2024-01-07] Corrected after retract"},
        ],
    )

    content = _read(memory_file)
    # Contract: a retract followed by an update of the same id yields a live,
    # corrected fact (UPDATE operates in place on the still-present, tombstoned
    # line), and the retraction is recorded.
    assert stats["retracted"] == 1
    assert stats["updated"] == 1
    assert "Corrected after retract" in content
    assert "fact:f2" in _read_history(memory_file)  # the retraction was recorded


def test_merge_retires_old_source_ids(memory_file: Path) -> None:
    # MERGE consumes its source ids; a later UPDATE addressed to a consumed
    # source id no longer matches. The merged fact is addressable under a new id
    # (the executor derives it as ``merged-<first-source-id>`` — that scheme is
    # impl detail, exercised here only to show the *old* id was retired).
    stats = apply_operations(
        str(memory_file),
        [
            {"op": "MERGE", "ids": ["f2", "f3"], "new_text": "- [2024-01-02] Merged fact"},
            {"op": "UPDATE", "id": "f2", "new_text": "- [2024-01-08] Old id update"},
            {"op": "UPDATE", "id": "merged-f2", "new_text": "- [2024-01-08] New id update"},
        ],
    )

    content = _read(memory_file)
    assert stats["merged"] == 1
    assert stats["updated"] == 1          # only the new-id update matched
    assert "New id update" in content
    assert "Old id update" not in content  # the consumed f2 id is retired
    assert "<!-- fact:f3 -->" not in content


# ── nightly MERGE policy ─────────────────────────────────────────────────────

def test_nightly_merge_rejects_empty_ids(memory_file: Path) -> None:
    before = _read(memory_file)
    stats = apply_operations(
        str(memory_file),
        [{"op": "MERGE", "ids": [], "new_text": "- [2024-01-01] Empty merge"}],
        nightly_policy=True,
    )

    assert stats == ZERO_STATS
    assert _read(memory_file) == before


@pytest.mark.xfail(
    reason="MERGE `ids` is documented as a list, but a tuple is currently accepted under nightly policy (#311 gap).",
    strict=False,
)
def test_nightly_merge_rejects_malformed_id_list_without_crashing(memory_file: Path) -> None:
    before = _read(memory_file)
    stats = apply_operations(
        str(memory_file),
        [{"op": "MERGE", "ids": ("f2", "f3"), "new_text": "- [2024-01-01] Bad merge"}],
        nightly_policy=True,
    )

    assert stats == ZERO_STATS
    assert _read(memory_file) == before
