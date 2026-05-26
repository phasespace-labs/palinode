"""
Palinode Git Tools — Memory provenance, change tracking, and rollback.

Every memory file is git-versioned. This module exposes git's power
as clean Python functions: diff, blame, log, rollback, push.

All operations run against the data repo (config.memory_dir).
"""
from __future__ import annotations

import os
import re
import subprocess
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from palinode.core.config import config

logger = logging.getLogger("palinode.git_tools")


def _resolve_memory_path(file_path: str) -> str:
    """Resolve a relative file_path against memory_dir and reject traversal.

    Returns the validated relative path. Raises ValueError if the resolved
    path escapes memory_dir.
    """
    if "\x00" in file_path:
        raise ValueError("Null bytes are not allowed in file paths")
    base = os.path.realpath(config.memory_dir)
    resolved = os.path.realpath(os.path.join(base, file_path))
    if not resolved.startswith(base + os.sep) and resolved != base:
        raise ValueError(f"Path traversal rejected: {file_path}")
    return file_path


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _run_git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run a git command in the memory data directory.

    Security note: this is the only entry point through which any palinode
    code in this module touches ``subprocess``. The argv-list form is used
    deliberately — never ``shell=True``, never string-interpolated commands
    — so user-supplied inputs (file paths, commit messages, search terms,
    refs) cannot inject shell metacharacters. Callers MUST forward their
    arguments through this helper rather than constructing their own
    subprocess invocations.

    Args:
        *args: Git arguments (e.g., 'log', '--oneline', '-10').
        check: If True, raise on non-zero exit.

    Returns:
        CompletedProcess with stdout and stderr.
    """
    # bandit: argv-form invocation; shell=False (default). User-supplied
    # arguments are passed as separate list elements, not interpolated into
    # a shell command string. See module docstring for the security model.
    return subprocess.run(  # nosec B603 - argv form, no shell, validated cwd
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=config.memory_dir,
        check=check,
    )


def diff(days: int = 7, paths: list[str] | None = None) -> str:
    """Show what memory files changed in the last N days.

    Returns a human-readable summary of additions, modifications,
    and deletions. Includes both a stat summary and the actual
    content changes (truncated per file).

    Args:
        days: Look back this many days.
        paths: Optional list of paths to filter (e.g., ['projects/', 'decisions/']).
            Defaults to all memory directories.

    Returns:
        Formatted diff output. Empty string if no changes.
    """
    # Find the commit closest to N days ago
    since = (_utc_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    # Get the first commit after the cutoff date
    result = _run_git("log", "--after", since, "--reverse", "--format=%H", "-1")
    base_commit = result.stdout.strip()
    
    if not base_commit:
        return f"No commits found in the last {days} days."

    # Stat summary
    cmd = ["diff", "--stat", f"{base_commit}^..HEAD"]
    filter_paths = paths or ["people/", "projects/", "decisions/", "insights/", "daily/"]
    cmd.extend(["--", *filter_paths])
    stat = _run_git(*cmd)

    # Content diff (truncated)
    cmd_diff = ["diff", "--no-color", "-U2", f"{base_commit}^..HEAD"]
    cmd_diff.extend(["--", *filter_paths])
    content = _run_git(*cmd_diff)

    # Truncate long diffs
    lines = content.stdout.split("\n")
    if len(lines) > 200:
        content_text = "\n".join(lines[:200]) + f"\n\n... ({len(lines) - 200} more lines truncated)"
    else:
        content_text = content.stdout

    output = f"## Memory Changes (last {days} days)\n\n"
    output += f"### Summary\n```\n{stat.stdout}\n```\n\n"
    if content_text.strip():
        output += f"### Changes\n```diff\n{content_text}\n```"
    else:
        output += "No content changes in the specified paths."
    
    return output


def blame(file_path: str, search: str | None = None) -> str:
    """Show when each line of a memory file was last changed, with origin dates.

    Combines git blame (when was this line last modified?) with frontmatter
    provenance (when was this memory originally captured?). This is critical
    for backfilled memories: git shows the migration date, but frontmatter
    shows the true origin date.

    Output format:
        [git: 2026-03-29, origin: 2026-02-11, source: mem0-backfill] content...
        [git: 2026-04-06, origin: 2026-04-06, source: consolidation] content...

    Args:
        file_path: Relative path within the data repo (e.g., 'projects/my-app.md').
        search: Optional search term to filter lines.

    Returns:
        Formatted blame output with both git dates and origin provenance.
    """
    file_path = _resolve_memory_path(file_path)
    full_path = os.path.join(config.memory_dir, file_path)
    if not os.path.exists(full_path):
        return f"File not found: {file_path}"

    # Extract frontmatter provenance
    origin_date = ""
    source = ""
    try:
        with open(full_path) as f:
            content = f.read()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm = parts[1]
                # Extract created_at
                match = re.search(r"created_at:\s*['\"]?(\d{4}-\d{2}-\d{2})", fm)
                if match:
                    origin_date = match.group(1)
                # Extract source
                match = re.search(r"source:\s*['\"]?([^\s'\"]+)", fm)
                if match:
                    source = match.group(1)
    except Exception:
        pass

    # Get git blame
    result = _run_git("blame", "--date=short", "-w", file_path)

    if result.returncode != 0:
        return f"Git blame failed: {result.stderr}"

    # Build header with provenance context
    header = f"## Blame: {file_path}\n"
    if origin_date or source:
        header += f"Origin: {origin_date or 'unknown'}"
        if source:
            header += f" | Source: {source}"
        header += "\n"
        # Check if git date differs from origin (indicates backfill)
        first_line = result.stdout.split("\n")[0] if result.stdout else ""
        git_date_match = re.search(r"\d{4}-\d{2}-\d{2}", first_line)
        if git_date_match and origin_date and git_date_match.group() != origin_date:
            header += f"Note: Git shows {git_date_match.group()} (migration date). "
            header += f"True origin is {origin_date} (from {source or 'external system'}).\n"
    header += "\n"

    blame_output = result.stdout

    if search:
        lines = [
            line for line in blame_output.split("\n")
            if search.lower() in line.lower()
        ]
        if not lines:
            return f'{header}No lines matching "{search}" in {file_path}'
        return header + "\n".join(lines)

    return header + blame_output


def history(
    file_path: str,
    limit: int = 20,
    detail: str = "summary",
) -> list[dict[str, str]]:
    """Show the change history of a memory file.

    Returns a list of commits that touched this file, with diff stats.
    Uses ``--follow`` to track renames.

    Args:
        file_path: Relative path within the data repo.
        limit: Maximum number of commits to return.
        detail: ``"summary"`` (default) returns hash/date/message/stats;
            ``"full"`` additionally includes the full unified diff for each
            commit so the caller can see exactly what changed (commit-level
            evolution view, formerly ``palinode_timeline``).

    Returns:
        List of dicts with keys: hash, date, message, stats.
        When ``detail="full"``, each dict also has a ``diff`` key.
        Empty list if no history found.
    """
    file_path = _resolve_memory_path(file_path)
    if not os.path.exists(os.path.join(config.memory_dir, file_path)):
        return []

    # Get commits that touched this file (--follow tracks renames)
    result = _run_git(
        "log", f"-{limit}", "--format=%h|%aI|%s",
        "--follow", "--", file_path
    )

    if not result.stdout.strip():
        return []

    commits = []
    for entry in result.stdout.strip().split("\n"):
        parts = entry.split("|", 2)
        if len(parts) == 3:
            hash_short, date, message = parts
            # Get the diff stat for this specific commit
            stat = _run_git("diff", "--stat", f"{hash_short}^..{hash_short}", "--", file_path)
            stat_line = stat.stdout.strip().split("\n")[-1] if stat.stdout.strip() else ""
            stats = stat_line.strip() if stat_line and "changed" in stat_line else ""
            commit: dict[str, str] = {
                "hash": hash_short,
                "date": date,
                "message": message,
                "stats": stats,
            }
            if detail == "full":
                diff_result = _run_git(
                    "show", "--unified=3", f"{hash_short}", "--", file_path
                )
                commit["diff"] = diff_result.stdout.strip()
            commits.append(commit)

    return commits


def rollback(file_path: str, commit: str | None = None, dry_run: bool = False) -> str:
    """Revert a memory file to a previous version.

    Creates a new commit that restores the file. The old version
    is preserved in git history (nothing is lost).

    Args:
        file_path: Relative path within the data repo.
        commit: Target commit hash. Defaults to HEAD~1 (previous version).
        dry_run: If True, show what would change without applying.

    Returns:
        Description of what was (or would be) rolled back.
    """
    file_path = _resolve_memory_path(file_path)
    if not os.path.exists(os.path.join(config.memory_dir, file_path)):
        return f"File not found: {file_path}"

    target = commit or "HEAD~1"
    
    if dry_run:
        # Show what would change
        result = _run_git("diff", f"{target}..HEAD", "--", file_path)
        if not result.stdout.strip():
            return f"No differences between {target} and HEAD for {file_path}"
        lines = result.stdout.split("\n")
        preview = "\n".join(lines[:50])
        if len(lines) > 50:
            preview += f"\n... ({len(lines) - 50} more lines)"
        return f"## Dry Run: Rollback {file_path} to {target}\n\n```diff\n{preview}\n```"

    # Perform the rollback
    checkout = _run_git("checkout", target, "--", file_path)
    if checkout.returncode != 0:
        return f"Rollback failed: {checkout.stderr}"

    # Commit the revert
    message = f"palinode: rollback {file_path} to {target}"
    _run_git("add", file_path)
    _run_git("commit", "-m", message)
    
    return f"Rolled back {file_path} to {target}. Committed as: {message}"


def push() -> str:
    """Push memory changes to the remote repository.

    Syncs the local data repo to GitHub for backup and cross-machine access.

    Returns:
        Push result or error message.
    """
    # Check if there are unpushed commits
    status = _run_git("status", "--porcelain")
    if status.stdout.strip():
        # Auto-commit any uncommitted changes first (only markdown, not journals)
        _run_git("add", "*.md", "**/*.md")
        _run_git("commit", "-m", f"palinode: auto-commit before push ({_utc_now().strftime('%Y-%m-%d %H:%M')})")
    
    result = _run_git("push", "origin", "main")
    if result.returncode != 0:
        return f"Push failed: {result.stderr}"
    
    return f"Pushed to origin/main successfully.\n{result.stderr.strip()}"


def commit_count(days: int = 7) -> dict[str, Any]:
    """Get commit statistics for the memory repo.

    Args:
        days: Look back this many days.

    Returns:
        Dict with total_commits, files_changed, insertions, deletions.
    """
    since = (_utc_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    # Count commits in last N days
    result = _run_git("log", "--oneline", f"--since={since}", "HEAD")
    commit_count = len(result.stdout.strip().splitlines()) if result.returncode == 0 else 0
    
    # Get shortstat for changed files
    result2 = _run_git("diff", "--shortstat", f"HEAD@{{{days}days}}", "HEAD")
    summary = result2.stdout.strip() if result2.returncode == 0 and result2.stdout.strip() else f"{commit_count} commits"
    
    return {
        "period_days": days,
        "total_commits": commit_count,
        "summary": summary,
    }
