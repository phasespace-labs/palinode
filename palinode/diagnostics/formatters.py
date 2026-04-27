"""
Output formatters for palinode doctor results.

format_text  — human-readable with ✓/✗/⚠ markers and rich markup colours.
format_json  — machine-parseable JSON array.
"""
from __future__ import annotations

import json

from palinode.diagnostics.types import CheckResult

# Mapping severity → rich colour tag used when a check fails.
_SEVERITY_COLOUR: dict[str, str] = {
    "info": "blue",
    "warn": "yellow",
    "error": "red",
    "critical": "bold red",
}


def _check_marker(result: CheckResult) -> str:
    """Return a rich-markup marker string for the result."""
    if result.passed:
        return "[green]✓[/green]"
    severity = result.severity
    colour = _SEVERITY_COLOUR.get(severity, "red")
    if severity == "warn":
        return f"[{colour}]⚠[/{colour}]"
    return f"[{colour}]✗[/{colour}]"


def format_text(results: list[CheckResult], verbose: bool = False) -> str:
    """Return a rich-markup string suitable for printing with a rich Console.

    Each result occupies one line with a marker, the check name, and a brief
    message. In verbose mode, remediation text is appended for failed checks.
    """
    lines: list[str] = []
    for result in results:
        marker = _check_marker(result)
        lines.append(f"  {marker} {result.name}: {result.message}")
        if verbose or not result.passed:
            if result.remediation:
                for rem_line in result.remediation.splitlines():
                    lines.append(f"      {rem_line}")
    return "\n".join(lines)


def format_json(results: list[CheckResult]) -> str:
    """Return a JSON array of check result objects."""
    payload = [
        {
            "name": r.name,
            "severity": r.severity,
            "passed": r.passed,
            "message": r.message,
            "remediation": r.remediation,
            "linked_issue": r.linked_issue,
        }
        for r in results
    ]
    return json.dumps(payload, indent=2)
