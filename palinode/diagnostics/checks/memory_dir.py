"""
Check: memory_dir_exists

Verifies that the configured memory directory is present on disk.
Severity: critical — without it nothing works.
"""
from __future__ import annotations

from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


@register(tags=("fast",))
def memory_dir_exists(ctx: DoctorContext) -> CheckResult:
    """Verify that config.memory_dir exists on disk."""
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()

    if memory_dir.exists() and memory_dir.is_dir():
        return CheckResult(
            name="memory_dir_exists",
            severity="critical",
            passed=True,
            message=f"Memory directory exists: {memory_dir}",
            remediation=None,
            linked_issue="#190",
        )

    return CheckResult(
        name="memory_dir_exists",
        severity="critical",
        passed=False,
        message=f"Memory directory not found: {memory_dir}",
        remediation=(
            f"Create the directory or set PALINODE_DIR to an existing path.\n"
            f"  mkdir -p {memory_dir}"
        ),
        linked_issue="#190",
    )
