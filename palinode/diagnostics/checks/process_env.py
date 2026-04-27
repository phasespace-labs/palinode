"""
Check: process_env_drift

For every running palinode-{api,mcp,watcher} process, read its environment
via `/proc/$pid/environ` (Linux only) and compare its `PALINODE_DIR` against
the value the doctor's own config now resolves to.  Catches the
2026-04-26 watcher-on-old-env scenario directly: a process that was started
before a directory rename and still indexes the stale path.

Failure mode this prevents:
  - Operator renames PALINODE_DIR + edits palinode.config.yaml
  - Restarts API + MCP, forgets to restart the watcher
  - Watcher silently keeps writing to the old DB; new saves never appear in
    search.  No error, no log, just confusion.

Heuristic for false-positive avoidance
--------------------------------------
This check fires only when drift looks UNINTENTIONAL.

  - One palinode-{api,mcp,watcher} of each kind running, and its env doesn't
    match config → warn (high confidence: this is the renamed-and-forgot case).
  - Two or more of the same kind running → demote to info severity:
    we treat that as an operator who deliberately runs side-by-side
    instances (test + prod, two memory dirs, etc).  We still report what
    we saw so the operator can confirm, but we don't cry wolf.
  - macOS / Windows / anywhere without /proc → info, declined.
    (`ps -E` exists on macOS but requires sudo; we decline rather than
    escalate.)

The heuristic is documented inline in the remediation string so operators
who hit a false positive can read why and override.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


# Process-name fragments we recognize as palinode services.
# We match if any of these substrings appears in the process's argv[0] or
# command-name; specific enough to avoid false positives, generic enough to
# survive systemd-mangled cmdlines.
_KINDS = ("palinode-api", "palinode-mcp", "palinode-watcher")


def _proc_root() -> Path:
    """Override hook for tests."""
    return Path("/proc")


def _read_environ(proc_dir: Path) -> dict[str, str] | None:
    """Read NUL-separated KEY=VAL pairs from /proc/$pid/environ.

    Returns None if the file isn't readable (permission, race, kernel
    quirk).  An empty environ is reported as `{}`, not None.
    """
    environ_path = proc_dir / "environ"
    try:
        raw = environ_path.read_bytes()
    except (OSError, PermissionError):
        return None

    env: dict[str, str] = {}
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            continue
        if "=" not in text:
            continue
        key, _, value = text.partition("=")
        env[key] = value
    return env


def _read_cmdline(proc_dir: Path) -> str | None:
    """Read the NUL-joined argv from /proc/$pid/cmdline."""
    try:
        raw = (proc_dir / "cmdline").read_bytes()
    except (OSError, PermissionError):
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _classify(cmdline: str) -> str | None:
    """Return the palinode-* kind embedded in *cmdline*, or None."""
    for kind in _KINDS:
        if kind in cmdline:
            return kind
    return None


def _scan_processes(proc_root: Path) -> list[dict[str, object]]:
    """Return a list of palinode-* process descriptors found under *proc_root*.

    Each descriptor contains: pid, kind, cmdline, environ (dict).
    Skips entries we cannot read.  Skips our own PID (the doctor process).
    """
    if not proc_root.is_dir():
        return []

    me = os.getpid()
    found: list[dict[str, object]] = []

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        cmdline = _read_cmdline(entry)
        if not cmdline:
            continue
        kind = _classify(cmdline)
        if kind is None:
            continue
        environ = _read_environ(entry)
        if environ is None:
            # Process exists but we can't read its env (likely permission).
            # Record it so the message is honest about what we did/didn't see.
            found.append({
                "pid": pid,
                "kind": kind,
                "cmdline": cmdline,
                "environ": None,
            })
            continue
        found.append({
            "pid": pid,
            "kind": kind,
            "cmdline": cmdline,
            "environ": environ,
        })
    return found


def _normalize_path(value: str) -> str:
    """Expand and resolve a path string for comparison."""
    try:
        return str(Path(os.path.expanduser(value)).resolve())
    except (OSError, RuntimeError):
        return os.path.expanduser(value)


@register(tags=("fast",))
def process_env_drift(ctx: DoctorContext) -> CheckResult:
    """Warn when a running palinode service has a stale PALINODE_DIR."""

    if platform.system() != "Linux":
        return CheckResult(
            name="process_env_drift",
            severity="info",
            passed=True,
            message=(
                f"Skipped on {platform.system()}: /proc/$pid/environ is not "
                "available."
            ),
            remediation=(
                "On macOS, `ps -E` would expose process env vars but requires "
                "sudo to read other users' processes. Doctor declines rather "
                "than escalates. To check manually:\n"
                "  sudo ps -E -p $(pgrep -f palinode-watcher) | tr ' ' '\\n' "
                "| grep PALINODE_DIR"
            ),
        )

    proc_root = _proc_root()
    procs = _scan_processes(proc_root)

    if not procs:
        return CheckResult(
            name="process_env_drift",
            severity="info",
            passed=True,
            message="No palinode-{api,mcp,watcher} processes found.",
            remediation=None,
        )

    configured_dir = _normalize_path(ctx.config.memory_dir)

    # Bucket by kind so we can tell singletons from intentional multi-instance
    by_kind: dict[str, list[dict[str, object]]] = {}
    for p in procs:
        by_kind.setdefault(str(p["kind"]), []).append(p)

    drift_lines: list[str] = []
    drift_count = 0
    intentional_count = 0
    unreadable_count = 0

    for kind, group in by_kind.items():
        is_singleton = len(group) == 1
        for p in group:
            environ = p["environ"]
            pid = p["pid"]
            if environ is None:
                unreadable_count += 1
                drift_lines.append(
                    f"  [unreadable] PID {pid} {kind}: env not readable "
                    f"(likely owned by another user)"
                )
                continue
            assert isinstance(environ, dict)
            proc_dir = environ.get("PALINODE_DIR")
            if proc_dir is None:
                # No env override → process inherits whatever default applied
                # at start-up. Compare against current default if no env set.
                # If config was loaded purely from YAML, this is fine.
                # We only flag if env is set somewhere AND mismatches.
                continue
            if _normalize_path(proc_dir) == configured_dir:
                continue
            # Drift detected
            if is_singleton:
                drift_count += 1
                drift_lines.append(
                    f"  PID {pid} {kind}:\n"
                    f"      process PALINODE_DIR = {proc_dir}\n"
                    f"      configured value     = {configured_dir}"
                )
            else:
                intentional_count += 1
                drift_lines.append(
                    f"  [info: {len(group)}× {kind} running, treating as intentional]"
                    f"\n"
                    f"  PID {pid} {kind}: PALINODE_DIR = {proc_dir} "
                    f"(configured: {configured_dir})"
                )

    if drift_count == 0 and intentional_count == 0 and unreadable_count == 0:
        return CheckResult(
            name="process_env_drift",
            severity="info",
            passed=True,
            message=(
                f"All {len(procs)} palinode process(es) have PALINODE_DIR "
                f"matching configured value ({configured_dir})."
            ),
            remediation=None,
        )

    if drift_count > 0:
        # The bad case — a singleton service with stale env.
        msg = (
            f"{drift_count} palinode service(s) running with stale "
            f"PALINODE_DIR (configured: {configured_dir})."
        )
        remediation_parts = [
            "Restart the affected services to pick up the current env:",
            "  systemctl --user restart palinode-watcher palinode-api palinode-mcp",
            "(or kill + relaunch via your service manager of choice).",
            "",
            "Heuristic note: this check warns only when a SINGLE process of",
            "a given kind has drift — that's the renamed-and-forgot pattern.",
            "Multiple processes of the same kind are treated as intentional",
            "(test + prod side-by-side) and reported at info severity.",
            "",
            "Detected drift:",
            *drift_lines,
        ]
        return CheckResult(
            name="process_env_drift",
            severity="warn",
            passed=False,
            message=msg,
            remediation="\n".join(remediation_parts),
        )

    # Only intentional / unreadable findings — info severity, no failure
    msg_parts = []
    if intentional_count:
        msg_parts.append(
            f"{intentional_count} process(es) with mismatched PALINODE_DIR "
            f"(treated as intentional multi-instance)"
        )
    if unreadable_count:
        msg_parts.append(
            f"{unreadable_count} process(es) with unreadable env "
            f"(permission denied)"
        )
    return CheckResult(
        name="process_env_drift",
        severity="info",
        passed=True,
        message="; ".join(msg_parts),
        remediation="\n".join(["Detected:", *drift_lines]),
    )
