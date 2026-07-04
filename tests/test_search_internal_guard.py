"""Tests for the search_internal() guard and the bare-search lint net (ADR-015 H1, #481).

Two components are verified here:

1. ``store.search_internal()`` structural guard — a thin wrapper around
   ``store.search()`` with ``record_access`` hard-set to ``False`` and absent
   from its signature.  Internal / maintenance callers (consolidation dedup,
   embedding-candidate pipelines) must never accidentally record recall stats.

2. Grep-guard test — catches any *new* bare ``store.search(`` call landing in a
   non-user-facing module without ``record_access=False``.  The allowlist below
   identifies the modules whose bare calls are legitimate (user-facing recorders
   or the internal search_hybrid inner pass that carries its own explicit
   ``record_access=False``).  Any unlisted module with a bare ``store.search(``
   fails the guard.

Design note: the grep guard and the structural helper are complementary.
``search_internal()`` is the *right* API for new internal callers; the grep
guard is the *net* that flags regressions where someone accidentally uses the
raw API in a maintenance context.  Together they make H1 ratchet-tight.
"""
from __future__ import annotations

import ast
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from palinode.core import store
from palinode.core.config import config

# ---------------------------------------------------------------------------
# Helpers shared with test_recall_feedback.py
# ---------------------------------------------------------------------------

EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _embedding(seed: int) -> list[float]:
    vec = [0.0] * EMBED_DIM
    vec[seed % EMBED_DIM] = 0.9
    vec[(seed * 7 + 3) % EMBED_DIM] = 0.4
    return _normalize(vec)


def _index_chunk(*, chunk_id: str, file_path: str, content: str, seed: int) -> None:
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    store.upsert_chunks([{
        "id": chunk_id,
        "file_path": file_path,
        "section_id": None,
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": now_iso,
        "last_updated": now_iso,
        "embedding": _embedding(seed),
    }])


def _meta(chunk_id: str) -> tuple[int, str | None, float | None]:
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT recall_count, last_recalled, importance FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
    finally:
        db.close()
    return row["recall_count"], row["last_recalled"], row["importance"]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    store._db_checked = False
    for d in ("insights",):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)
    store.init_db()
    yield memory_dir
    store._db_checked = False


# ---------------------------------------------------------------------------
# 1. search_internal() structural guard
# ---------------------------------------------------------------------------

class TestSearchInternalNeverRecords:
    """search_internal() returns hits but never writes recall stats."""

    def test_returns_hits_without_recording(self, _isolated_env):
        """search_internal returns matching results AND leaves recall stats untouched."""
        _index_chunk(chunk_id="internal-hit", file_path="insights/ih.md",
                     content="alpha topic", seed=1)

        results = store.search_internal(query_embedding=_embedding(1), threshold=0.5)
        assert any(r["id"] == "internal-hit" for r in results), \
            "search_internal returned no results — should find the indexed chunk"

        rc, lr, imp = _meta("internal-hit")
        assert rc == 0, \
            f"search_internal polluted recall_count={rc} (H1 regression)"
        assert lr is None, \
            "search_internal stamped last_recalled (H1 regression)"
        assert imp == pytest.approx(0.5), \
            f"search_internal nudged importance to {imp} (H1 regression)"

    def test_record_access_not_in_signature(self):
        """search_internal's signature must not expose record_access — callers
        cannot accidentally pass True and re-introduce pollution."""
        import inspect
        sig = inspect.signature(store.search_internal)
        assert "record_access" not in sig.parameters, (
            "search_internal exposes record_access — callers can override the guard"
        )

    def test_normal_search_still_records(self, _isolated_env):
        """Regression guard: the plain search() path still records recall
        (search_internal must not break the user-facing path)."""
        _index_chunk(chunk_id="user-hit", file_path="insights/uh.md",
                     content="beta topic", seed=2)

        store.search(query_embedding=_embedding(2), threshold=0.5)

        rc, lr, _ = _meta("user-hit")
        assert rc == 1, "store.search() no longer records recall (regression)"
        assert lr is not None

    def test_search_internal_passes_kwargs(self, _isolated_env):
        """search_internal forwards top_k and threshold to the underlying search."""
        _index_chunk(chunk_id="int-kwargs", file_path="insights/ik.md",
                     content="gamma topic", seed=3)

        # top_k=1 — should cap at one result even if multiple chunks present
        results = store.search_internal(query_embedding=_embedding(3),
                                        threshold=0.5, top_k=1)
        # Result count is at most top_k
        assert len(results) <= 1
        # No recall recorded
        rc, lr, _ = _meta("int-kwargs")
        assert rc == 0, "search_internal(top_k=1) still recorded recall (H1)"


# ---------------------------------------------------------------------------
# 2. Grep-guard: no new bare store.search() in non-user-facing modules
# ---------------------------------------------------------------------------

# Repo root — resolve relative to this test file's location.
_REPO_ROOT = Path(__file__).parent.parent

