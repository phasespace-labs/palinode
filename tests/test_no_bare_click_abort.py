"""Guard: no bare click.Abort() calls in palinode/ sources.

click.Abort() constructs an exception object but does not raise it unless
prefixed with `raise`. A bare `click.Abort()` is a no-op that silently
swallows the error, causing the command to exit 0 instead of non-zero.
This guard walks all tracked Python sources under palinode/ and fails the
build if any bare click.Abort() expression statements are found.

The check uses AST to avoid false positives from string literals,
comments, or the `raise click.Abort()` form that is already used
correctly throughout the codebase (e.g., in prompt.py and save.py).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "palinode"


class _BareClickAbortVisitor(ast.NodeVisitor):
    """AST visitor that finds bare click.Abort() expression statements."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.violations: list[tuple[int, str]] = []  # (line_number, source_snippet)

    def visit_Expr(self, node: ast.Expr) -> None:
        """Check expression statements for bare click.Abort() calls."""
        if isinstance(node.value, ast.Call):
            call = node.value
            # Check if it's a click.Abort() call
            if self._is_click_abort(call):
                # Get the source line for reporting
                line = node.lineno
                self.violations.append((line, "click.Abort()"))
        self.generic_visit(node)

    def _is_click_abort(self, call: ast.Call) -> bool:
        """Check if call is click.Abort()."""
        func = call.func
        if isinstance(func, ast.Attribute):
            # click.Abort()
            return (
                isinstance(func.value, ast.Name)
                and func.value.id == "click"
                and func.attr == "Abort"
            )
        elif isinstance(func, ast.Name):
            # Abort() if imported directly (unlikely but handle it)
            return func.id == "Abort"
        return False


def _find_python_files(root: Path) -> list[Path]:
    """Find all Python files under the source root."""
    return list(root.rglob("*.py"))


def _check_file(filepath: Path) -> list[tuple[int, str]]:
    """Check a single file for bare click.Abort() calls."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
    except UnicodeDecodeError:
        return []  # skip binary/unreadable files

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []  # skip files with syntax errors

    visitor = _BareClickAbortVisitor(str(filepath))
    visitor.visit(tree)
    return visitor.violations


def test_no_bare_click_abort():
    """Guard test: fail if any bare click.Abort() found in palinode/ sources."""
    all_violations: list[tuple[Path, int, str]] = []

    for py_file in _find_python_files(SOURCE_ROOT):
        violations = _check_file(py_file)
        for line, snippet in violations:
            # Report as relative path from repo root
            rel_path = py_file.relative_to(REPO_ROOT)
            all_violations.append((rel_path, line, snippet))

    if all_violations:
        msg_lines = [
            "Bare click.Abort() calls found (missing 'raise'):",
            "",
        ]
        for rel_path, line, snippet in sorted(all_violations):
            msg_lines.append(f"  {rel_path}:{line}: {snippet}")
        msg_lines.append("")
        msg_lines.append(
            "Fix: change 'click.Abort()' to 'raise click.Abort()' at each location."
        )
        pytest.fail("\n".join(msg_lines))