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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from palinode.core import path_guard
from palinode.core.config import config

logger = logging.getLogger("palinode.git_tools")

#: Re-exported so callers that need to catch the guard's typed error don't
#: have to import :mod:`palinode.core.path_guard` directly.
PathTraversalError = path_guard.PathTraversalError


def _resolve_memory_path(file_path: str) -> str:
    """Validate ``file_path`` is inside memory_dir; return it unchanged.

    Thin wrapper over :func:`palinode.core.path_guard.resolve_memory_path`
    — this module used to carry its own weaker ``os.path.realpath``-based
    guard with no absolute-path rejection, before the two path guards in the
    tree were unified into one. Every function below routes the
    ``file_path`` it was handed through this wrapper before touching git or
    the filesystem.

    Returns the original relative path, not the resolved absolute form:
    callers here pass it straight to ``git`` subcommands run with
    ``cwd=config.memory_dir``, which need the relative spelling.

    Raises:
        PathTraversalError: (a ``ValueError`` subclass) if ``file_path`` is
            absolute, contains a null byte, or resolves outside memory_dir.
    """
    path_guard.resolve_memory_path(file_path)
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
        encoding="utf-8",
        errors="replace",
        cwd=config.memory_dir,
        check=check,
    )


# ── Mutation choke point ─────────────────────────────────────────────────────
#
# Every path that mutates a memory file routes its write through
# :func:`write_memory_file` (or, for a rename/relocation, :func:`move_memory_file`)
# and its commit through :func:`commit_memory_file` / :func:`commit_memory_files`.
# Concentrating both here gives the substrate a single observation point for
# the mutation chain — a future signer hooks one function instead of the
# formerly-scattered ``open(w)`` / ``git add`` sites (save, write-time dedup,
# consolidation ops, ttl-archive, migration). It also enforces the
# one-mutation-one-commit invariant: a commit stages an explicit list of
# files, never a repo-wide ``git add *.md`` sweep that would conflate
# unrelated working-tree edits under one message.


def _is_windows() -> bool:
    """Platform probe for the fsync/chmod fallbacks, as one patchable seam.

    Reading ``os.name`` inline looks simpler, but a test faking Windows has to
    set it on the real ``os`` module — and since `write_memory_file` now
    validates its target through ``pathlib`` first, that global flip makes
    ``Path()`` try to build a ``WindowsPath`` on a POSIX host and raise
    ``NotImplementedError`` before the branch under test is ever reached.
    Patching this function fakes the platform for the code that cares without
    perturbing every other ``os.name`` reader in the process.
    """
    return os.name == "nt"


def _fsync_directory(path: str) -> None:
    """Flush directory metadata so a rename survives a crash."""
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _validate_write_target(file_path: str) -> None:
    """Precondition for :func:`write_memory_file` / :func:`move_memory_file`:
    reject a target outside ``memory_dir``.

    Unlike the read-side helpers above (``blame``, ``history``, ``rollback``,
    …), which take the memory-relative spelling the git subcommands need,
    every current write-side caller passes an *absolute* path it already
    built under ``config.memory_dir`` (``os.path.join(config.memory_dir,
    ...)``). :func:`palinode.core.path_guard.resolve_memory_path` rejects any
    absolute input outright — that is its contract for a caller-supplied,
    externally-facing path — so an absolute ``file_path`` here is first
    rewritten relative to the memory root before crossing the guard; a
    relative ``file_path`` is passed through unchanged. Either way, the
    guard's own ``.resolve()`` + containment check is what actually decides:
    the rewrite only avoids rejecting the write choke point's own internal
    callers on a rule aimed at a different threat model (an untrusted
    caller-supplied path arriving as ``/etc/passwd``).

    Raises:
        PathTraversalError: ``file_path`` contains a null byte, or resolves
            (after the above normalization) outside ``memory_dir``.
    """
    candidate = file_path
    if os.path.isabs(file_path):
        base = path_guard.memory_base_dir()  # already realpath'd
        # realpath file_path too before diffing against the (already
        # realpath'd) base — otherwise a path built through an unresolved
        # symlink (macOS: /tmp -> /private/tmp, /var -> /private/var; the
        # pytest tmp_path fixture routinely hands out /var/folders/... while
        # memory_base_dir() resolves it to /private/var/folders/...) produces
        # a relpath dominated by leading `..` segments that legitimately
        # resolves outside memory_dir once path_guard re-joins and
        # re-resolves it below — rejecting an in-tree write as traversal.
        # realpath is safe on a not-yet-created file: it resolves symlinks in
        # whatever prefix exists and appends the rest verbatim.
        try:
            candidate = os.path.relpath(os.path.realpath(file_path), base)
        except ValueError:
            # Different drive on Windows — cannot possibly be inside memory_dir.
            raise path_guard.PathTraversalError(file_path) from None
    path_guard.resolve_memory_path(candidate)


