"""Collection integrity — no test may go missing without saying so (#356).

#356 was filed on a smoke run that collected 1081 unit tests on the LXC rig
against an MBP baseline of 1215, and read that 134-test delta as tests silently
uncollected on Linux. The delta was a numerator mismatch (the rig runs unit-only,
the baseline number included ``tests/integration/``), not a collection failure —
ubuntu CI and macOS collect the same suite item-for-item. But the underlying
worry is real and cheap to close permanently: *silent* non-collection is
indistinguishable from a green run.

These checks make it loud. The invariant is derived from the tree rather than
recorded as a baseline number, so it costs nothing to maintain:

1. Every ``tests/test_*.py`` on disk contributes at least one collected item.
   A module that stops being collected — renamed, import-erroring, gated behind
   ``pytest.skip(allow_module_level=True)``, dropped by an ``--ignore`` — fails
   here by name instead of quietly shrinking the count.
2. Every module collects **at least as many items as it declares** test
   functions, counted with pytest's own naming rules. This is the "minimum
   collected count" guard, but expressed per module and re-derived from the AST
   on every run: adding or deleting tests moves the floor automatically, and a
   platform-conditional class or function body that swallows tests trips it.
3. Collection itself exits clean — no collection errors hiding behind a pass.

A test that genuinely cannot run somewhere must be an explicit ``skipif`` (which
still collects, and reports with ``-rs``). Skipping is fine; disappearing is not.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

import palinode

REPO_ROOT = Path(palinode.__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

# Matches the CI / smoke-rig unit invocation: tests/ minus integration and live.
COLLECT_ARGS = [
    "--collect-only",
    "-q",
    "-p",
    "no:cacheprovider",
    "--ignore=tests/integration",
    "--ignore=tests/live",
    "tests",
]


def _declared_test_count(path: Path) -> int:
    """Count test items a module declares, using pytest's default naming rules.

    Module-level ``test*`` functions plus ``test*`` methods of ``Test*`` classes
    (pytest skips classes with an ``__init__``). Parametrization only ever
    multiplies this, so it is a true lower bound on the collected count.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                count += 1
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            if any(
                isinstance(member, ast.FunctionDef) and member.name == "__init__"
                for member in node.body
            ):
                continue
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if member.name.startswith("test"):
                        count += 1
    return count


@pytest.fixture(scope="module")
def collection() -> tuple[int, Counter[str], str]:
    """Run ``pytest --collect-only`` in a subprocess; return rc, per-file counts, output.

    A subprocess (rather than in-process collection) so the result reflects the
    canonical unit-test invocation regardless of how *this* run was invoked —
    ``pytest tests/test_collection_integrity.py`` must check the same thing the
    full suite does.
    """
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *COLLECT_ARGS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    per_file: Counter[str] = Counter(
        line.split("::", 1)[0].replace(os.sep, "/")
        for line in proc.stdout.splitlines()
        if "::" in line
    )
    return proc.returncode, per_file, proc.stdout + proc.stderr


def test_collection_exits_clean(collection: tuple[int, Counter[str], str]) -> None:
    returncode, per_file, output = collection
    assert returncode == 0, f"pytest --collect-only exited {returncode}:\n{output[-4000:]}"
    # The summary line is where pytest reports collection errors
    # ("N tests collected, M errors"); the node-ID lines above it contain the
    # word "error" all over the place.
    summary = next(
        (line for line in reversed(output.splitlines()) if "collected" in line), ""
    )
    assert "error" not in summary.lower(), f"collection reported errors: {summary!r}"
    assert sum(per_file.values()) > 0, "collected nothing at all"


def test_no_unit_test_module_is_silently_uncollected(
    collection: tuple[int, Counter[str], str],
) -> None:
    _, per_file, output = collection
    on_disk = {f"tests/{p.name}" for p in TESTS_DIR.glob("test_*.py")}
    missing = sorted(on_disk - set(per_file))
    assert not missing, (
        "test module(s) on disk contributed zero collected items — a silent "
        f"non-collection, which is exactly the #356 failure mode: {missing}\n"
        f"{output[-2000:]}"
    )


def test_every_module_collects_at_least_the_tests_it_declares(
    collection: tuple[int, Counter[str], str],
) -> None:
    _, per_file, _ = collection
    shortfalls = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        rel = f"tests/{path.name}"
        declared = _declared_test_count(path)
        if per_file[rel] < declared:
            shortfalls[rel] = (declared, per_file[rel])
    assert not shortfalls, (
        "module(s) collected fewer items than they declare test functions "
        f"(declared, collected): {shortfalls}"
    )
