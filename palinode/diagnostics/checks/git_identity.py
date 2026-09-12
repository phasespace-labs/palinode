"""
Check: git_commit_ready

Verifies that git auto-commit can actually land: ``memory_dir`` is a git
repository and a committer identity resolves there (``git var
GIT_COMMITTER_IDENT`` — the same predicate ``git commit`` applies).  Either
gap turns every save into a silent no-op commit — the file is on disk but
the provenance/history guarantee is not in force (the git_committed
truthfulness fix).

Read-only: probes with ``git rev-parse`` / ``git var`` only.

Severity: warn (auto_commit enabled but a commit cannot succeed).
Info/pass when auto_commit is disabled — nothing to check.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

_NAME = "git_commit_ready"
_GIT_TIMEOUT = 3  # seconds


def _run_git(args: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


@register(tags=("fast",))
def git_commit_ready(ctx: DoctorContext) -> CheckResult:
    """Check that ``memory_dir`` is a git repo with a commit identity.

    Reports:
    - Pass: repo present, identity resolves
    - Warn: auto_commit on but memory_dir is not a git repository
    - Warn: auto_commit on but no committer identity resolves in memory_dir
    - Info: auto_commit disabled (skipped)
    """
    if not ctx.config.git.auto_commit:
        return CheckResult(
            name=_NAME,
            severity="info",
            passed=True,
            message="git.auto_commit is disabled; saves are not git-versioned.",
        )

    memory_dir = str(Path(ctx.config.memory_dir).expanduser().resolve())

    try:
        repo = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=memory_dir, timeout=_GIT_TIMEOUT)
    except FileNotFoundError:
        return CheckResult(
            name=_NAME,
            severity="warn",
            passed=False,
            message="git binary not found; auto-commit cannot run.",
            remediation="Install git, or set git.auto_commit: false in palinode.config.yaml.",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=_NAME,
            severity="info",
            passed=True,
            message="git rev-parse timed out in memory_dir; skipping identity check.",
        )

    if repo.returncode != 0 or repo.stdout.strip() != "true":
        return CheckResult(
            name=_NAME,
            severity="warn",
            passed=False,
            message=(
                f"git.auto_commit is enabled but memory_dir is not a git repository: "
                f"{memory_dir}. Every save reports git_committed=false."
            ),
            remediation=(
                f"Run 'git -C {memory_dir} init' (and set user.name / user.email) "
                "so saves are versioned, or set git.auto_commit: false."
            ),
        )

    # ``git var GIT_COMMITTER_IDENT`` applies exactly the predicate ``git
    # commit`` does (config, env, then a hostname-derived fallback unless
    # ``user.useConfigOnly`` is set), so it fails precisely when a commit would.
    try:
        ident = _run_git(["var", "GIT_COMMITTER_IDENT"], cwd=memory_dir, timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        ident = None

    if ident is None or ident.returncode != 0 or not ident.stdout.strip():
        detail = (ident.stderr.strip().splitlines() or ["(no output)"])[0] if ident else "timed out"
        return CheckResult(
            name=_NAME,
            severity="warn",
            passed=False,
            message=(
                "git.auto_commit is enabled but no commit identity resolves in "
                f"memory_dir ({detail}). git commit will fail and every save "
                "reports git_committed=false."
            ),
            remediation=(
                f"Run 'git -C {memory_dir} config user.name \"Palinode\"' and "
                f"'git -C {memory_dir} config user.email \"palinode@localhost\"' "
                "(or set them globally for the service user)."
            ),
        )

    return CheckResult(
        name=_NAME,
        severity="warn",
        passed=True,
        message=f"memory_dir is a git repository with a commit identity: {memory_dir}",
    )