def write_memory_file(file_path: str, content: str) -> None:
    """Atomically write ``content`` to ``file_path`` (temp + fsync + rename).

    The single write primitive for memory-file mutations. Validates
    ``file_path`` resolves inside ``memory_dir`` (see
    :func:`_validate_write_target`) before touching disk — the traversal
    guard folded in as a precondition here, rather than left to the
    read/provenance side only. Crash-safe: the target is only replaced once
    the temp file is durably on disk, so a torn write can never leave a
    half-written memory file. Preserves the existing file's permission bits
    when overwriting.

    Raises:
        PathTraversalError: ``file_path`` resolves outside ``memory_dir``.
    """
    _validate_write_target(file_path)
    directory = os.path.dirname(file_path) or "."
    prefix = f".{os.path.basename(file_path)}."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        if os.path.exists(file_path):
            mode = os.stat(file_path).st_mode & 0o777
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(fd, mode)
            else:
                # ``os.fchmod`` is unavailable on Windows before Python 3.13.
                os.chmod(tmp_path, mode)

        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            fd = -1
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        os.replace(tmp_path, file_path)
        # Windows cannot open a directory for fsync, so the rename's metadata
        # durability is weaker there after a crash.
        if not _is_windows():
            _fsync_directory(directory)
    except Exception:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def move_memory_file(src_path: str, dst_path: str) -> None:
    """Atomically relocate a memory file within ``memory_dir`` (e.g. archiving
    a daily note into ``archive/<year>/``).

    The move counterpart to :func:`write_memory_file`: both endpoints cross
    the same traversal guard, then the rename happens via ``os.replace``
    (atomic on a single filesystem, which every path under ``memory_dir``
    is). Where the platform supports it, the destination directory is fsynced
    so the rename survives a crash; Windows cannot open a directory for fsync,
    so its rename metadata has weaker crash durability. Does not create the
    destination directory — callers that need one create it first, same as
    :func:`write_memory_file` never creates ``os.path.dirname(file_path)``.

    This function only moves the file; it does not commit. A caller commits
    the result via ``commit_memory_files([src_path, dst_path], message)`` —
    git recognizes the now-missing source as a staged deletion and the
    destination as a staged addition, landing the rename as one commit.

    Raises:
        PathTraversalError: either path resolves outside ``memory_dir``.
    """
    _validate_write_target(src_path)
    _validate_write_target(dst_path)
    directory = os.path.dirname(dst_path) or "."
    os.replace(src_path, dst_path)
    # Windows cannot open a directory for fsync, so the rename's metadata
    # durability is weaker there after a crash.
    if not _is_windows():
        _fsync_directory(directory)


@dataclass(frozen=True)
class CommitOutcome:
    """Result of :func:`try_commit_memory_files`.

    ``committed`` is True only when git actually accepted the commit (or had
    nothing new to commit for the given paths — the caller asked to commit
    and there was nothing new, which is not an error). ``error`` carries the
    reason when ``committed`` is False and a commit was attempted: git's
    stderr (trimmed) for a non-zero exit — ``fatal: not a git repository``,
    ``Author identity unknown``, an ``index.lock`` collision — or the
    exception text when the subprocess could not be spawned. ``None`` when
    committed, or when no commit was attempted (auto_commit off, no paths).
    """

    committed: bool
    error: str | None = None


