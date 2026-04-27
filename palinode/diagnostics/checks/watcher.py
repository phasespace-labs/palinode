"""
Checks: watcher_alive, watcher_indexes_correct_db

Verify the palinode-watcher process is running and is bound to the correct
PALINODE_DIR / db_path.

Platform notes
--------------
On Linux:
  - watcher_alive: uses ``systemctl --user is-active palinode-watcher.service``
    as the primary probe, falling back to scanning ``ps -ef`` for a process
    whose command line contains ``palinode.indexer.watcher``.
  - watcher_indexes_correct_db: reads ``/proc/<pid>/environ`` for the watcher
    PID to compare its PALINODE_DIR against the configured value. This catches
    the case where the watcher is restarted after a directory rename but still
    has the old PALINODE_DIR in its environment.

On macOS:
  - watcher_alive: scans ``ps -ef`` for the watcher process because no launchd
    unit is shipped yet.
  - watcher_indexes_correct_db: /proc is not available on macOS, so this check
    returns severity=info with a "not supported on macOS" message.  Process
    env can be approximated via ``ps -Eww -p <pid>`` but that is not portable
    across macOS versions and requires SIP permissions. Proper support is
    planned when a launchd unit ships.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

_WATCHER_SERVICE = "palinode-watcher.service"
_WATCHER_MODULE = "palinode.indexer.watcher"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_watcher_pid() -> int | None:
    """Return the PID of the running watcher process, or None if not found.

    Uses ``ps -ef`` to scan all processes for one whose command line contains
    ``palinode.indexer.watcher``.  Works on both Linux and macOS.
    """
    try:
        result = subprocess.run(
            ["ps", "-ef"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    for line in result.stdout.splitlines():
        if _WATCHER_MODULE in line:
            # ps -ef columns: UID, PID, PPID, ...
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    continue
    return None


def _systemctl_is_active(service: str) -> bool:
    """Return True if systemctl reports the unit as active."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "active"
    except (OSError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _read_proc_environ(pid: int) -> dict[str, str]:
    """Parse /proc/<pid>/environ into a dict (Linux only).

    Returns an empty dict if the file is unreadable (permission denied,
    process gone, or non-Linux platform).
    """
    environ_path = Path(f"/proc/{pid}/environ")
    try:
        raw = environ_path.read_bytes()
    except (OSError, PermissionError):
        return {}
    env: dict[str, str] = {}
    for entry in raw.split(b"\x00"):
        if b"=" in entry:
            key, _, val = entry.partition(b"=")
            env[key.decode(errors="replace")] = val.decode(errors="replace")
    return env


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@register(tags=("deep",))
def watcher_alive(ctx: DoctorContext) -> CheckResult:
    """Verify the palinode-watcher process is currently running.

    On Linux: primary check is ``systemctl --user is-active
    palinode-watcher.service``.  Falls back to ``ps -ef`` scan if systemctl
    is unavailable (e.g. the unit doesn't exist yet).

    On macOS: only ``ps -ef`` scan is used. There is no launchd unit yet, so
    the ps scan is best-effort.
    """
    is_linux = sys.platform.startswith("linux")

    # Linux: try systemctl first
    if is_linux:
        active = _systemctl_is_active(_WATCHER_SERVICE)
        if active:
            return CheckResult(
                name="watcher_alive",
                severity="error",
                passed=True,
                message=f"systemctl reports {_WATCHER_SERVICE} is active",
                remediation=None,
            )
        # systemctl says not active; also check via ps for non-unit installs
        pid = _find_watcher_pid()
        if pid is not None:
            return CheckResult(
                name="watcher_alive",
                severity="error",
                passed=True,
                message=(
                    f"Watcher process found via ps (PID {pid}); "
                    f"systemctl unit '{_WATCHER_SERVICE}' is not active — "
                    f"consider installing the unit for auto-restart on reboot."
                ),
                remediation=(
                    "Install the systemd unit with the templates under "
                    "'deploy/systemd/'."
                ),
            )
        # Not found by either method
        return CheckResult(
            name="watcher_alive",
            severity="error",
            passed=False,
            message=(
                f"Watcher is not running: "
                f"'{_WATCHER_SERVICE}' is inactive and no matching process found in ps."
            ),
            remediation=(
                "Start the watcher: 'systemctl --user start palinode-watcher' "
                "or run 'palinode-watcher' in the foreground to see errors. "
                "To install the unit: 'palinode deploy-systemd'."
            ),
        )

    # macOS / other: ps-only scan
    pid = _find_watcher_pid()
    if pid is not None:
        return CheckResult(
            name="watcher_alive",
            severity="error",
            passed=True,
            message=f"Watcher process found via ps (PID {pid}).",
            remediation=None,
        )
    return CheckResult(
        name="watcher_alive",
        severity="error",
        passed=False,
        message=(
            "No palinode-watcher process found in ps output. "
                "(macOS: no launchd unit yet, so this is a ps-only check.)"
        ),
        remediation=(
            "Run 'palinode-watcher' in a terminal to start the watcher. "
            "On macOS, consider adding it to your login items or a launchd plist."
        ),
    )


@register(tags=("deep",))
def watcher_indexes_correct_db(ctx: DoctorContext) -> CheckResult:
    """Verify the running watcher's PALINODE_DIR matches the configured value.

    On Linux: reads ``/proc/<pid>/environ`` for the watcher PID to extract its
    PALINODE_DIR, then compares (after realpath resolution) against
    ``config.memory_dir``. This catches the case where the watcher was
    restarted after a rename but retained the old PALINODE_DIR in its environment,
    silently writing new embeddings to the stale database.

    On macOS: /proc is unavailable.  This check returns severity=info with a
    "not supported on macOS" skip message.  Proper support requires reading the
    process environment via ``sysctl KERN_PROCARGS2`` or a privileged helper,
    which is not yet implemented.
    """
    is_linux = sys.platform.startswith("linux")

    # macOS / other platforms: skip with info
    if not is_linux:
        return CheckResult(
            name="watcher_indexes_correct_db",
            severity="info",
            passed=True,
            message=(
                "watcher_indexes_correct_db is not supported on macOS "
                "(requires /proc/<pid>/environ)."
            ),
            remediation=None,
        )

    # Find watcher PID
    pid = _find_watcher_pid()
    if pid is None:
        return CheckResult(
            name="watcher_indexes_correct_db",
            severity="warn",
            passed=False,
            message=(
                "Cannot verify watcher DB: no watcher process found. "
                "Run watcher_alive first."
            ),
            remediation=(
                "Start the watcher: 'systemctl --user start palinode-watcher'."
            ),
        )

    # Read /proc/<pid>/environ
    proc_env = _read_proc_environ(pid)
    if not proc_env:
        return CheckResult(
            name="watcher_indexes_correct_db",
            severity="warn",
            passed=False,
            message=(
                f"Cannot read /proc/{pid}/environ "
                f"(permission denied or process exited). "
                f"Run as the same user as the watcher to inspect its env."
            ),
            remediation=(
                "Run 'palinode doctor' as the user that owns the watcher process, "
                "or inspect manually: "
                f"'cat /proc/{pid}/environ | tr \"\\0\" \"\\n\" | grep PALINODE_DIR'."
            ),
        )

    watcher_palinode_dir = proc_env.get("PALINODE_DIR", "")
    configured_dir = str(Path(ctx.config.memory_dir).expanduser().resolve())

    if not watcher_palinode_dir:
        return CheckResult(
            name="watcher_indexes_correct_db",
            severity="warn",
            passed=False,
            message=(
                f"Watcher PID {pid} has no PALINODE_DIR in its environment. "
                f"It will fall back to the default (~/palinode), "
                f"which may not match configured memory_dir={configured_dir}."
            ),
            remediation=(
                "Restart the watcher after setting PALINODE_DIR: "
                "'systemctl --user restart palinode-watcher'. "
                "Verify the unit's Environment= block: "
                "'systemctl --user cat palinode-watcher'."
            ),
        )

    watcher_resolved = str(Path(watcher_palinode_dir).expanduser().resolve())

    if watcher_resolved != configured_dir:
        return CheckResult(
            name="watcher_indexes_correct_db",
            severity="error",
            passed=False,
            message=(
                f"Watcher PID {pid} has PALINODE_DIR={watcher_palinode_dir!r} "
                f"(resolved: {watcher_resolved}) "
                f"but configured memory_dir is {configured_dir}. "
                f"The watcher is indexing the wrong directory."
            ),
            remediation=(
                f"Restart the watcher with the correct env: "
                f"'systemctl --user restart palinode-watcher'. "
                f"Verify the unit's Environment= block includes "
                f"PALINODE_DIR={configured_dir}: "
                f"'systemctl --user show palinode-watcher | grep PALINODE_DIR'. "
                f"If the unit is stale, re-deploy it: 'palinode deploy-systemd'. "
                f"Review the related diagnostics above."
            ),
        )

    return CheckResult(
        name="watcher_indexes_correct_db",
        severity="error",
        passed=True,
        message=(
            f"Watcher PID {pid} PALINODE_DIR={watcher_palinode_dir!r} "
            f"matches configured memory_dir."
        ),
        remediation=None,
    )
