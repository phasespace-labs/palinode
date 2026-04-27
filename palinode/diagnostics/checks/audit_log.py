"""
Check: audit_log_writable

Verifies that the configured audit log path is writable and, if relative,
warns that each cwd will produce a different log file (the "scattered audit
logs" footgun from palinode/core/config.py's default ".audit/mcp-calls.jsonl").

Severity:
  warn — relative path (scattered logs across cwds)
  warn — path not writable (audit logging silently fails)
  pass — absolute path and writable

Tag: fast (no network, no SQLite)

 """
from __future__ import annotations

import os
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


@register(tags=("fast",))
def audit_log_writable(ctx: DoctorContext) -> CheckResult:
    """Check whether the audit log path is absolute and writable.

    When audit.enabled is False, the check passes immediately (no log → no
    problem).  When enabled, it validates the configured path.

    Two distinct failure modes are detected:
    1. Relative path — logs scatter across cwd-dependent locations.
    2. Not writable — audit calls silently fail.
    """
    if not ctx.config.audit.enabled:
        return CheckResult(
            name="audit_log_writable",
            severity="warn",
            passed=True,
            message="Audit logging is disabled (audit.enabled=false) — nothing to check.",
            remediation=None,
        )

    log_path_raw: str = ctx.config.audit.log_path
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()

    # Resolve the path: absolute as-is, relative → resolve relative to memory_dir.
    is_relative = not os.path.isabs(log_path_raw)
    if is_relative:
        resolved = (memory_dir / log_path_raw).resolve()
    else:
        resolved = Path(log_path_raw).expanduser().resolve()

    # Warn on relative path regardless of writability.
    if is_relative:
        return CheckResult(
            name="audit_log_writable",
            severity="warn",
            passed=False,
            message=(
                f"audit.log_path is relative ('{log_path_raw}').  "
                "Every directory palinode is invoked from will create a separate "
                "log file, silently scattering audit records across the filesystem."
            ),
            remediation=(
                f"Set audit.log_path to an absolute path under memory_dir in "
                f"palinode.config.yaml.  For example:\n"
                f"  audit:\n"
                f"    log_path: {memory_dir / '.audit' / 'mcp-calls.jsonl'}\n\n"
                f"Resolved current effective path: {resolved}"
            ),
        )

    # Absolute path: check writability.
    # Strategy: check parent directory writability (the file may not exist yet).
    parent = resolved.parent
    if not parent.exists():
        return CheckResult(
            name="audit_log_writable",
            severity="warn",
            passed=False,
            message=(
                f"Audit log parent directory does not exist: {parent}  "
                "(Audit calls will fail silently.)"
            ),
            remediation=(
                f"Create the directory:\n"
                f"  mkdir -p {parent}\n"
                f"Then verify palinode-api can write to it."
            ),
        )

    if not os.access(str(parent), os.W_OK):
        return CheckResult(
            name="audit_log_writable",
            severity="warn",
            passed=False,
            message=(
                f"Audit log parent directory is not writable: {parent}  "
                "(Audit calls will fail silently.)"
            ),
            remediation=(
                f"Fix permissions:\n"
                f"  chmod u+w {parent}\n"
                f"Or change audit.log_path to a writable location."
            ),
        )

    # If the file already exists, check it is also writable.
    if resolved.exists() and not os.access(str(resolved), os.W_OK):
        return CheckResult(
            name="audit_log_writable",
            severity="warn",
            passed=False,
            message=f"Audit log file exists but is not writable: {resolved}",
            remediation=(
                f"Fix permissions:\n"
                f"  chmod u+w {resolved}"
            ),
        )

    return CheckResult(
        name="audit_log_writable",
        severity="warn",
        passed=True,
        message=f"Audit log path is absolute and writable: {resolved}",
        remediation=None,
    )
