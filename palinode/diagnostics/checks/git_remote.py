"""
Check: git_remote_health

Runs `git -C <memory_dir> ls-remote origin HEAD` with a short timeout.
A reachable remote means the memory store has an offsite backup channel.
An unreachable remote is a warning — silent backup absence is a forward-
looking risk (user may not have noticed git push failing for days).

Also counts unpushed commits (via `git rev-list @{u}..HEAD`).  Warns
at >50 unpushed commits (significant drift between local and backup).

Severity: warn (remote unreachable or large drift)

This check requires network I/O, so it is tagged "deep".

 """
from __future__ import annotations

import subprocess
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

_GIT_TIMEOUT = 8  # seconds


def _run_git(args: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@register(tags=("deep",))
def git_remote_health(ctx: DoctorContext) -> CheckResult:
    """Check whether memory_dir's git remote is reachable.

    Reports:
    - Pass: remote reachable, unpushed commit count
    - Warn: remote unreachable (network / SSH key / URL issue)
    - Info: no git remote configured (offline-only store, not a failure)
    - Info: memory_dir is not a git repo
    """
    memory_dir = str(Path(ctx.config.memory_dir).expanduser().resolve())

    # Is this a git repo at all?
    try:
        result = _run_git(["rev-parse", "--git-dir"], cwd=memory_dir, timeout=3)
    except FileNotFoundError:
        return CheckResult(
            name="git_remote_health",
            severity="warn",
            passed=False,
            message="git binary not found; cannot check remote health.",
            remediation="Install git to enable remote backup checks.",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="git_remote_health",
            severity="info",
            passed=True,
            message="git rev-parse timed out; skipping remote check.",
            remediation=None,
        )

    if result.returncode != 0:
        return CheckResult(
            name="git_remote_health",
            severity="info",
            passed=True,
            message=f"memory_dir is not a git repository: {memory_dir}",
            remediation=(
                "Run 'git init && git remote add origin <url>' inside memory_dir "
                "to enable git-backed offsite backup."
            ),
        )

    # Is there a remote named "origin"?
    try:
        remote_result = _run_git(["remote", "get-url", "origin"], cwd=memory_dir, timeout=3)
    except subprocess.TimeoutExpired:
        remote_result = None

    if remote_result is None or remote_result.returncode != 0:
        return CheckResult(
            name="git_remote_health",
            severity="info",
            passed=True,
            message="No git remote named 'origin' configured.",
            remediation=(
                "Run 'git remote add origin <url>' inside memory_dir to enable "
                "offsite backup.  Without a remote, 'palinode push' has nowhere to push."
            ),
        )

    remote_url = remote_result.stdout.strip()

    # Probe the remote with ls-remote (reads only, no fetch).
    try:
        ls_result = _run_git(
            ["ls-remote", "origin", "HEAD"],
            cwd=memory_dir,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="git_remote_health",
            severity="warn",
            passed=False,
            message=f"git ls-remote origin timed out after {_GIT_TIMEOUT}s — remote unreachable.",
            remediation=(
                f"Remote URL: {remote_url}\n"
                "Check network connectivity and SSH keys.  "
                "Run 'palinode push' manually to see the full error."
            ),
        )

    if ls_result.returncode != 0:
        return CheckResult(
            name="git_remote_health",
            severity="warn",
            passed=False,
            message=(
                f"git ls-remote origin failed (exit {ls_result.returncode}) — "
                "remote may be unreachable or misconfigured."
            ),
            remediation=(
                f"Remote URL: {remote_url}\n"
                f"git stderr: {ls_result.stderr.strip() or '(none)'}\n"
                "Check network connectivity, SSH key permissions, and the remote URL.  "
                "Run 'git -C <memory_dir> ls-remote origin' manually to see the full error."
            ),
        )

    # Count unpushed commits.
    unpushed = 0
    try:
        unpushed_result = _run_git(
            ["rev-list", "--count", "@{u}..HEAD"],
            cwd=memory_dir,
            timeout=5,
        )
        if unpushed_result.returncode == 0:
            unpushed = int(unpushed_result.stdout.strip() or "0")
    except (subprocess.TimeoutExpired, ValueError):
        unpushed = -1  # unknown

    unpushed_str = f"{unpushed} unpushed commit(s)" if unpushed >= 0 else "unpushed count unknown"

    if unpushed > 50:
        return CheckResult(
            name="git_remote_health",
            severity="warn",
            passed=False,
            message=(
                f"Remote is reachable but {unpushed} commits have not been pushed.  "
                "Memory backup is significantly behind."
            ),
            remediation=(
                "Run 'palinode push' or 'git -C <memory_dir> push origin' to sync backups.\n"
                f"Remote URL: {remote_url}"
            ),
        )

    return CheckResult(
        name="git_remote_health",
        severity="warn",
        passed=True,
        message=f"Remote 'origin' is reachable — {unpushed_str}.",
        remediation=None,
    )
