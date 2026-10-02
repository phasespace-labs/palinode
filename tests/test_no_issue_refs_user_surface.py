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

String constants are scanned across every source root. The few literals that
must carry an issue-shaped token — the pinned fixtures a guard cannot test
without quoting the form it rejects — are declared by the test module that
holds them, in ``_ISSUE_REF_FIXTURES``, with a reason each. A declaration that
names a literal no longer present fails too. An exemption mechanism without
those rules is the hole it was meant to close.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path
from typing import NamedTuple

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


def _folded_str(node: object) -> str | None:
    """The literal value of *node* when it is a string built only from literals.

    Covers the plain constant and any ``+`` chain of them. A ref split across a
    concatenation (``"#" + "100"``) reaches the public payload exactly as a
    whole literal does; it just becomes invisible to a scan that only looks at
    single ``Constant`` nodes. Folding is what keeps the allowlist below the
    only way through.
    """
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _folded_str(node.left)
        right = _folded_str(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _string_constant_refs_in(path: Path) -> list[tuple[int, str]]:
    """(line, ref_text) for every string literal in *path* carrying an issue ref."""
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover — a broken file fails elsewhere
        return []
    found: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        text = _folded_str(node)
        if text is None or not _issue_refs(text):
            continue
        key = (getattr(node, "lineno", 1), text)
        if key in seen:  # a fold and its own operand can both match
            continue
        seen.add(key)
        found.append(key)
    return found


# ── Fixture declarations: the one way past the literal scan ──────────────────
#
# A test that pins this rule has to quote the form it rejects — the guard would
# otherwise be untestable. So a TEST module may declare its own literals, in
# itself, as ``_ISSUE_REF_FIXTURES = ((ref, reason), ...)``.
#
# Declaring in the file it covers is what makes this hold in both trees. A
# central list here would name files this one cannot assume exist: not every
# test module is published alongside it, and an entry pointing at an absent
# file matches nothing and reads as stale. Keeping each exemption with its
# literal means a file that is not there takes its declarations with it.
#
# Three rules, enforced below, because an exemption mechanism without them is
# the blind spot with a nicer name:
#
#   - only test modules may declare (production and benchmark code gets
#     reworded — a ref there is text a user reads);
#   - every entry carries a reason;
#   - every entry still matches a literal in that same file.
#
# Entries are written as plain literals on purpose. Splitting a ref across a
# concatenation (``"#" + "100"``) also gets past the scan, but it gets past by
# being invisible to it — nobody reviews what the guard never reports.

#: Module-level name a test module uses to declare its own fixture literals.
_FIXTURE_DECL = "_ISSUE_REF_FIXTURES"

#: Roots whose modules may carry a declaration at all.
_FIXTURE_DECL_ROOTS = ("tests",)

#: A reason short enough to be a shrug ("fixture", "ok") is not a reason. Long
#: enough to have named the literal and why it must stay.
_MIN_REASON_CHARS = 40

#: This module's own pinned cases — see the block comment above.
_ISSUE_REF_FIXTURES: tuple[tuple[str, str], ...] = (
    (
        "#100",
        "Pinned fixture for _issue_refs: the bare-tag case this guard rejects "
        "has to appear literally. Synthetic number, no tracker meaning.",
    ),
    (
        "#715",
        "Pinned fixture: proves that stripping the permitted public-URL form "
        "does not swallow a bare tag beside it. Synthetic number.",
    ),
    (
        "https://github.com/some-owner/some-private-repo/issues/715",
        "Pinned fixture: the foreign-tracker URL form this guard rejects. "
        "Owner and repo are placeholders, not a real tracker.",
    ),
    (
        "#404",
        "Synthetic value in this mechanism's own self-tests, which build a "
        "reasonless and a stale declaration to prove both rules fire.",
    ),
)


def _declaration_in(path: Path) -> tuple[list[tuple[str, str]], tuple[int, int] | None, list[str]]:
    """``(entries, line span, malformed complaints)`` for *path*'s declaration.

    An entry that is not a ``(ref, reason)`` pair of literals is reported
    rather than skipped: a declaration the parser cannot read is one nobody
    can review either.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover — a broken file fails elsewhere
        return [], None, []

    for node in tree.body:
        targets = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else getattr(node, "targets", [])
        )
        if not any(isinstance(t, ast.Name) and t.id == _FIXTURE_DECL for t in targets):
            continue

        span = (node.lineno, node.end_lineno or node.lineno)
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List)):
            return [], span, [f"{_FIXTURE_DECL} must be a tuple of (ref, reason) pairs"]

        entries: list[tuple[str, str]] = []
        problems: list[str] = []
        for element in value.elts:
            parts = (
                [_folded_str(e) for e in element.elts]
                if isinstance(element, (ast.Tuple, ast.List))
                else []
            )
            if len(parts) != 2 or any(p is None for p in parts):
                problems.append(
                    f"line {getattr(element, 'lineno', span[0])}: each entry must be "
                    "a (ref, reason) pair of string literals"
                )
                continue
            entries.append((parts[0], parts[1]))  # type: ignore[arg-type]
        return entries, span, problems

    return [], None, []


class _ScanResult(NamedTuple):
    """What one pass over a source tree found."""

    offenders: list[str]  #: literals carrying a ref that nothing covers
    reasonless: list[str]  #: declared entries with no usable reason
    stale: list[str]  #: declared entries matching no literal in their file
    malformed: list[str]  #: declarations the parser could not read


def _scan_source_strings(
    repo_root: Path,
    roots: tuple[str, ...],
    *,
    skip_diagnostics: bool = False,
    honor_declarations: bool = True,
) -> _ScanResult:
    """Scan *roots* under *repo_root* and apply all three declaration rules.

    Parameterised on the root so the same pass can be run against a copy of a
    published subset, not only the tree it happens to live in.

    A declared ref's own literal inside the declaration is not a hit — the
    declaration has to write each exempted token down, and it does so beside
    the reason for it. Only the exact declared strings are skipped: prose in
    the same block is still scanned, and the entry stays subject to the reason
    and staleness rules.
    """
    diagnostics = repo_root / "palinode" / "diagnostics"
    result = _ScanResult([], [], [], [])

    for root in roots:
        for py in sorted((repo_root / root).rglob("*.py")):
            if skip_diagnostics and py.is_relative_to(diagnostics):
                continue
            rel = py.relative_to(repo_root).as_posix()
            may_declare = rel.startswith(tuple(f"{r}/" for r in _FIXTURE_DECL_ROOTS))
            entries, span, problems = _declaration_in(py)
            result.malformed.extend(f"  {rel}: {p}" for p in problems)

            if entries and not (may_declare and honor_declarations):
                result.malformed.append(
                    f"  {rel}: declares {_FIXTURE_DECL}, which only test modules may do"
                )
                entries = []

            declared = {ref for ref, _reason in entries}
            live: set[str] = set()
            for line, text in _string_constant_refs_in(py):
                in_declaration = span is not None and span[0] <= line <= span[1]
                if in_declaration and text in declared:
                    continue
                refs = _issue_refs(text)
                if not in_declaration:
                    live.update(refs)
                offending = [r for r in refs if r not in declared]
                if offending:
                    result.offenders.append(
                        f"  {rel}:{line}: {offending}  →  {text.strip()[:70]}"
                    )

            for ref, reason in entries:
                if len(reason.strip()) < _MIN_REASON_CHARS:
                    result.reasonless.append(f"  {rel}: {ref}: reason={reason.strip()!r}")
                if ref not in live:
                    result.stale.append(f"  {rel}: {ref}")

    return result


def test_no_issue_refs_in_package_strings() -> None:
    """String constants in ``palinode/`` must not carry bare issue refs.

    This started scoped to ``palinode/diagnostics/``, where remediation text is
    printed straight at the user during a health check. That is the loudest
    case, not the only one: an assertion message, a CLI warning, and the
    ``/wrap`` command body rendered into a user's repo are all string constants
    outside ``diagnostics/`` that a reader sees and cannot follow.

    The package keeps its own failure message because a ref here is printed at
    a user, not read by a contributor — the companion below covers the rest of
    the source roots without diluting that.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders = _scan_source_strings(repo_root, ("palinode",)).offenders

    assert not offenders, (
        "Unfollowable issue refs found in package string constants. A bare "
        "number cannot be followed by a public reader — use the full public issue "
        "URL, or name the change instead:\n" + "\n".join(offenders)
    )


def test_no_issue_refs_in_source_string_constants() -> None:
    """Every shipping source root gets the literal check, not just diagnostics.

    Diagnostics keeps its dedicated failure above because its remediation text
    is directly user-facing. This companion closes the equivalent gap in
    production code, fixtures, and benchmark harnesses without diluting that
    diagnostic-specific explanation: an ``xfail`` reason, a bench constant, a
    fixture's project name are all literals that reach the public payload with
    every other gate green.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders = _scan_source_strings(
        repo_root, _SOURCE_ROOTS, skip_diagnostics=True
    ).offenders

    assert not offenders, (
        "Unfollowable issue refs found in source string constants. Replace the "
        "bare number with the full public issue URL, name the change instead, or "
        "— only in a test module, and only when the literal is the subject of "
        f"the test — declare it in that file's {_FIXTURE_DECL} with a "
        "reason:\n" + "\n".join(offenders)
    )


def test_fixture_declarations_are_well_formed() -> None:
    """Only test modules declare, and a declaration must be readable.

    Production and benchmark code cannot exempt itself: a ref there is text a
    user reads, so the answer is to reword it.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders = _scan_source_strings(repo_root, _SOURCE_ROOTS).malformed
    assert not offenders, (
        f"Unusable {_FIXTURE_DECL} declarations. Each must be a tuple of "
        "(ref, reason) string-literal pairs, in a test module:\n"
        + "\n".join(offenders)
    )


def test_fixture_declarations_carry_a_reason() -> None:
    """Every declared literal says why it stays.

    Without this the declaration is the blind spot with a nicer name: a later
    reader cannot tell a reviewed exception from a drive-by silencing.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders = _scan_source_strings(repo_root, _SOURCE_ROOTS).reasonless
    assert not offenders, (
        f"{_FIXTURE_DECL} entries without a usable reason. Say which literal "
        "this is and why it cannot be reworded:\n" + "\n".join(offenders)
    )


def test_fixture_declarations_are_not_stale() -> None:
    """A declared literal that is no longer there is deleted, not left to rot.

    A stale entry pre-authorises a literal nobody reviewed: the line it was
    written for is gone, but the ref keeps whatever takes its place out of the
    scan. A declaration cannot keep itself alive either — its own token inside
    the declaration does not count as the literal it covers.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders = _scan_source_strings(repo_root, _SOURCE_ROOTS).stale
    assert not offenders, (
        f"{_FIXTURE_DECL} entries matching no literal in their own file. "
        "Delete them — what they covered is gone:\n" + "\n".join(offenders)
    )


# ── The declaration rules, pinned on synthetic modules ───────────────────────
# Written to tmp_path rather than asserted against this repo, so they hold the
# same way in any tree this file is published into.


def _write_module(tmp_path: Path, rel: str, body: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_undeclared_literal_is_an_offender(tmp_path: Path) -> None:
    """The baseline: a ref in a literal fails with no declaration to cover it."""
    _write_module(tmp_path, "tests/test_sample.py", 'NOTE = "deferred:#404"\n')
    result = _scan_source_strings(tmp_path, ("tests",))
    assert result.offenders and "#404" in result.offenders[0]
    assert not (result.reasonless or result.stale or result.malformed)


def test_declared_literal_with_a_reason_passes(tmp_path: Path) -> None:
    """A declaration with a real reason clears its own literal, and only it."""
    _write_module(
        tmp_path,
        "tests/test_sample.py",
        f'{_FIXTURE_DECL} = (("#404", "{"x" * _MIN_REASON_CHARS}"),)\n'
        'NOTE = "deferred:#404"\n'
        'OTHER = "deferred:#715"\n',
    )
    result = _scan_source_strings(tmp_path, ("tests",))
    assert not (result.reasonless or result.stale or result.malformed)
    assert len(result.offenders) == 1 and "#715" in result.offenders[0]


def test_reasonless_declaration_is_rejected(tmp_path: Path) -> None:
    """A missing or one-word reason fails, though the literal is covered."""
    for reason in ("", "fixture"):
        tree = tmp_path / reason.rjust(1, "_")
        _write_module(
            tree,
            "tests/test_sample.py",
            f'{_FIXTURE_DECL} = (("#404", "{reason}"),)\nNOTE = "deferred:#404"\n',
        )
        result = _scan_source_strings(tree, ("tests",))
        assert result.reasonless, f"reason={reason!r} should be rejected"
        assert not result.offenders


def test_stale_declaration_is_rejected(tmp_path: Path) -> None:
    """A declaration whose literal is gone fails — including its own token."""
    _write_module(
        tmp_path,
        "tests/test_sample.py",
        f'{_FIXTURE_DECL} = (("#404", "{"x" * _MIN_REASON_CHARS}"),)\n',
    )
    result = _scan_source_strings(tmp_path, ("tests",))
    assert result.stale == ["  tests/test_sample.py: #404"]


def test_declaration_outside_a_test_module_is_rejected(tmp_path: Path) -> None:
    """Production code cannot declare its way out of the scan."""
    _write_module(
        tmp_path,
        "palinode/sample.py",
        f'{_FIXTURE_DECL} = (("#404", "{"x" * _MIN_REASON_CHARS}"),)\n'
        'NOTE = "deferred:#404"\n',
    )
    result = _scan_source_strings(tmp_path, ("palinode",))
    assert result.malformed and _FIXTURE_DECL in result.malformed[0]
    assert result.offenders, "the literal is still an offender"


def test_malformed_declaration_is_rejected(tmp_path: Path) -> None:
    """A declaration the parser cannot read fails loudly instead of silently."""
    _write_module(
        tmp_path,
        "tests/test_sample.py",
        f'{_FIXTURE_DECL} = ("#404",)\nNOTE = "deferred:#404"\n',
    )
    result = _scan_source_strings(tmp_path, ("tests",))
    assert result.malformed
    assert result.offenders, "an unreadable declaration covers nothing"


def test_prose_inside_a_declaration_is_still_scanned(tmp_path: Path) -> None:
    """Only the declared token is skipped — a ref in a reason is an offender."""
    _write_module(
        tmp_path,
        "tests/test_sample.py",
        f'{_FIXTURE_DECL} = (("#404", "{"x" * _MIN_REASON_CHARS} see #715"),)\n'
        'NOTE = "deferred:#404"\n',
    )
    result = _scan_source_strings(tmp_path, ("tests",))
    assert result.offenders and "#715" in result.offenders[0]


# ── What counts as an unfollowable reference ─────────────────────────────────
# The guards above are only as good as this distinction, and it is the part a
# contributor actually collides with, so it is pinned directly.


def test_bare_number_is_rejected() -> None:
    """The original case: a bare tag, whichever tracker the author meant."""
    ref = "#100"
    assert _issue_refs(f"Issue {ref}: the body was mangled.") == [ref]


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
    url = "https://github.com/some-owner/some-private-repo/issues/715"
    text = f"Context: {url}"
    assert _issue_refs(text) == [url]


def test_allowed_url_does_not_mask_a_bare_ref_beside_it() -> None:
    """Stripping the permitted form must not swallow an offender next to it."""
    ref = "#715"
    text = "https://github.com/phasespace-labs/palinode/issues/100 and also " + ref
    assert _issue_refs(text) == [ref]
