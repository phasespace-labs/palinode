"""Guard: unfollowable issue refs must not appear in examples/.

The ``examples/`` directory contains non-Python collateral — Markdown files,
shell scripts, JSON snippets, and plain-text notes — that ship verbatim to the
public repository. Before this guard existed, these files sat outside every
mechanical gate: the source-comment check only tokenises ``.py``, the root-
config check covers a short allowlist of root-level dotfiles, and the CI-
workflow check is scoped to ``.github/workflows/``.

Issue refs in example files are doubly problematic: they appear exactly where
a reader trying to reproduce an example is most likely to copy the text, and
they resolve to the wrong tracker issue in the public mirror.

**Scope decisions**

*   ``.sh``, ``.json``, ``.txt``, and ``.md`` files are all in scope — each is
    read as plain text, line by line, identical to how
    ``test_no_issue_refs_in_root_config`` scans ``.gitattributes``.

*   ``.md`` files **are** in scope.  The existing ``_ROOT_CONFIG_FILES``
    allowlist excludes ``.md`` explicitly because ``docs/CHANGELOG.md`` cites
    issues on purpose.  ``examples/`` is different: these files are usage guides
    and never need bare issue numbers for a reader's benefit.  Including them
    keeps the guarantee consistent across all example collateral.

*   Recursion is intentional — ``examples/`` contains several sub-directories
    (``compaction-demo/``, ``decisions/``, ``hooks/``, ``insights/``,
    ``people/``, ``projects/``, ``sample-memory/``) and future additions should
    be covered automatically.

**Reuse**

The ``_issue_refs`` helper and both compiled patterns are imported from the
sibling guard module rather than reimplemented here, so this test inherits
exactly the same accept/reject semantics: bare ``#NNN`` fails, full public-repo
URLs pass, foreign-repo URLs fail.
"""
from __future__ import annotations


from pathlib import Path



# ---------------------------------------------------------------------------
# Re-use the identical helper and patterns from the existing guard module.
# This ensures the same semantics: bare #NNN fails; public palinode URLs pass;
# foreign-repo URLs fail.  We import the compiled patterns directly to avoid
# accidental divergence from a copy-pasted regex.
# ---------------------------------------------------------------------------
from tests.test_no_issue_refs_user_surface import (

    _issue_refs,
)

# ---------------------------------------------------------------------------
# File extensions that are scanned in examples/.
#
# .sh   — shell scripts (e.g. hooks examples)
# .json — JSON config snippets
# .txt  — plain-text notes
# .md   — Markdown guides (see scope decision in module docstring)
#
# .py files inside examples/ would already be caught by the source-comment
# and string-constant guards; they are not excluded here, just unlikely.
# ---------------------------------------------------------------------------
_EXAMPLES_EXTENSIONS = {".sh", ".json", ".txt", ".md"}

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLES_DIR = _REPO_ROOT / "examples"


def _examples_files() -> list[Path]:
    """All scanned files under examples/, sorted for stable output."""
    if not _EXAMPLES_DIR.exists():
        return []
    return sorted(
        p
        for p in _EXAMPLES_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in _EXAMPLES_EXTENSIONS
    )


def _scan_file(path: Path) -> list[tuple[int, str, list[str]]]:
    """(line_number, raw_line, refs) for every offending line in *path*."""
    found: list[tuple[int, str, list[str]]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):  # pragma: no cover
        return found
    for i, line in enumerate(text.splitlines(), 1):
        refs = _issue_refs(line)
        if refs:
            found.append((i, line.rstrip(), refs))
    return found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_issue_refs_in_examples() -> None:
    """No file under examples/ may carry an unfollowable issue reference.

    Scans ``.sh``, ``.json``, ``.txt``, and ``.md`` files line-by-line,
    reusing the same ``_issue_refs`` helper that guards CLI help text,
    Python comments, docstrings, and CI workflows.

    To reference a real issue, use the full public URL::

        https://github.com/phasespace-labs/palinode/issues/<N>

    That form passes the guard and can be followed by any public reader.
    """
    offenders: list[str] = []
    for path in _examples_files():
        rel = path.relative_to(_REPO_ROOT)
        for lineno, raw, refs in _scan_file(path):
            offenders.append(
                f"  {rel}:{lineno}: {refs}  →  {raw[:80]}"
            )

    assert not offenders, (
        "Unfollowable issue refs found in examples/. Replace the bare number "
        "with the full public issue URL, or name the change instead:\n"
        "  https://github.com/phasespace-labs/palinode/issues/<N>\n"
        + "\n".join(offenders)
    )


def test_examples_dir_is_scanned() -> None:
    """Sanity: the examples/ directory exists and contains scannable files.

    This prevents the guard from silently passing when the directory is
    renamed or removed — a vacuously true test is worse than no test.
    """
    files = _examples_files()
    assert files, (
        f"No scannable files found under {_EXAMPLES_DIR}. "
        "Either the directory was removed or _EXAMPLES_EXTENSIONS needs updating."
    )


# ---------------------------------------------------------------------------
# Negative-control (unit) tests — same taxonomy as the sibling module
# ---------------------------------------------------------------------------


def test_bare_number_rejected_in_examples_context() -> None:
    """A bare ``#NNN`` tag is unfollowable and must fail the guard."""
    ref = "#" + "224"
    line = f"# workaround for {ref}"
    assert _issue_refs(line) == [ref]


def test_public_url_allowed_in_examples_context() -> None:
    """The full public-repo URL is the correct escape hatch and must pass.

    Contributors documenting a workaround in an example script should cite the
    full URL so a public reader can follow it directly.
    """
    url = "https://github.com/phasespace-labs/palinode/issues/224"
    line = f"# workaround for {url}"
    assert _issue_refs(line) == [], (
        f"Expected full public URL to pass the guard, but got: {_issue_refs(line)}"
    )


def test_foreign_url_rejected_in_examples_context() -> None:
    """A full URL pointing at a *different* repo is also rejected.

    This mirrors the semantics of the sibling guard: a foreign URL is
    worse than a bare tag because it contains no ``#``, bypassing the
    bare-tag pattern while still being unfollowable in the public mirror.
    """
    url = "https://github.com/some-owner/some-private-repo/issues/" + "224"
    line = f"# context: {url}"
    assert _issue_refs(line) == [url]


def test_public_url_does_not_mask_bare_ref_beside_it_in_examples() -> None:
    """Stripping the permitted URL must not swallow an offender next to it."""
    ref = "#" + "224"
    line = (
        "# https://github.com/phasespace-labs/palinode/issues/100 "
        f"and also {ref}"
    )
    assert _issue_refs(line) == [ref]


def test_adr_refs_are_not_flagged() -> None:
    """``ADR-NNN`` strings are never flagged (mirrors the comment-scrub policy)."""
    line = "# Superseded by ADR-018 — see examples/decisions/adr-018.md"
    assert _issue_refs(line) == []


def test_plain_hash_in_shell_comment_is_not_flagged() -> None:
    """A ``#`` that is not followed by two or more digits must not fire.

    Shell scripts begin every comment line with ``#`` and often use single-
    character or non-numeric suffixes (``#!``, ``# set -e``, etc.).
    """
    for line in (
        "#!/usr/bin/env bash",
        "# set -euo pipefail",
        "# one digit: #1 should not match",
        "echo 'hello'  # no number here",
    ):
        assert _issue_refs(line) == [], (
            f"Unexpected match in non-issue shell fragment: {line!r}"
        )
