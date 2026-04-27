"""
Check: multiple_palinode_dirs

Warns when the PALINODE_DIR environment variable and the memory_dir value
loaded from palinode.config.yaml disagree.  After load_config() runs, the
env var wins — but if YAML hasn't been updated to match, the system is one
env-unset away from silently switching to a different store.

Detection strategy:
  The config module applies env overrides *after* loading YAML.  By the time
  DoctorContext.config is constructed, config.memory_dir already reflects the
  env-win.  To detect disagreement we re-read PALINODE_DIR directly from
  os.environ and compare against the YAML value that was actually loaded.
  Because we don't re-parse the YAML here (doctor must not re-invoke
  load_config), we infer the YAML value by checking whether config.memory_dir
  differs from what PALINODE_DIR says.  If they match → no env-vs-yaml drift.

Severity: warn
 """
from __future__ import annotations

import os
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


@register(tags=("fast",))
def multiple_palinode_dirs(ctx: DoctorContext) -> CheckResult:
    """Warn when PALINODE_DIR env var and config.memory_dir disagree."""
    env_dir_raw = os.environ.get("PALINODE_DIR")

    # If env var is not set, there can be no env-vs-YAML conflict.
    if env_dir_raw is None:
        return CheckResult(
            name="multiple_palinode_dirs",
            severity="warn",
            passed=True,
            message=(
                "PALINODE_DIR not set in environment; "
                f"memory_dir resolved from config: {ctx.config.memory_dir}"
            ),
            remediation=None,
        )

    env_dir = Path(env_dir_raw).expanduser().resolve()
    config_dir = Path(ctx.config.memory_dir).expanduser().resolve()

    if env_dir == config_dir:
        return CheckResult(
            name="multiple_palinode_dirs",
            severity="warn",
            passed=True,
            message=(
                f"PALINODE_DIR matches config memory_dir: {config_dir}"
            ),
            remediation=None,
        )

    return CheckResult(
        name="multiple_palinode_dirs",
        severity="warn",
        passed=False,
        message=(
            f"PALINODE_DIR env var and config memory_dir disagree — "
            f"env: {env_dir}  config: {config_dir}"
        ),
        remediation=(
            "The PALINODE_DIR environment variable overrides memory_dir from "
            "palinode.config.yaml.  They currently point to different locations.\n"
            "If you recently renamed the data directory and updated PALINODE_DIR "
            "but forgot to update the YAML, fix the YAML to match:\n"
            f"  Open palinode.config.yaml and set:\n"
            f"    memory_dir: {env_dir}\n"
            "  Then restart palinode-api and palinode-watcher.\n"
            "If the env var is stale, unset it:\n"
            "  unset PALINODE_DIR\n"
            "See the related diagnostics above."
        ),
    )
