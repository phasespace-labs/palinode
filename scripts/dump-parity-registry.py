#!/usr/bin/env python3
"""Dump ``palinode.core.parity`` to deterministic JSON on stdout.

Consumers (the plugin TS-side parity test, future TS tooling) read this
artifact instead of parsing Python.  ADR-010 keeps ``parity.py`` as the
single source of truth; this script is only a serializer.

Usage::

    python scripts/dump-parity-registry.py > plugin/parity-registry.json

Output is sorted (operations by name, params by index, drift entries by
``(surface, param)``) so JSON diffs stay clean across runs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Allow running from anywhere — add repo root to sys.path so ``palinode``
# imports work without ``pip install -e .``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from palinode.core.parity import (  # noqa: E402  (sys.path mutation above)
    ADMIN_EXEMPT_OPERATIONS,
    CATEGORIES,
    MEMORY_TYPES,
    PROMPT_TASKS,
    REGISTRY,
)


def _canonical_param_to_dict(cp: Any) -> dict[str, Any]:
    """Serialize a CanonicalParam preserving every field, with stable ordering."""
    return {
        "name": cp.name,
        "type": cp.type,
        "required": cp.required,
        "default_key": cp.default_key,
        "enum": list(cp.enum) if cp.enum is not None else None,
        "notes": cp.notes,
    }


def _operation_to_dict(op: Any) -> dict[str, Any]:
    """Serialize an Operation including its drift and realization entries."""
    drift_entries = sorted(
        (
            {
                "surface": surface,
                "param": param_name,
                "issue": issue,
            }
            for (surface, param_name), issue in op.known_drift.items()
        ),
        key=lambda d: (d["surface"], d["param"]),
    )
    realization_entries = sorted(
        (
            {
                "surface": surface,
                "param": param_name,
                "capability": capability,
            }
            for (surface, param_name), capability in op.surface_realizations.items()
        ),
        key=lambda d: (d["surface"], d["param"]),
    )
    return {
        "name": op.name,
        "canonical_params": [_canonical_param_to_dict(cp) for cp in op.canonical_params],
        "cli_command": op.cli_command,
        "mcp_tool": op.mcp_tool,
        "api_endpoint": list(op.api_endpoint) if op.api_endpoint is not None else None,
        "plugin_tool": op.plugin_tool,
        "exempt_surfaces": sorted(op.exempt_surfaces),
        "known_drift": drift_entries,
        "surface_realizations": realization_entries,
    }


def build_payload() -> dict[str, Any]:
    operations = sorted(
        (_operation_to_dict(op) for op in REGISTRY),
        key=lambda d: d["name"],
    )
    return {
        "operations": operations,
        "admin_exempt": sorted(ADMIN_EXEMPT_OPERATIONS),
        "categories": list(CATEGORIES),
        "memory_types": list(MEMORY_TYPES),
        "prompt_tasks": list(PROMPT_TASKS),
    }


def main() -> int:
    payload = build_payload()
    json.dump(payload, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
