"""
Cross-surface parity test — ADR-010 forcing function.

For every operation in ``palinode.core.parity.REGISTRY``, asserts that each
non-exempt surface (CLI, MCP, REST API, plugin) exposes the canonical
parameters with matching names.

Known drift (per the audit on 2026-04-26) is recorded as ``known_drift`` on
each Operation, with the GitHub issue tracking the fix.  Drift entries are
reported as ``xfail`` with a ``reason="drift tracked in #N"`` — the test
*passes* while the drift exists, but as soon as the surface is fixed the
``known_drift`` entry must be removed (or the test will fail because the
parameter now appears unexpectedly).

Plugin parity is documented in ``docs/PARITY.md`` but not asserted in v0
(Python cannot easily introspect TypeBox schemas in ``plugin/index.ts``).
A plugin-side TypeScript test is a follow-up.

Run: ``pytest tests/test_surface_parity.py -v``
"""
from __future__ import annotations

import asyncio
import inspect
import os
import typing
from typing import Any

import click
import pytest
from pydantic import BaseModel

from palinode.cli import main as cli_root
from palinode.core.parity import (
    REGISTRY,
    CanonicalParam,
    Operation,
    Surface,
    required_surfaces,
)


# ─────────────────────────────────────────────────────────────────────────────
# CLI extraction
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_cli_command(path: str) -> click.Command | None:
    """Walk dotted/space-separated CLI path to a Click command."""
    parts = path.replace(".", " ").split()
    node: click.Command | click.Group = cli_root
    for part in parts:
        if not isinstance(node, click.Group):
            return None
        node = node.commands.get(part)  # type: ignore[assignment]
        if node is None:
            return None
    return node  # type: ignore[return-value]


def _cli_param_names(cmd: click.Command) -> set[str]:
    """Return the set of canonical param names a Click command exposes.

    Click stores params as ``--foo-bar`` on the CLI but ``foo_bar`` in
    Python.  We compare against the Python name (which is the canonical
    form in our registry).  ``--ps`` flags are renamed to their dest
    (e.g. ``is_ps``) — we map both to ``ps`` for parity purposes.
    """
    names: set[str] = set()
    for param in cmd.params:
        if isinstance(param, click.Option):
            # Click's `name` is the dest; `opts` is the surface flag list.
            # Prefer `name` (canonical Python form).
            if param.name:
                names.add(param.name)
        elif isinstance(param, click.Argument) and param.name:
            names.add(param.name)
    # Click uses `is_ps` as the dest for `--ps`; expose under "ps" too.
    if "is_ps" in names:
        names.add("ps")
    # Click uses `memory_type` as the dest for `--type` (avoid keyword
    # collision); expose under "type" too.
    if "memory_type" in names:
        names.add("type")
    # Click uses `entities` (multiple=True) for `--entity` repeated.  Same
    # canonical name; nothing to do.
    # Click uses `external_ref_pairs` as dest for `--external-ref` (multiple);
    # expose under the canonical "external_refs" name too (#115).
    if "external_ref_pairs" in names:
        names.add("external_refs")
    return names


# ─────────────────────────────────────────────────────────────────────────────
# MCP extraction
# ─────────────────────────────────────────────────────────────────────────────


_MCP_TOOL_CACHE: dict[str, dict[str, Any]] | None = None


def _mcp_tools() -> dict[str, dict[str, Any]]:
    """Return ``{tool_name: inputSchema}`` for every MCP tool, cached.

    ``palinode.mcp.list_tools`` is async (MCP protocol contract).  We
    invoke it once via ``asyncio.run`` and cache for all tests.
    """
    global _MCP_TOOL_CACHE
    if _MCP_TOOL_CACHE is None:
        from palinode.mcp import list_tools as mcp_list_tools

        previous = os.environ.get("PALINODE_MCP_SURFACE")
        os.environ["PALINODE_MCP_SURFACE"] = "full"
        try:
            tools = asyncio.run(mcp_list_tools())
        finally:
            if previous is None:
                os.environ.pop("PALINODE_MCP_SURFACE", None)
            else:
                os.environ["PALINODE_MCP_SURFACE"] = previous
        _MCP_TOOL_CACHE = {t.name: t.inputSchema for t in tools}
    return _MCP_TOOL_CACHE


def _mcp_param_names(tool_name: str) -> set[str]:
    schema = _mcp_tools().get(tool_name)
    if schema is None:
        return set()
    props = schema.get("properties", {}) or {}
    return set(props.keys())


# ─────────────────────────────────────────────────────────────────────────────
# API extraction
# ─────────────────────────────────────────────────────────────────────────────


# Map ``(method, path)`` → request-body model (or ``None`` for GET-style).
# We import lazily inside the helper to keep test collection cheap.
def _api_param_names(method: str, path: str) -> set[str]:
    """Extract API parameter names from the FastAPI app.

    For POST endpoints with a pydantic body, returns the model's field
    names.  For GET endpoints, returns the function's keyword arguments
    (excluding ``request``-shaped helpers).
    """
    from palinode.api.server import app  # lazy

    for route in app.routes:
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", set()) or set()
        if route_path != path or method.upper() not in route_methods:
            continue

        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            return set()

        # ``server.py`` uses ``from __future__ import annotations`` so the
        # raw ``param.annotation`` is a string.  Resolve via ``get_type_hints``
        # which evaluates the strings against the function's module globals.
        try:
            hints = typing.get_type_hints(endpoint)
        except Exception:
            hints = {}

        names: set[str] = set()
        sig = inspect.signature(endpoint)
        for param_name, param in sig.parameters.items():
            if param_name in {"request", "self"}:
                continue
            ann = hints.get(param_name, param.annotation)
            if _is_request_helper(ann):
                continue
            # Body param: pydantic BaseModel subclass → use its field names
            if inspect.isclass(ann) and issubclass(ann, BaseModel):
                names.update(ann.model_fields.keys())
            else:
                names.add(param_name)
        return names

    return set()


