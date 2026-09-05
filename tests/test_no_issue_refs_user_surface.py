"""Guard: unfollowable issue refs must not appear in shipping source.

Palinode is developed in a private repo whose issue numbers are meaningful, then
synced to a public repo where the same `#NNN` resolves to a different (or
nonexistent) issue. Guarded here: CLI ``--help`` output, MCP tool/param schema
descriptions, Python comments, docstrings, CI workflow comments, and a short
list of root config files. Each is enumerated live and fails the build.

**A bare `#NNN` is the problem, not the reference.** It is unfollowable — the
reader cannot tell which tracker it belongs to, and in the public repo it
silently resolves to the wrong issue. A reference qualified with the full
public-repo URL is fine and always has been the intended escape hatch; cite
provenance that way rather than deleting it. See ``_issue_refs``.

That distinction matters for contributors working *in* the public repo, where
citing a public issue by number is a natural and correct-looking thing to do.
The guard cannot tell those apart from a bare number, so it asks for the URL.

`ADR-` string refs are always fine (mirrors the scrub policy: ADR refs in
comments ship, only ADR `.md` files don't). Shipped docs (docs/*.md) are still
not scanned — `docs/CHANGELOG.md` cites issues on purpose.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import click
import pytest

from palinode.cli import main as cli_root

# A bare issue reference: `#` followed by 2–4 digits (a bare tag).
# Two digits min avoids matching enumerations like "fix" / "ranks"; four
# max avoids long digit runs that aren't issue numbers. (This comment is kept
# ref-free on purpose so the source-comment guard below doesn't flag itself.)
_ISSUE_REF = re.compile(r"#\d{2,4}\b")

# A reference qualified to the PUBLIC repository. These are allowed: a public
# reader can follow them, and they cannot be mistaken for a different tracker's
# numbering. This is the escape hatch for genuine provenance — cite the full
# URL rather than a bare number.
_PUBLIC_ISSUE_URL = re.compile(
    r"https://github\.com/phasespace-labs/palinode/(?:issues|pull)/\d+"
)

# A reference qualified to some OTHER repository. Rejected, and worth rejecting
# explicitly: these contain no `#`, so the bare-tag pattern above never saw
# them. A full dev-tracker URL used to pass this guard untouched, which is a
# worse leak than the bare number it was written to catch.
_FOREIGN_ISSUE_URL = re.compile(
    r"https?://(?:www\.)?github\.com/(?!phasespace-labs/palinode/)"
    r"[\w.-]+/[\w.-]+/(?:issues|pull)/\d+"
)


def _issue_refs(text: str) -> list[str]:
    """Every unfollowable issue reference in *text*.

    Public-repo URLs are removed first, so they neither match nor mask a bare
    tag elsewhere in the same string. What remains is reported: bare `#NNN`
    tags, and issue URLs pointing at any other repository.
    """
    remaining = _PUBLIC_ISSUE_URL.sub("", text)
    return _ISSUE_REF.findall(remaining) + _FOREIGN_ISSUE_URL.findall(remaining)


# Shipping Python roots whose COMMENT tokens must stay issue-ref-free.
_SOURCE_ROOTS = ("palinode", "tests", "bench")


def _iter_commands(
    node: click.Command, path: str = ""
) -> list[tuple[str, click.Command]]:
    """Flatten the Click command tree into ``(dotted_path, command)`` pairs."""
    here = f"{path} {node.name}".strip()
    found = [(here, node)]
    if isinstance(node, click.Group):
        for sub in node.commands.values():
            found.extend(_iter_commands(sub, here))
    return found


def _cli_help_strings() -> list[tuple[str, str]]:
    """Every user-visible help string in the CLI: command help + option help.

    Returns ``(location_label, text)`` pairs so a failure names exactly where
    the ref lives.
    """
    strings: list[tuple[str, str]] = []
    for cmd_path, cmd in _iter_commands(cli_root):
        if cmd.help:
            strings.append((f"cmd {cmd_path} (help)", cmd.help))
        if getattr(cmd, "short_help", None):
            strings.append((f"cmd {cmd_path} (short_help)", cmd.short_help))
        for param in cmd.params:
            help_text = getattr(param, "help", None)
            if help_text:
                strings.append((f"cmd {cmd_path} --{param.name} (help)", help_text))
    return strings


def test_no_issue_refs_in_cli_help() -> None:
    """No CLI ``--help`` text may carry an unfollowable issue reference."""
    offenders = [
        (loc, _issue_refs(text))
        for loc, text in _cli_help_strings()
        if _issue_refs(text)
    ]
    assert not offenders, (
        "Unfollowable issue refs found in user-visible CLI help. Replace the bare "
        "number with the full public issue URL, or name the change instead:\n"
        + "\n".join(f"  {loc}: {refs}" for loc, refs in offenders)
    )


@pytest.mark.asyncio
async def test_no_issue_refs_in_mcp_tool_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No MCP tool description or param description may contain an issue ref.

    Runs against the full surface so no tool is skipped.
    """
    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    from palinode.mcp import list_tools

    offenders: list[tuple[str, list[str]]] = []
    for tool in await list_tools():
        if tool.description and _issue_refs(tool.description):
            offenders.append(
                (f"tool {tool.name} (description)", _issue_refs(tool.description))
            )
        props = (tool.input_schema or {}).get("properties", {}) or {}
        for pname, prop in props.items():
            desc = prop.get("description") if isinstance(prop, dict) else None
            if desc and _issue_refs(desc):
                offenders.append(
                    (f"tool {tool.name}.{pname} (description)", _issue_refs(desc))
                )

    assert not offenders, (
        "Unfollowable issue refs found in user-visible MCP schema text. Replace the "
        "bare number with the full public issue URL, or name the change instead:\n"
        + "\n".join(f"  {loc}: {refs}" for loc, refs in offenders)
    )


