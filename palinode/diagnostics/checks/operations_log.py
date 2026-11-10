"""Check whether the active operations audit log is awaiting rotation."""
from __future__ import annotations

from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


@register(tags=("fast",))
def operations_log_size(ctx: DoctorContext) -> CheckResult:
    """Warn when the active audit stream exceeds its configured rotation size."""
    limit = ctx.config.logging.operations_max_bytes
    raw_path = Path(ctx.config.logging.operations_log).expanduser()
    path = raw_path if raw_path.is_absolute() else Path(ctx.config.memory_dir) / raw_path

    if limit == 0:
        return CheckResult(
            name="operations_log_size",
            severity="warn",
            passed=True,
            message="Operations log size rotation is disabled (limit: 0 bytes).",
        )

    try:
        actual = path.stat().st_size
    except FileNotFoundError:
        return CheckResult(
            name="operations_log_size",
            severity="warn",
            passed=True,
            message=f"Operations log does not yet exist: {path}",
        )

    oversized = actual > limit
    return CheckResult(
        name="operations_log_size",
        severity="warn",
        passed=not oversized,
        message=(
            f"Operations log size is {actual} bytes; configured limit is {limit} bytes."
            + (" Rotation will occur on a future write." if oversized else "")
        ),
        remediation=(
            "Write another log record to trigger size-based rotation, or review the "
            "configured operations log limit."
            if oversized
            else None
        ),
    )
