"""The MCP dispatcher's failure prefixes must cover every failure it emits.

The dispatcher reports failure *in-band*: a normal ``TextContent`` whose text
starts with a known prefix. Nothing in the type system distinguishes
``"Save failed: connection refused"`` from a successful result, so
``DISPATCH_ERROR_PREFIXES`` is the only thing that can — which makes an
incomplete list a silent hole rather than a cosmetic one.

It was incomplete. The list lived in ``tests/integration/_smoke_args.py`` as a
hand-maintained copy under a "keep this in sync" comment, and six messages the
dispatcher really emits matched nothing in it:

    Review failed:            ← palinode_review is registered strict
    Unknown action:
    Error activating prompt:
    Error listing prompts:
    Error reading file:
    Error reading prompt:

The last four are the instructive ones: ``"Error reading prompt: …"`` does not
start with ``"Error:"``, so the one generic prefix everybody assumes is a
catch-all is not one. ``test_every_tool_dispatches`` therefore scored those
failures as passes.

This module is the guard that keeps the declaration honest. It reads the
literals out of ``palinode/mcp.py`` — the strings *are* the contract, and they
are constructed inline, so there is no runtime registry to enumerate instead.
A regex over source is a weak test when it stands in for behaviour; here it is
the right tool, because what is being checked is precisely whether a declared
list covers what the source emits.
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

import palinode.mcp as mcp
from palinode.mcp import DISPATCH_ERROR_PREFIXES

#: The words a dispatcher failure message can begin with. Matched at the START
#: of a string literal, which is what makes this precise rather than a keyword
#: sweep: prose that merely mentions "failed" does not begin with it.
#:
#: ``API`` is spelled out as its two real messages rather than left bare. Bare
#: ``API`` also matched the diagnostics ``f"  API:  {url}"`` and
#: ``f"  API backend:  {url}"``, which are aligned console output, not failures.
_FAILURE_OPENERS = (
    "API Error",
    "API unreachable",
    "Archive",
    "Consolidation",
    "Doctor",
    "Error",
    "Ingest",
    "Lint",
    "Push",
    "Review",
    "Save",
    "Search",
    "Session-end",
    "Timeout",
    "Unknown",
)

_OPENS_LIKE_FAILURE = re.compile(
    r"^(?:" + "|".join(_FAILURE_OPENERS) + r")\b[^A-Za-z0-9]*"
)


def _emitted_failure_literals() -> set[str]:
    """Every failure-shaped string literal in the mcp module source.

    Reads **all** string constants, not just those inside a ``_text(...)`` call.
    The first version of this guard scanned `_text(` sites and therefore could
    not see ``_timeout_message()``, which assembles ``"Timeout: …"`` one
    function away and hands it to a caller to wrap — so ``Timeout:`` stayed
    undeclared and a timed-out ``palinode_save`` still read as success. Any
    guard tied to one syntactic form is dodged by moving the string.

    Parsed with ``ast`` rather than ``tokenize``, and that is not a style
    preference — it is the fix for a version split that made this guard behave
    differently on the two Pythons CI runs. Before PEP 701 (3.11) an f-string is
    a single ``STRING`` token; from 3.12 it is ``FSTRING_START`` /
    ``FSTRING_MIDDLE`` / ``FSTRING_END``. A ``tok.type == STRING`` filter
    therefore saw every f-string on 3.11 and none on 3.12 — and nearly every
    real failure message here is an f-string (``f"Save failed: {resp.text}"``),
    so on 3.12 the guard was matching little more than the declaration against
    itself. ``ast`` reports the pieces of a ``JoinedStr`` as ordinary ``Constant``
    nodes on both versions.

    Two exclusions, both by construction rather than by denylist:

    * ``_all_tools()`` — the MCP schema block — and the ``*_input_properties``
      builders that render its spec-generated properties (ADR-022). Their
      titles and descriptions are *data* the server advertises, not responses
      it returns, and they open with the same words ("Save Memory", "Push the
      memory repo…").
    * the ``DISPATCH_ERROR_PREFIXES`` assignment itself — otherwise the scan
      finds the contract and reports that the contract covers it, which is how
      a guard passes while seeing no real emission at all.
    """
    src = inspect.getsource(mcp).replace(inspect.getsource(mcp._all_tools), "")
    for name in dir(mcp):
        if name.endswith("_input_properties") and callable(getattr(mcp, name)):
            src = src.replace(inspect.getsource(getattr(mcp, name)), "")
    tree = ast.parse(src)

    # Span of the declaration, so the contract cannot satisfy itself.
    skip: set[int] = set()
    for node in tree.body:
        targets = getattr(node, "targets", []) or ([node.target] if hasattr(node, "target") else [])
        if any(isinstance(t, ast.Name) and t.id == "DISPATCH_ERROR_PREFIXES" for t in targets):
            skip.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.lineno in skip:
            continue
        head = node.value.strip()
        if not head or "\n" in head:
            continue  # multi-line block: a docstring, not a response message
        if _OPENS_LIKE_FAILURE.match(head):
            found.add(head)
    return found


def test_the_scan_finds_something() -> None:
    """Guard the guard: a regex that matches nothing would pass every assertion."""
    literals = _emitted_failure_literals()
    assert len(literals) >= 25, (
        f"only found {len(literals)} failure literals in palinode/mcp.py — the "
        "scan pattern has drifted away from how the dispatcher writes errors, "
        "and every other assertion in this module is now vacuous."
    )


def test_the_scan_sees_f_string_messages() -> None:
    """The specific vacuity this guard shipped with, named so it cannot return.

    Almost every real failure here is an f-string. Under the original
    ``tokenize``-based scan those were single ``STRING`` tokens on 3.11 and
    ``FSTRING_*`` tokens on 3.12, so the filter saw all of them on one CI leg
    and none on the other — and with the declaration in scope, the 3.12 run
    still found "enough" literals to satisfy a bare count and pass while
    inspecting no emission at all.

    ``'Save failed:'`` (with the colon) exists only as the f-string
    ``f"Save failed: {resp.text}"``; the declaration spells it without one, and
    is excluded from the scan besides. Finding it proves interpolated messages
    are in scope on whichever Python is running.
    """
    literals = _emitted_failure_literals()
    assert "Save failed:" in literals
    assert any(lit.startswith("Timeout:") for lit in literals)


def test_aligned_diagnostics_are_not_mistaken_for_failures() -> None:
    """``f"  API:  {url}"`` is console output, not a dispatcher failure.

    The scan opener was once a bare ``API``, which matched these as well as the
    two real ``API Error:`` / ``API unreachable`` messages. On 3.12 that went
    unnoticed because f-strings were invisible to the scan; on 3.11 it failed
    CI. Both halves of that are fixed, and this pins the half that a narrowing
    of the opener list could undo.
    """
    literals = _emitted_failure_literals()
    assert "API:" not in literals
    assert "API backend:" not in literals
    # …while the two genuine API failures are still in scope.
    assert "API Error:" in literals


def test_every_emitted_failure_is_declared() -> None:
    """No failure message may exist that the declared prefixes do not cover.

    This is the assertion the six drifted messages would have failed.
    """
    uncovered = sorted(
        literal
        for literal in _emitted_failure_literals()
        if not literal.startswith(DISPATCH_ERROR_PREFIXES)
    )
    assert not uncovered, (
        "these dispatcher failure messages match no entry in "
        "palinode.mcp.DISPATCH_ERROR_PREFIXES, so the hermetic smoke test "
        "(test_every_tool_dispatches) reads them as success:\n  " +
        "\n  ".join(repr(u) for u in uncovered)
    )


@pytest.mark.parametrize(
    "message",
    [
        "Review failed: 500 boom",
        "Unknown action: frobnicate. Use 'list', 'read', or 'activate'.",
        "Error activating prompt: no such prompt",
        "Error listing prompts: boom",
        "Error reading file: boom",
        "Error reading prompt: boom",
    ],
)
def test_the_six_that_had_drifted_are_now_caught(message: str) -> None:
    """Named explicitly so a future edit that drops one fails loudly here."""
    assert message.startswith(DISPATCH_ERROR_PREFIXES)


def test_error_colon_is_not_a_catch_all() -> None:
    """The assumption that let four of the six hide.

    Kept as an executable note: anyone tempted to collapse the ``Error <verb>``
    entries back into a single ``"Error:"`` will fail this.
    """
    assert not "Error reading prompt: boom".startswith("Error:")


def test_timeout_is_declared_despite_not_being_a_text_call() -> None:
    """``_timeout_message()`` builds its failure one function away from ``_text``.

    The regression that motivated widening the scan: a write-path tool that
    times out returns ``"Timeout: …"``, which the first version of this guard
    could not see and the declaration therefore omitted — so a timed-out
    ``palinode_save`` read as a success to both the smoke suite and the audit
    log.
    """
    assert mcp._timeout_message("palinode_save").startswith(DISPATCH_ERROR_PREFIXES)
    assert mcp._timeout_message("palinode_search").startswith(DISPATCH_ERROR_PREFIXES)


# ── the audit log's classification ───────────────────────────────────────────


def _audit_status_for(monkeypatch, response_text: str) -> str:
    """Run ``call_tool`` with a canned dispatch result; return the logged status."""
    import asyncio

    captured: dict = {}

    async def _fake_dispatch(name, arguments):
        return mcp._text(response_text)

    def _fake_log_call(name, arguments, duration_ms, status=None, error=None, **kwargs):
        # **kwargs absorbs result_bytes/result_blocks and anything added later —
        # this fake exists to capture `status`, not to pin the signature.
        captured["status"] = status

    monkeypatch.setattr(mcp, "_dispatch_tool", _fake_dispatch)
    monkeypatch.setattr(mcp._audit, "log_call", _fake_log_call)
    asyncio.run(mcp.call_tool("palinode_archive", {}))
    return captured["status"]


@pytest.mark.parametrize(
    "message",
    [
        "Archive failed: 500 boom",
        "Archive-expired sweep failed: boom",
        "Review failed: boom",
        "API unreachable: connection refused",
        "Unknown action: frobnicate",
        "Unknown tool: palinode_nope",
    ],
)
def test_audit_log_records_these_failures_as_errors(monkeypatch, message: str) -> None:
    """These six were logged as ``status="success"``.

    ``call_tool`` carried its own hand-written prefix tuple — a third copy of
    this contract — and these matched nothing in it. Operators reading the audit
    log saw a clean run.
    """
    assert _audit_status_for(monkeypatch, message) == "error"


def test_audit_log_still_records_success_as_success(monkeypatch) -> None:
    """The fix must not make every response an error."""
    assert _audit_status_for(monkeypatch, "Saved memory → insights/x.md") == "success"


def test_audit_classification_uses_the_one_declaration(monkeypatch) -> None:
    """Every declared prefix must classify as an error — no third copy to drift.

    Asserting over the whole tuple rather than a sample is what makes this a
    contract test: adding a prefix to the declaration cannot leave the audit log
    behind, because there is nothing left to update separately.
    """
    for prefix in DISPATCH_ERROR_PREFIXES:
        assert _audit_status_for(monkeypatch, f"{prefix} detail") == "error", prefix


def test_the_smoke_suite_imports_rather_than_mirrors() -> None:
    """The integration copy must be the same object, not an equal one.

    An equal-but-separate tuple is exactly the state this fixed: it passes a
    value comparison on the day it is written and drifts silently thereafter.
    """
    from tests.integration import _smoke_args

    assert _smoke_args.DISPATCH_ERROR_PREFIXES is DISPATCH_ERROR_PREFIXES