def _comment_refs_in(path: Path) -> list[tuple[int, str]]:
    """Return ``(line, comment)`` for every COMMENT token carrying an issue ref.

    Uses ``tokenize`` so only comment text is inspected — never code or
    string/docstring literals (those are out of the comment-scrub scope).
    """
    src = path.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and _issue_refs(tok.string):
                found.append((tok.start[0], tok.string.strip()))
    except tokenize.TokenError:
        pass
    return found


def test_no_issue_refs_in_source_comments() -> None:
    """No Python COMMENT in the shipping source may carry an unfollowable ref.

    Locks in the comment-scrub baseline: a bare tag in a comment is recurring
    public-sync noise, so it stays at zero. Docstrings/strings are excluded.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for root in _SOURCE_ROOTS:
        for py in sorted((repo_root / root).rglob("*.py")):
            for line, text in _comment_refs_in(py):
                rel = py.relative_to(repo_root)
                offenders.append(f"  {rel}:{line}: {_issue_refs(text)}  →  {text[:80]}")

    assert not offenders, (
        "Unfollowable issue refs found in source comments. Replace the bare number "
        "with the full public issue URL, or name the change instead:\n" + "\n".join(offenders)
    )


#: Root config files that ship and where an issue ref is never product
#: vocabulary. Deliberately a short allowlist rather than "all non-Python
#: shipping files": `docs/CHANGELOG.md` cites issues on purpose (they are how a
#: release note identifies its change), `pyproject.toml` carries one dependency
#: rationale whose ref is load-bearing context for a version cap, and the CI
#: workflows plus ~150 module docstrings are a much larger scrub decision that
#: has not been made. See the enforcement issue for that measurement.
_ROOT_CONFIG_FILES = (".gitattributes", ".gitignore")


def test_no_issue_refs_in_root_config() -> None:
    """Extend the rule to the root config files that ship.

    The issue-ref scrub scoped itself to Python *comments*, so an unfollowable ref in a
    non-Python config file was outside every mechanical gate — content scrub
    looks for secret patterns, path scrub looks at filenames, and this test
    only parsed `.py`. A v0.9.6 sync found exactly that: `.gitattributes`
    carried "Verified <date> on PR #NNN" and a "#NNN branch" reference, which
    only a human reading the payload caught.

    These files are small, rarely edited, and never reference an issue for a
    reader's benefit — which makes them cheap to hold at zero, unlike the
    broader surface.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for name in _ROOT_CONFIG_FILES:
        path = repo_root / name
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _issue_refs(line):
                offenders.append(f"  {name}:{i}: {_issue_refs(line)}  →  {line[:80]}")

    assert not offenders, (
        "Unfollowable issue refs found in shipping root config. These reach the "
        "public payload and no mechanical gate sees them — use the full public "
        "issue URL, or rewrite the line to describe the situation instead:\n"
        + "\n".join(offenders)
    )