# Modules whose bare ``store.search(`` calls are expected and legitimate.
# Each entry is a path RELATIVE to the repo root, using forward slashes.
# Only ``palinode/`` source files are scanned; tests and artifacts are excluded.
_ALLOWLIST: frozenset[str] = frozenset({
    # Public search surface — these callers intentionally record recall.
    # The /search handler and its candidate-list helper moved out of server.py
    # into the routers/ package during the router split (stage 1); the
    # bare store.search() call now lives in routers/search.py and the comment
    # that references it in api/search_helpers.py (the _shared.py successor).
    "palinode/api/routers/search.py",   # /search endpoint (moved from server.py)
    "palinode/api/search_helpers.py",  # comment referencing store.search (moved)
    "palinode/mcp.py",             # MCP palinode_search tool
    "palinode/cli/search.py",      # CLI palinode search
    # store.py contains search_hybrid's inner pass with explicit record_access=False.
    "palinode/core/store.py",
})

# Pattern: a bare ``store.search(`` that is NOT the search_internal call or
# the search_hybrid inner pass (which both carry explicit record_access=False).
# We flag lines in non-allowlisted modules that contain ``store.search(``
# without ``record_access=False`` on the same line.
_BARE_SEARCH_RE = re.compile(r'\bstore\.search\(')
_HAS_RECORD_FALSE_RE = re.compile(r'record_access\s*=\s*False')


def _iter_python_files(root: Path):
    """Yield .py files under *root/palinode/* only.

    We scope the guard to the shipping source tree (``palinode/``) — tests and
    internal artifacts are excluded because:
    - ``tests/`` calls ``store.search()`` intentionally to exercise the API.
    - ``artifacts/`` are historical snapshots of old code (pre-search_internal).
    Neither should be gate-broken by this guard.
    """
    for path in (root / "palinode").rglob("*.py"):
        parts = path.parts
        if any(p in (".venv", "venv", "__pycache__", ".git") for p in parts):
            continue
        yield path


class TestBareSearchCallGuard:
    """No bare store.search() call may appear in a non-allowlisted module
    without ``record_access=False`` on the same line.

    This guard is the regression net: if someone adds a new maintenance caller
    using the raw store.search() API, this test fails and directs them to use
    store.search_internal() instead.
    """

    def test_no_unlisted_bare_store_search(self):
        violations: list[str] = []

        for py_file in _iter_python_files(_REPO_ROOT):
            rel = py_file.relative_to(_REPO_ROOT)
            rel_str = str(rel)

            if rel_str in _ALLOWLIST:
                continue

            try:
                lines = py_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue

            for lineno, line in enumerate(lines, start=1):
                if _BARE_SEARCH_RE.search(line):
                    # A bare store.search( call — check for the opt-out.
                    if not _HAS_RECORD_FALSE_RE.search(line):
                        violations.append(
                            f"{rel_str}:{lineno}: bare store.search() without "
                            f"record_access=False — use store.search_internal() "
                            f"for maintenance callers, or add to the allowlist "
                            f"if this is a user-facing recorder."
                        )

        assert not violations, (
            "Bare store.search() calls detected in non-allowlisted modules:\n"
            + "\n".join(violations)
            + "\n\nFix: use store.search_internal() for internal / maintenance "
            "callers (ADR-015 H1, #481), or add the module to _ALLOWLIST if it "
            "is genuinely user-facing and already records recall intentionally."
        )

    def test_guard_detects_planted_violation(self, tmp_path):
        """Self-check: the grep guard correctly identifies a planted violation."""
        bad_file = tmp_path / "fake_maintenance.py"
        bad_file.write_text(
            "from palinode.core import store\n"
            "def do_thing(emb):\n"
            "    return store.search(emb, top_k=5)\n",
            encoding="utf-8",
        )

        violations = []
        lines = bad_file.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _BARE_SEARCH_RE.search(line) and not _HAS_RECORD_FALSE_RE.search(line):
                violations.append(f"{bad_file}:{lineno}")

        assert violations, \
            "Guard failed to detect a planted bare store.search() violation"

    def test_guard_accepts_search_internal(self, tmp_path):
        """A file using store.search_internal() passes the guard."""
        ok_file = tmp_path / "ok_maintenance.py"
        ok_file.write_text(
            "from palinode.core import store\n"
            "def do_thing(emb):\n"
            "    return store.search_internal(emb, top_k=5)\n",
            encoding="utf-8",
        )

        violations = []
        lines = ok_file.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _BARE_SEARCH_RE.search(line) and not _HAS_RECORD_FALSE_RE.search(line):
                violations.append(f"{ok_file}:{lineno}")

        assert not violations, \
            "Guard incorrectly flagged a store.search_internal() call"

    def test_guard_accepts_explicit_record_access_false(self, tmp_path):
        """A bare store.search() with record_access=False on the same line passes."""
        ok_file = tmp_path / "ok_explicit.py"
        ok_file.write_text(
            "from palinode.core import store\n"
            "def inner(emb):\n"
            "    return store.search(emb, record_access=False)\n",
            encoding="utf-8",
        )

        violations = []
        lines = ok_file.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _BARE_SEARCH_RE.search(line) and not _HAS_RECORD_FALSE_RE.search(line):
                violations.append(f"{ok_file}:{lineno}")

        assert not violations, \
            "Guard incorrectly flagged a store.search() with explicit record_access=False"