def _git_failure_reason(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    first_line = text.splitlines()[0] if text else ""
    return f"exit {result.returncode}: {first_line or '(no output)'}"


def try_commit_memory_files(file_paths: list[str], message: str) -> CommitOutcome:
    """Stage an explicit list of files and commit them in one commit.

    The single commit primitive. ``file_paths`` may be absolute or relative to
    the data repo; each is staged explicitly (never a ``git add *.md`` sweep),
    so the commit captures exactly the files this mutation touched and nothing
    else dirty in the working tree.

    No-op (``committed=False, error=None``) when ``config.git.auto_commit`` is
    disabled or no paths are given. Otherwise the outcome is truthful: a
    non-zero ``git add`` exit (not a repo, bad path) or a ``git commit`` exit
    that is not the benign "nothing to commit" case (missing identity, an
    ``index.lock`` held by another writer, a hook rejection) yields
    ``committed=False`` with the reason in ``error`` and an ERROR log line.
    Before this the helper returned True whenever the subprocess merely
    *spawned*, so a memory dir that was never ``git init``-ed reported
    ``git_committed=True`` on every save (the git_committed truthfulness fix).

    "Nothing to commit" is detected locale-independently: a ``git commit``
    exit of 1 followed by ``git diff --cached --quiet`` succeeding on the
    same paths means the index holds no change for them.
    """
    if not config.git.auto_commit or not file_paths:
        return CommitOutcome(False)

    rels = []
    for p in file_paths:
        rels.append(os.path.relpath(p, config.memory_dir) if os.path.isabs(p) else p)

    try:
        add = _run_git("add", "--", *rels)
        if add.returncode != 0:
            reason = _git_failure_reason(add)
            logger.error("Git add failed for %r: %s", rels, reason)
            return CommitOutcome(False, reason)
        commit = _run_git("commit", "-m", message)
        if commit.returncode == 0:
            return CommitOutcome(True)
        if commit.returncode == 1:
            staged = _run_git("diff", "--cached", "--quiet", "--", *rels)
            if staged.returncode == 0:
                return CommitOutcome(True)
        reason = _git_failure_reason(commit)
        logger.error("Git commit failed for %r: %s", rels, reason)
        return CommitOutcome(False, reason)
    except (subprocess.SubprocessError, OSError) as e:
        logger.error("Git commit failed for %r: %s", rels, e, exc_info=True)
        return CommitOutcome(False, str(e))


def commit_memory_files(file_paths: list[str], message: str) -> bool:
    """Boolean form of :func:`try_commit_memory_files` for callers that only
    need to know whether the commit landed. Same staging/return contract;
    the failure reason is logged there and available via the outcome form.
    """
    return try_commit_memory_files(file_paths, message).committed


def commit_memory_file(file_path: str, message: str) -> bool:
    """Stage and commit a single memory file (one mutation = one commit).

    Thin wrapper over :func:`commit_memory_files` for the common single-file
    case. See that function for the staging/return contract.
    """
    return commit_memory_files([file_path], message)


#: Directories the default (caller passed no ``paths``) diff reports on.
#:
#: Every category the save path writes to — ``people``/``decisions``/
#: ``projects``/``insights``/``research``/``inbox`` — plus the ``daily``
#: journal. ``research/`` and ``inbox/`` were absent from the original list,
#: which made two real save categories invisible to the "what changed?"
#: surface; ``inbox/`` is where the ADR-015 deterministic-monitor writers land
#: their incidents, so the omission hid exactly the telemetry an operator
#: queries this tool to find. ``tests/test_git_tools_diff.py`` pins this
#: against the save-path category map so a new category cannot be added
#: without becoming visible here.
DEFAULT_DIFF_PATHS: tuple[str, ...] = (
    "people/",
    "projects/",
    "decisions/",
    "insights/",
    "research/",
    "inbox/",
    "daily/",
)


def _empty_tree() -> str:
    """The repo's empty-tree object id, derived (not hardcoded).

    Diffing against it yields "everything that currently exists", which is the
    correct base when the whole history is younger than the requested window.
    Derived via ``git hash-object`` rather than pinned to the well-known SHA-1
    constant so the value is right in a SHA-256 repository too.
    """
    return _run_git("hash-object", "-t", "tree", os.devnull).stdout.strip()


def _diff_window_base(since: str) -> str | None:
    """Resolve the tree-ish that a ``since`` cutoff should be diffed against.

    Returns the newest commit at or before ``since`` — so ``base..HEAD`` spans
    exactly the commits inside the window — or the empty tree when every commit
    in the repo falls inside it. Returns ``None`` when the repo has no commits.

    Anchoring on the *preceding* commit is what makes the lookback mean what it
    says. Picking a base with ``git log --after=<since> --reverse -1`` does not
    return the oldest commit in the window: ``-1`` limits the newest-first walk
    and ``--reverse`` then reverses an already-single-element result. The base
    therefore came back as HEAD, and every lookback silently collapsed to "the
    most recent commit" no matter how many days were asked for.
    """
    if _run_git("rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        return None
    base = _run_git("rev-list", "-1", f"--before={since}", "HEAD").stdout.strip()
    return base or _empty_tree()


def _changed_files(base: str, filter_paths: list[str] | None = None) -> set[str]:
    """Paths changed between ``base`` and HEAD, optionally narrowed to ``filter_paths``.

    NUL-separated so a path containing a space or a quote is returned verbatim
    rather than in git's quoted form.
    """
    cmd = ["diff", "--name-only", "-z", base, "HEAD"]
    if filter_paths:
        cmd.extend(["--", *filter_paths])
    result = _run_git(*cmd)
    return {p for p in result.stdout.split("\x00") if p}


def _by_top_level(file_paths: set[str]) -> list[tuple[str, int]]:
    """Group paths by their top-level directory, most-changed first."""
    counts: dict[str, int] = {}
    for path in file_paths:
        top = f"{path.split('/', 1)[0]}/" if "/" in path else path
        counts[top] = counts.get(top, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def diff(days: int = 7, paths: list[str] | None = None) -> str:
    """Show what memory files changed in the last N days.

    Returns a human-readable summary of additions, modifications, and
    deletions: a stat summary, the actual content changes (truncated), and —
    when the path filter hid anything — an explicit account of what was left
    out.

    This is a diagnostic surface, so it states its own blind spots. A filter
    that silently drops changes turns "I have no data" into "I have proof of
    absence", and a caller asking "did anything change?" acts on the answer.
    Every exclusion this function applies is therefore reported: never a bare
    "no changes" when changes existed and were filtered away.

    Note that the filter is by *path* only. Unlike default semantic recall,
    which hard-excludes ``metadata.kind: telemetry`` under ADR-015 §2.3, this
    view applies no ``kind`` predicate — machine and monitor writes appear the
    same as any other change. Recall exclusion protects ranked relevance;
    provenance must not hide anything it was asked about.

    Args:
        days: Look back this many days.
        paths: Optional list of paths to filter (e.g., ['projects/', 'decisions/']).
            Defaults to :data:`DEFAULT_DIFF_PATHS`.

    Returns:
        Formatted diff output.
    """
    since = (_utc_now() - timedelta(days=days)).strftime("%Y-%m-%d")

    base = _diff_window_base(since)
    if base is None:
        return f"No commits found in the last {days} days."

    caller_filtered = bool(paths)
    filter_paths = list(paths) if paths else list(DEFAULT_DIFF_PATHS)
    filter_label = ", ".join(filter_paths)

    changed_all = _changed_files(base)
    if not changed_all:
        return f"No memory files changed in the last {days} days."

    changed_shown = _changed_files(base, filter_paths)
    hidden = changed_all - changed_shown

    # Stat summary
    stat = _run_git("diff", "--stat", base, "HEAD", "--", *filter_paths)

    # Content diff (truncated)
    content = _run_git("diff", "--no-color", "-U2", base, "HEAD", "--", *filter_paths)

    # Truncate long diffs
    lines = content.stdout.split("\n")
    if len(lines) > 200:
        content_text = "\n".join(lines[:200]) + f"\n\n... ({len(lines) - 200} more lines truncated)"
    else:
        content_text = content.stdout

    output = f"## Memory Changes (last {days} days)\n\n"
    output += f"### Summary\n```\n{stat.stdout}\n```\n\n"

    if hidden:
        whose = "your path filter" if caller_filtered else "the default path filter"
        breakdown = " · ".join(f"{top} {count}" for top, count in _by_top_level(hidden))
        output += (
            f"### Not shown\n"
            f"{len(hidden)} changed file(s) in this window were excluded by "
            f"{whose} ({filter_label}):\n"
            f"  {breakdown}\n"
            f"Re-run with `paths` naming those directories to see them.\n\n"
        )

    if content_text.strip():
        output += f"### Changes\n```diff\n{content_text}\n```"
    elif hidden:
        output += (
            f"No changes under {filter_label} — but {len(hidden)} file(s) did change "
            f"elsewhere in this window (see 'Not shown' above)."
        )
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
        with open(full_path, encoding="utf-8") as f:
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
        # Surface to the log, not just the returned string — git
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
            evolution view).

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


def first_commit(file_path: str) -> dict[str, str] | None:
    """Return the earliest commit that touched a memory file (its creation).

    The provenance counterpart to :func:`history` (which lists recent changes,
    newest-first): this answers "when was this fact first saved" by walking the
    full ``--follow`` log and taking the oldest entry. Returns a dict with keys
    ``hash``, ``date`` (ISO-8601), ``author``, and ``message``, or ``None`` when
    the file is absent or has no git history.
    """
    file_path = _resolve_memory_path(file_path)
    if not os.path.exists(os.path.join(config.memory_dir, file_path)):
        return None

    # Full log (no -N limit — a bounded limit would combine badly with --reverse,
    # which reverses only the already-limited window). Memory files are small, so
    # the whole-history read is cheap; take the last (oldest) line.
    result = _run_git(
        "log", "--format=%h|%aI|%an|%s", "--follow", "--", file_path
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    oldest = result.stdout.strip().split("\n")[-1]
    parts = oldest.split("|", 3)
    if len(parts) != 4:
        return None
    hash_short, date, author, message = parts
    return {"hash": hash_short, "date": date, "author": author, "message": message}


def last_commit(file_path: str) -> dict[str, str] | None:
    """Return the most recent commit that touched a memory file.

    The newest-end counterpart to :func:`first_commit`: "when did this file last
    change on disk", as recorded by git. Same return shape (``hash``, ``date``,
    ``author``, ``message``) and the same ``None`` for an absent file or a path
    with no git history. Single ``git log`` call — unlike :func:`history`, which
    also shells out for a per-commit ``--stat``.
    """
    file_path = _resolve_memory_path(file_path)
    if not os.path.exists(os.path.join(config.memory_dir, file_path)):
        return None

    result = _run_git(
        "log", "-1", "--format=%h|%aI|%an|%s", "--follow", "--", file_path
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    parts = result.stdout.strip().split("\n")[0].split("|", 3)
    if len(parts) != 4:
        return None
    hash_short, date, author, message = parts
    return {"hash": hash_short, "date": date, "author": author, "message": message}


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
        # return value — log at ERROR.
        logger.error(
            "rollback checkout failed op=rollback file_path=%s target=%s "
            "returncode=%d stderr=%r",
            file_path, target, checkout.returncode, checkout.stderr.strip(),
        )
        return f"Rollback failed: {checkout.stderr}"

    # Commit the revert through the choke point (commit_memory_files) rather
    # than a raw add + commit — the fourth commit-message shape this module
    # used to carry alongside commit_memory_file/commit_memory_files/push().
    # Note: this now also respects config.git.auto_commit, same as every
    # other commit site in this module; the raw calls it replaces did not.
    message = f"palinode: rollback {file_path} to {target}"
    committed = commit_memory_files([file_path], message)
    if not committed:
        # The checkout landed but the commit did not — the working tree is now
        # dirty (rolled-back content uncommitted). Surface it so the operator
        # knows the rollback is half-applied. commit_memory_files already logs
        # genuine I/O failures (subprocess errors) at ERROR with a stack
        # trace; this WARNING covers the other reason it can return False —
        # config.git.auto_commit is off — which isn't itself an error but
        # still leaves this rollback's revert uncommitted.
        logger.warning(
            "rollback commit did not complete op=commit file_path=%s target=%s",
            file_path, target,
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
    dirty = status.stdout.strip()
    if dirty:
        # Auto-commit any uncommitted changes first — an explicit file list,
        # never the repo-wide `*.md` / `**/*.md` sweep this module's own
        # docstring forbids elsewhere. `git status --porcelain` prefixes each
        # line with a two-character status code; a rename entry reads
        # "old -> new", of which only the destination is still on disk to
        # add. Quoted paths (spaces/unicode under core.quotepath) are
        # unquoted so the pathspec matches the real filename.
        md_files: list[str] = []
        for line in dirty.split("\n"):
            if not line:
                continue
            entry = line[3:]
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            entry = entry.strip('"')
            if entry.endswith(".md"):
                md_files.append(entry)

        if md_files:
            _run_git("add", "--", *md_files)
            pre_commit = _run_git(
                "commit", "-m",
                f"palinode: auto-commit before push ({_utc_now().strftime('%Y-%m-%d %H:%M')})",
            )
            if pre_commit.returncode != 0:
                # A failed pre-push commit silently proceeds to push stale state —
                # surface it. "nothing to commit" also lands here but is
                # benign; stderr distinguishes a real failure.
                logger.warning(
                    "auto-commit before push failed op=commit returncode=%d stderr=%r",
                    pre_commit.returncode, pre_commit.stderr.strip(),
                )

    result = _run_git("push", "origin", "main")
    if result.returncode != 0:
        # Push failures (no remote, auth, not-a-repo) were returned as a string
        # only — log so backup-sync drift is visible in journalctl.
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