def _docstring_refs_in(path: Path) -> list[tuple[int, str]]:
    """``(line, docstring)`` for every module/class/function docstring with a ref."""
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover — a broken file fails elsewhere
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        text = ast.get_docstring(node)
        if text and _issue_refs(text):
            found.append((getattr(node, "lineno", 1), text))
    return found


def test_no_issue_refs_in_docstrings() -> None:
    """Docstrings carry descriptions, not issue numbers.

    The original scrub scoped itself to Python comments and left docstrings
    alone, on the reasoning that the refs were genuine provenance. They are —
    which is why the fix was to *convert* rather than delete: every `#NNN`
    became a phrase naming what it pointed at, so the sentence keeps its
    referent while the private number stays out of the public payload.

    That only holds if it is enforced. A reintroduced ref is not a leak of a
    secret, but it is a pointer a public reader cannot follow, and it
    reaccumulates one docstring at a time.

    To add provenance, name the thing — "the router split" — rather than
    citing a bare issue number.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for root in _SOURCE_ROOTS:
        for py in sorted((repo_root / root).rglob("*.py")):
            for line, text in _docstring_refs_in(py):
                rel = py.relative_to(repo_root)
                offenders.append(f"  {rel}:{line}: {_issue_refs(text)}")

    assert not offenders, (
        "Unfollowable issue refs found in docstrings. A bare number cannot be "
        "followed by a public reader and may resolve to the wrong issue — use the "
        "full public issue URL, or name the change instead:\n" + "\n".join(offenders)
    )


def test_no_issue_refs_in_ci_workflows() -> None:
    """CI workflow comments ship too, and their history narrative cited issues."""
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for wf in sorted((repo_root / ".github" / "workflows").glob("*.yml")):
        for i, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            if _issue_refs(line):
                offenders.append(f"  {wf.name}:{i}: {_issue_refs(line)}  →  {line.strip()[:70]}")

    assert not offenders, (
        "Unfollowable issue refs found in CI workflow comments. Replace the bare "
        "number with the full public issue URL, or name the change instead:\n"
        + "\n".join(offenders)
    )


# ── What counts as an unfollowable reference ─────────────────────────────────
# The guards above are only as good as this distinction, and it is the part a
# contributor actually collides with, so it is pinned directly.


def test_bare_number_is_rejected() -> None:
    """The original case: a bare tag, whichever tracker the author meant."""
    assert _issue_refs("Issue #100: the body was mangled.") == ["#100"]


def test_public_url_is_allowed() -> None:
    """The escape hatch — a reference a public reader can actually follow.

    Someone working in the public repo who cites the issue they are fixing is
    doing the right thing. The guard cannot read intent from a bare tag, so
    this is the form it asks for instead of refusing provenance outright.
    """
    text = "Fixes https://github.com/phasespace-labs/palinode/issues/100 — see there."
    assert _issue_refs(text) == []


def test_url_to_another_repository_is_rejected() -> None:
    """A qualified URL to a *different* tracker is worse than a bare number.

    It carries no ``#``, so the bare-tag pattern never saw it: a full
    dev-tracker link used to pass this guard untouched, while the same issue
    written as a bare tag beside it would have failed.
    """
    text = "Context: https://github.com/some-owner/some-private-repo/issues/715"
    assert _issue_refs(text) == [
        "https://github.com/some-owner/some-private-repo/issues/715"
    ]


def test_allowed_url_does_not_mask_a_bare_ref_beside_it() -> None:
    """Stripping the permitted form must not swallow an offender next to it."""
    text = "https://github.com/phasespace-labs/palinode/issues/100 and also #715"
    assert _issue_refs(text) == ["#715"]