def _is_request_helper(annotation: Any) -> bool:
    """Best-effort check for FastAPI ``Request``/``Response`` helpers."""
    try:
        from fastapi import Request, Response

        return annotation in (Request, Response)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Surface dispatch
# ─────────────────────────────────────────────────────────────────────────────


def _surface_param_names(op: Operation, surface: Surface) -> set[str]:
    if surface == "cli":
        if op.cli_command is None:
            return set()
        cmd = _resolve_cli_command(op.cli_command)
        if cmd is None:
            return set()
        return _cli_param_names(cmd)
    if surface == "mcp":
        if op.mcp_tool is None:
            return set()
        return _mcp_param_names(op.mcp_tool)
    if surface == "api":
        if op.api_endpoint is None:
            return set()
        method, path = op.api_endpoint
        return _api_param_names(method, path)
    if surface == "plugin":
        # Plugin parity is documented in PARITY.md, not asserted in v0.
        return set()
    raise AssertionError(f"unknown surface {surface!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Parametrized test
# ─────────────────────────────────────────────────────────────────────────────


def _flatten_cases() -> list[tuple[Operation, Surface, CanonicalParam]]:
    cases: list[tuple[Operation, Surface, CanonicalParam]] = []
    for op in REGISTRY:
        # v0: skip plugin parity entirely
        for surface in sorted(required_surfaces(op) - {"plugin"}):
            for cp in op.canonical_params:
                cases.append((op, surface, cp))  # type: ignore[arg-type]
    return cases


def _case_id(case: tuple[Operation, Surface, CanonicalParam]) -> str:
    op, surface, cp = case
    return f"{op.name}/{surface}/{cp.name}"


@pytest.mark.parametrize("case", _flatten_cases(), ids=_case_id)
def test_canonical_param_present(case: tuple[Operation, Surface, CanonicalParam]) -> None:
    """Every canonical param appears on every required surface (or is known drift)."""
    op, surface, cp = case
    surface_params = _surface_param_names(op, surface)
    drift_key = (surface, cp.name)
    if drift_key in op.known_drift:
        issue = op.known_drift[drift_key]
        if cp.name in surface_params:
            # Drift was tracked but the surface now exposes the param — the
            # known_drift entry should be removed.  Failing here is the point.
            pytest.fail(
                f"{op.name}/{surface}: param {cp.name!r} is now present; "
                f"remove `known_drift[(\"{surface}\", \"{cp.name}\")]` "
                f"and close issue #{issue}."
            )
        pytest.xfail(f"drift tracked in #{issue}")

    assert cp.name in surface_params, (
        f"{op.name}/{surface}: canonical param {cp.name!r} not exposed "
        f"(found: {sorted(surface_params)}). "
        f"If this is intentional drift, add `known_drift[(\"{surface}\", "
        f"\"{cp.name}\")] = <issue>` on the Operation."
    )


def test_admin_exempt_ops_are_not_in_registry() -> None:
    """Operations in ADMIN_EXEMPT_OPERATIONS must not also be in REGISTRY.

    The two lists are mutually exclusive: registry = parity-bound,
    exempt = parity-free.  Confusion here means an admin op is being
    silently parity-tested.
    """
    from palinode.core.parity import ADMIN_EXEMPT_OPERATIONS

    registry_names = {op.name for op in REGISTRY}
    overlap = registry_names & ADMIN_EXEMPT_OPERATIONS
    assert not overlap, (
        f"Operations both in REGISTRY and ADMIN_EXEMPT_OPERATIONS: {overlap}. "
        "Pick one — exempt = parity-free, registry = parity-bound."
    )


def test_default_keys_resolve() -> None:
    """Every CanonicalParam.default_key must exist in palinode.core.defaults."""
    from palinode.core import defaults as defaults_mod

    missing: list[str] = []
    for op in REGISTRY:
        for cp in op.canonical_params:
            if cp.default_key is not None and not hasattr(defaults_mod, cp.default_key):
                missing.append(f"{op.name}/{cp.name} → defaults.{cp.default_key}")
    assert not missing, (
        "Unknown default_key references in parity registry:\n  "
        + "\n  ".join(missing)
    )


def test_known_drift_references_a_canonical_param() -> None:
    """``known_drift`` keys must reference a real canonical param name."""
    bad: list[str] = []
    for op in REGISTRY:
        canonical_names = {cp.name for cp in op.canonical_params}
        for surface, param_name in op.known_drift:
            if param_name not in canonical_names:
                bad.append(f"{op.name}: known_drift[({surface!r}, {param_name!r})]")
    assert not bad, (
        "known_drift entries reference unknown params:\n  "
        + "\n  ".join(bad)
    )
