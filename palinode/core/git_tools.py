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
import tempfile
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


# ── Mutation choke point ─────────────────────────────────────────────────────
#
# Every path that mutates a memory file routes its write through
# :func:`write_memory_file` and its commit through :func:`commit_memory_file` /
# :func:`commit_memory_files`. Concentrating both here gives the substrate a
# single observation point for the mutation chain — a future signer hooks one
# function instead of the formerly-scattered ``open(w)`` / ``git add`` sites
# (save, write-time dedup, consolidation ops, ttl-archive, migration). It also
# enforces the one-mutation-one-commit invariant: a commit stages an explicit
# list of files, never a repo-wide ``git add *.md`` sweep that would conflate
# unrelated working-tree edits under one message.


def _fsync_directory(path: str) -> None:
    """Flush directory metadata so a rename survives a crash."""
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_memory_file(file_path: str, content: str) -> None:
    """Atomically write ``content`` to ``file_path`` (temp + fsync + rename).

    The single write primitive for memory-file mutations. Crash-safe: the
    target is only replaced once the temp file is durably on disk, so a torn
    write can never leave a half-written memory file. Preserves the existing
    file's permission bits when overwriting.
    """
    directory = os.path.dirname(file_path) or "."
    prefix = f".{os.path.basename(file_path)}."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        if os.path.exists(file_path):
            os.fchmod(fd, os.stat(file_path).st_mode & 0o777)

        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            fd = -1
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        os.replace(tmp_path, file_path)
        _fsync_directory(directory)
    except Exception:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def commit_memory_files(file_paths: list[str], message: str) -> bool:
    """Stage an explicit list of files and commit them in one commit.

    The single commit primitive. ``file_paths`` may be absolute or relative to
    the data repo; each is staged explicitly (never a ``git add *.md`` sweep),
    so the commit captures exactly the files this mutation touched and nothing
    else dirty in the working tree.

    No-op (returns False) when ``config.git.auto_commit`` is disabled or no
    paths are given. Returns True when the commit subprocess was spawned
    without raising (a "nothing to commit" exit is treated as success — the
    caller asked to commit and there was nothing new, which is not an error).
    """
    if not config.git.auto_commit or not file_paths:
        return False

    rels = []
    for p in file_paths:
        rels.append(os.path.relpath(p, config.memory_dir) if os.path.isabs(p) else p)

    try:
        _run_git("add", "--", *rels)
        _run_git("commit", "-m", message)
        # Mirror the #386 contract: "committed" means the commit subprocess was
        # spawned without raising. A non-zero exit (e.g. "nothing to commit")
        # is not an error — the caller asked to commit and there was nothing
        # new, which is benign. Genuine I/O failures (git missing, timeout)
        # raise and are caught below.
        return True
    except (subprocess.SubprocessError, OSError) as e:
        logger.error("Git commit failed for %r: %s", rels, e, exc_info=True)
        return False


def commit_memory_file(file_path: str, message: str) -> bool:
    """Stage and commit a single memory file (one mutation = one commit).

    Thin wrapper over :func:`commit_memory_files` for the common single-file
    case. See that function for the staging/return contract.
    """
    return commit_memory_files([file_path], message)


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
        # Frontmatter provenance is enrichment only — blame works without it,
        # so a parse/read failure here is provably inert (docs/logging.md
        # silent-except carve-out).
        pass

    # Get git blame
    result = _run_git("blame", "--date=short", "-w", file_path)

    if result.returncode != 0:
        # Surface to the log, not just the returned string (#337) — git
        # failures returned as strings otherwise never reach journalctl.
        logger.warning(
            "git blame failed op=blame file_path=%s returncode=%d stderr=%r",
            file_path, result.returncode, result.stderr.strip(),
        )
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
        # A failed rollback is operator-critical and was previously only a
        # return value — log at ERROR (#337).
        logger.error(
            "rollback checkout failed op=rollback file_path=%s target=%s "
            "returncode=%d stderr=%r",
            file_path, target, checkout.returncode, checkout.stderr.strip(),
        )
        return f"Rollback failed: {checkout.stderr}"

    # Commit the revert
    message = f"palinode: rollback {file_path} to {target}"
    _run_git("add", file_path)
    commit = _run_git("commit", "-m", message)
    if commit.returncode != 0:
        # The checkout landed but the commit did not — the working tree is now
        # dirty (rolled-back content uncommitted). Surface it so the operator
        # knows the rollback is half-applied (#337). "nothing to commit" also
        # lands here but is benign; stderr distinguishes the two.
        logger.warning(
            "rollback commit failed op=commit file_path=%s target=%s "
            "returncode=%d stderr=%r",
            file_path, target, commit.returncode, commit.stderr.strip(),
        )

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
        pre_commit = _run_git(
            "commit", "-m",
            f"palinode: auto-commit before push ({_utc_now().strftime('%Y-%m-%d %H:%M')})",
        )
        if pre_commit.returncode != 0:
            # A failed pre-push commit silently proceeds to push stale state —
            # surface it (#337). "nothing to commit" also lands here but is
            # benign; stderr distinguishes a real failure.
            logger.warning(
                "auto-commit before push failed op=commit returncode=%d stderr=%r",
                pre_commit.returncode, pre_commit.stderr.strip(),
            )

    result = _run_git("push", "origin", "main")
    if result.returncode != 0:
        # Push failures (no remote, auth, not-a-repo) were returned as a string
        # only — log so backup-sync drift is visible in journalctl (#337).
        logger.warning(
            "git push failed op=push returncode=%d stderr=%r",
            result.returncode, result.stderr.strip(),
        )
        return f"Push failed: {result.stderr}"
    
    return f"Pushed to origin/main successfully.\n{result.stderr.strip()}"


def recent_commits(
    days: int = 7,
    limit: int = 50,
    message_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """List recent commits across the whole memory repo (read-only).

    Repo-wide counterpart to :func:`history` (which is per-file). Backs the
    UI's recent-changes and compaction views — neither triggers any write; this
    is a pure ``git log`` read through the module's single ``_run_git``
    chokepoint.

    Args:
        days: Look back this many days.
        limit: Maximum number of commits to return.
        message_prefix: When set, only commits whose subject starts with this
            string are returned (e.g. ``"palinode: compaction"`` /
            ``"palinode: nightly"`` to isolate consolidation commits).

    Returns:
        List of dicts (newest first) with keys: ``hash``, ``date`` (ISO-8601),
        ``message``, and ``files`` (the relative paths the commit touched).
        Empty list on any git error or empty repo.
    """
    since = (_utc_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    # %x00 (NUL) record separator so subjects containing our "|" can't confuse
    # the parse; name-only file list follows each header line.
    result = _run_git(
        "log", f"-{limit}", f"--since={since}",
        "--name-only", "--format=%x00%h|%aI|%s", "HEAD",
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    commits: list[dict[str, Any]] = []
    # Records are separated by the NUL we prepended to each header.
    for record in result.stdout.split("\x00"):
        record = record.strip("\n")
        if not record:
            continue
        lines = record.split("\n")
        header = lines[0]
        parts = header.split("|", 2)
        if len(parts) != 3:
            continue
        hash_short, date, message = parts
        if message_prefix and not message.startswith(message_prefix):
            continue
        files = [ln for ln in lines[1:] if ln.strip()]
        commits.append(
            {
                "hash": hash_short,
                "date": date,
                "message": message,
                "files": files,
            }
        )
    return commits


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
