"""The shared "never memory" predicate, its named per-surface constants, and the
guard that keeps every other module from defining its own copy (issue 251)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import palinode
from palinode.core import skip_dirs
from palinode.core.skip_dirs import ALWAYS_SKIP, is_skipped_path


def test_always_skip_keeps_its_six_folders() -> None:
    assert ALWAYS_SKIP == frozenset(
        {"specs", "prompts", "logs", ".palinode", ".obsidian", ".git"}
    )


@pytest.mark.parametrize(
    "rel",
    [
        "specs/amr.md",
        "specs/prompts/p.md",
        "prompts/p.md",
        "logs/l.md",
        ".palinode/x.md",
        ".obsidian/o.md",
        ".git/x.md",
        "team/prompts/p.md",
    ],
)
def test_a_folder_in_always_skip_is_never_memory_at_any_depth(rel: str) -> None:
    assert is_skipped_path(rel)


@pytest.mark.parametrize(
    "rel",
    [".trash/t.md", "team/.drafts/d.md", "a/b/.hidden/c.md", ".cache/x.md"],
)
def test_a_dot_folder_is_never_memory(rel: str) -> None:
    assert is_skipped_path(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "venv/x.md",
        "node_modules/pkg/readme.md",
        "team/node_modules/x.md",
        "team/__pycache__/x.md",
        "palinode.egg-info/x.md",
        "notes/mypkg.egg-info/PKG.md",
    ],
)
def test_build_noise_is_never_memory(rel: str) -> None:
    assert is_skipped_path(rel)


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("inbox/processed/p.md", True),
        ("team/inbox/processed/p.md", True),
        ("processed/p.md", False),
        ("notes/processed/p.md", False),
        ("inbox/x/processed/p.md", False),
        ("inbox/processed.md", False),
        ("inbox/i.md", False),
    ],
)
def test_processed_is_never_memory_only_directly_under_inbox(rel: str, expected: bool) -> None:
    assert is_skipped_path(rel) is expected


@pytest.mark.parametrize(
    "rel",
    [
        "notes/a.md",
        "archive/a.md",
        "daily/2026-01-01.md",
        "inbox/i.md",
        "team/archive/n.md",
        "team/daily/n.md",
        "logs.md",
        "specs.md",
        "venv.md",
        "team/specsheet/s.md",
        "logsheet/l.md",
        "venvs/x.md",
        "node_modules_notes/x.md",
        "egg-info/x.md",
        "top.md",
    ],
)
def test_a_file_named_like_a_skipped_folder_or_a_folder_with_its_prefix_is_memory(rel: str) -> None:
    assert not is_skipped_path(rel)


def test_a_reserved_name_as_the_file_name_does_not_skip_the_file() -> None:
    assert not is_skipped_path("notes/.git")
    assert not is_skipped_path("notes/node_modules")


@pytest.mark.parametrize(
    "rel",
    ["specs\\prompts\\p.md", ".trash\\t.md", "inbox\\processed\\p.md", "team\\node_modules\\x.md"],
)
def test_the_windows_separator_matches_like_the_posix_one(rel: str) -> None:
    assert is_skipped_path(rel)


def test_extra_adds_folders_to_the_skip_set() -> None:
    assert is_skipped_path("archive/a.md", {"archive"})
    assert is_skipped_path("team/archive/n.md", frozenset({"archive"}))
    assert not is_skipped_path("archive/a.md")


@pytest.mark.parametrize("extra", [frozenset(), {"notes"}, {"specs", "logs"}, ["x"]])
def test_extra_never_removes_a_skip(extra) -> None:
    assert is_skipped_path("specs/prompts/p.md", extra)
    assert is_skipped_path(".trash/t.md", extra)
    assert is_skipped_path("inbox/processed/p.md", extra)
    assert is_skipped_path("venv/x.md", extra)


def test_a_surface_constant_names_its_extra_folders() -> None:
    assert skip_dirs.SKIP_FOR_LISTING == frozenset({"daily", "archive", "inbox"})
    assert skip_dirs.SKIP_FOR_DIGEST == frozenset({"daily", "archive"})
    assert skip_dirs.SKIP_FOR_LINT == frozenset({"archive"})
    assert skip_dirs.SKIP_FOR_TTL == frozenset({"daily"})
    assert skip_dirs.SKIP_FOR_CROSS_REFS == frozenset({"daily", "archive", "inbox"})


def test_the_index_skips_nothing_beyond_the_shared_rule() -> None:
    assert not is_skipped_path("archive/a.md")
    assert not is_skipped_path("daily/2026-01-01.md")
    assert not is_skipped_path("inbox/i.md")


_PACKAGE_ROOT = Path(palinode.__file__).parent

# Walks the source vault being imported, not the store (issue 251, out of scope).
_NOT_A_STORE_WALK = {Path("import_/vault.py")}

_SKIP_NAME = re.compile(r"skip|ignore", re.IGNORECASE)


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return []
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _holds_a_literal_collection_of_strings(value: ast.AST | None) -> bool:
    if value is None:
        return False
    for sub in ast.walk(value):
        if isinstance(sub, (ast.Set, ast.List, ast.Tuple)) and any(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in sub.elts
        ):
            return True
    return False


def _own_skip_sets() -> list[str]:
    found: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        rel = path.relative_to(_PACKAGE_ROOT)
        if rel == Path("core/skip_dirs.py") or rel in _NOT_A_STORE_WALK:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            for name in _target_names(node):
                if _SKIP_NAME.search(name) and _holds_a_literal_collection_of_strings(
                    getattr(node, "value", None)
                ):
                    found.append(f"{rel}:{node.lineno} {name}")
    return found


def test_no_module_defines_its_own_skip_set_of_folder_names() -> None:
    assert _own_skip_sets() == []


def test_the_guard_sees_a_skip_set_when_one_is_defined(tmp_path: Path, monkeypatch) -> None:
    package = tmp_path / "pkg"
    (package / "core").mkdir(parents=True)
    (package / "core" / "skip_dirs.py").write_text('ALWAYS_SKIP = {"specs"}\n')
    (package / "walker.py").write_text('def f():\n    skip_dirs = {"archive", "logs"}\n')
    (package / "clean.py").write_text("from pkg.core.skip_dirs import ALWAYS_SKIP\nX = ALWAYS_SKIP\n")
    monkeypatch.setitem(globals(), "_PACKAGE_ROOT", package)

    assert _own_skip_sets() == ["walker.py:2 skip_dirs"]
