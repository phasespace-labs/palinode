"""Executor guard: never SUPERSEDE/ARCHIVE a living (update_policy: replace) doc.

ADR-015 §2.2 / #431 §3: a memory declaring ``update_policy: replace`` is a
living/current-state document. Consolidation may UPDATE it in place but must
NEVER SUPERSEDE it (which strikes through the fact and forks a
``supersedes-`` sibling) or ARCHIVE-into-history it — either forks the single
current fact into a stale historical snapshot, the exact failure the axis
exists to prevent.

A normal episodic doc (no update_policy, or update_policy: append) must still
supersede/archive as before — the guard is a no-op for it.

Plain tempfile + frontmatter on disk; no DB, no Ollama (the executor is a pure
file-mutation layer).
"""
import os
import tempfile

import pytest

from palinode.consolidation.executor import apply_operations


def _write(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


_REPLACE_DOC = """---
id: inbox-uptime-kuma
category: inbox
type: ActionItem
update_policy: replace
status: open
---

# Uptime Kuma

- [2026-05-30] uptime-kuma is DOWN (HTTP 502) <!-- fact:f1 -->
"""

_EPISODIC_DOC = """---
id: project-alpha
category: project
type: ProjectSnapshot
---

# Project Alpha

- [2024-01-01] The project started today <!-- fact:f1 -->
"""


@pytest.fixture()
def replace_doc():
    path = _write(_REPLACE_DOC)
    yield path
    for p in (path, path.replace(".md", "-history.md")):
        if os.path.exists(p):
            os.remove(p)


@pytest.fixture()
def episodic_doc():
    path = _write(_EPISODIC_DOC)
    yield path
    for p in (path, path.replace(".md", "-history.md")):
        if os.path.exists(p):
            os.remove(p)


# ── (c) SUPERSEDE refused on replace-doc, allowed on episodic doc ────────────

def test_supersede_refused_on_replace_doc(replace_doc):
    ops = [{
        "op": "SUPERSEDE",
        "id": "f1",
        "new_text": "- [2026-05-31] uptime-kuma is UP",
        "reason": "recovered",
    }]
    stats = apply_operations(replace_doc, ops)

    assert stats["superseded"] == 0
    assert stats["protected_rejected"] == 1

    with open(replace_doc) as f:
        content = f.read()
    # Original fact untouched: no strikethrough, no supersedes- sibling.
    assert "~~" not in content
    assert "supersedes-f1" not in content
    assert "uptime-kuma is DOWN (HTTP 502) <!-- fact:f1 -->" in content
    # No history file forked.
    assert not os.path.exists(replace_doc.replace(".md", "-history.md"))


def test_archive_refused_on_replace_doc(replace_doc):
    ops = [{"op": "ARCHIVE", "id": "f1", "reason": "stale"}]
    stats = apply_operations(replace_doc, ops)

    assert stats["archived"] == 0
    assert stats["protected_rejected"] == 1

    with open(replace_doc) as f:
        content = f.read()
    # Fact still present — not removed into history.
    assert "uptime-kuma is DOWN (HTTP 502) <!-- fact:f1 -->" in content
    assert not os.path.exists(replace_doc.replace(".md", "-history.md"))


def test_supersede_still_works_on_episodic_doc(episodic_doc):
    """Regression guard: a normal episodic doc supersedes exactly as before."""
    ops = [{
        "op": "SUPERSEDE",
        "id": "f1",
        "new_text": "- [2024-01-04] The project was restarted",
        "reason": "Change of plans",
    }]
    stats = apply_operations(episodic_doc, ops)

    assert stats["superseded"] == 1
    assert stats["protected_rejected"] == 0

    with open(episodic_doc) as f:
        content = f.read()
    assert "~~[2024-01-01] The project started today~~" in content
    assert "The project was restarted <!-- fact:supersedes-f1 -->" in content
    assert os.path.exists(episodic_doc.replace(".md", "-history.md"))


def test_archive_still_works_on_episodic_doc(episodic_doc):
    ops = [{"op": "ARCHIVE", "id": "f1", "reason": "No longer relevant"}]
    stats = apply_operations(episodic_doc, ops)

    assert stats["archived"] == 1
    assert stats["protected_rejected"] == 0

    with open(episodic_doc) as f:
        content = f.read()
    assert "The project started today" not in content
    assert os.path.exists(episodic_doc.replace(".md", "-history.md"))


def test_update_still_allowed_on_replace_doc(replace_doc):
    """A replace doc is a *living* document: UPDATE-in-place must still work —
    only the history-forking ops (SUPERSEDE/ARCHIVE) are guarded."""
    ops = [{
        "op": "UPDATE",
        "id": "f1",
        "new_text": "- [2026-05-31] uptime-kuma is UP (HTTP 200)",
    }]
    stats = apply_operations(replace_doc, ops)

    assert stats["updated"] == 1
    assert stats["protected_rejected"] == 0

    with open(replace_doc) as f:
        content = f.read()
    assert "uptime-kuma is UP (HTTP 200) <!-- fact:f1 -->" in content
    assert "is DOWN (HTTP 502)" not in content


# ── (H3) RETRACT refused on replace-doc — it forks history too ───────────────

def test_retract_refused_on_replace_doc(replace_doc):
    """H3: RETRACT strikethrough-tombstones the fact in place AND appends a
    ``-history.md`` sibling — history-forking on a living doc, the exact thing
    the guard forbids. It must be rejected like SUPERSEDE/ARCHIVE, not allowed.
    A provably-wrong value in a living doc is corrected with UPDATE instead."""
    ops = [{
        "op": "RETRACT",
        "id": "f1",
        "reason": "probe was misconfigured; never actually down",
    }]
    stats = apply_operations(replace_doc, ops)

    assert stats["retracted"] == 0
    assert stats["protected_rejected"] == 1

    with open(replace_doc) as f:
        content = f.read()
    # Original fact untouched: no strikethrough tombstone in the living doc.
    assert "~~" not in content
    assert "[RETRACTED" not in content
    # No history sibling forked.
    assert not os.path.exists(replace_doc.replace(".md", "-history.md"))


def test_retract_allowed_on_episodic_doc(episodic_doc):
    """Parity: RETRACT still works on a normal episodic doc — the guard is a
    no-op there (tombstone + history fork are correct for episodic memory)."""
    ops = [{"op": "RETRACT", "id": "f1", "reason": "was wrong"}]
    stats = apply_operations(episodic_doc, ops)

    assert stats["retracted"] == 1
    assert stats["protected_rejected"] == 0
    with open(episodic_doc) as f:
        content = f.read()
    assert "[RETRACTED" in content
