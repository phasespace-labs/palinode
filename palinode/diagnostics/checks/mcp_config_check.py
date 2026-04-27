"""
Check: mcp_config_homes

Walks every canonical MCP-client config home and warns when multiple files
contain a `palinode` server entry whose contents disagree.  Editing the
wrong file is the silent-failure pattern documented by these diagnostics — this surfaces
divergence at doctor time.

Reuses the canonical-locations walker from `palinode/cli/mcp_config.py` so
behaviour stays in lock-step with `palinode mcp-config --diagnose`.

 """
from __future__ import annotations

import json
from pathlib import Path

from palinode.cli.mcp_config import (
    ConfigResult,
    _candidate_paths,
    _check_divergence,
    _extract_palinode_entry,
    _read_config,
)
from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


def _gather_results() -> list[ConfigResult]:
    """Walk canonical locations and collect a ConfigResult per candidate."""
    results: list[ConfigResult] = []
    for label, path in _candidate_paths():
        if not path.exists():
            results.append(ConfigResult(
                label=label,
                path=path,
                present=False,
                entry=None,
                entry_json=None,
                error=None,
            ))
            continue

        data, error = _read_config(path)
        if error:
            results.append(ConfigResult(
                label=label,
                path=path,
                present=True,
                entry=None,
                entry_json=None,
                error=error,
            ))
            continue

        entry = _extract_palinode_entry(data or {})
        entry_json = (
            json.dumps(entry, sort_keys=True, indent=2)
            if entry is not None else None
        )
        results.append(ConfigResult(
            label=label,
            path=path,
            present=True,
            entry=entry,
            entry_json=entry_json,
            error=None,
        ))
    return results


@register(tags=("fast",))
def mcp_config_homes(ctx: DoctorContext) -> CheckResult:
    """Warn when multiple MCP config files have divergent palinode entries."""

    results = _gather_results()
    with_entries = [r for r in results if r.entry is not None and r.error is None]

    if not with_entries:
        return CheckResult(
            name="mcp_config_homes",
            severity="info",
            passed=True,
            message=(
                "No MCP config files contain a 'palinode' entry. "
                "Run `palinode init` to scaffold one."
            ),
            remediation=None,
        )

    if len(with_entries) == 1:
        only = with_entries[0]
        return CheckResult(
            name="mcp_config_homes",
            severity="info",
            passed=True,
            message=f"Single MCP config has palinode entry: {only.path}",
            remediation=None,
        )

    divergences = _check_divergence(results)
    if not divergences:
        return CheckResult(
            name="mcp_config_homes",
            severity="info",
            passed=True,
            message=(
                f"{len(with_entries)} MCP config files have palinode entries; "
                f"all agree."
            ),
            remediation=None,
        )

    # Build remediation: list every divergent pair plus its diff
    remediation_parts: list[str] = [
        "Multiple MCP config files have a 'palinode' entry but they differ.",
        "The running client reads only ONE of these — editing the wrong one",
        "is the silent-failure pattern.",
        "",
        "Run `palinode mcp-config --diagnose` for a full breakdown,",
        "or use `palinode mcp-config --diagnose --json` for structured output.",
        "",
    ]
    for a, b, diff in divergences:
        remediation_parts.append(f"Differs:")
        remediation_parts.append(f"  A: {a.path}")
        remediation_parts.append(f"  B: {b.path}")
        if diff:
            for line in diff.splitlines():
                remediation_parts.append(f"    {line}")
        remediation_parts.append("")

    return CheckResult(
        name="mcp_config_homes",
        severity="warn",
        passed=False,
        message=(
            f"{len(divergences)} pair(s) of MCP config files have divergent "
            f"palinode entries across {len(with_entries)} files."
        ),
        remediation="\n".join(remediation_parts).rstrip(),
    )
