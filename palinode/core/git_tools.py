"""
Palinode Git Tools — Memory provenance, change tracking, and rollback.

Every memory file is git-versioned. This module exposes git's power
as clean Python functions: diff, blame, log, rollback, push.

All operations run against the data repo (config.memory_dir).
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import stat
import tempfile
import threading
import time
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
            try:
                os.unlink(tmp_path)
            except PermissionError:
                if not _is_windows():
                    raise
                # An overwrite copies the destination's read-only attribute to
                # our temporary file. Clear it only on that temporary file so
                # cleanup can succeed without changing the destination.
                os.chmod(tmp_path, os.stat(tmp_path).st_mode | stat.S_IWRITE)
                os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Temporary-file cleanup failed: %s", tmp_path, exc_info=True)
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


#: All commit sites share a lock for the canonical data-repository path.
#: RLock lets push protect its status snapshot and call the same primitive.
_COMMIT_LOCKS: dict[str, threading.RLock] = {}
_COMMIT_LOCKS_GUARD = threading.Lock()


def _repository_commit_lock() -> threading.RLock:
    repo = os.path.normcase(os.path.realpath(config.memory_dir))
    with _COMMIT_LOCKS_GUARD:
        return _COMMIT_LOCKS.setdefault(repo, threading.RLock())


#: Bounded backoff for index or reference lock contention. Git holds the lock for
#: milliseconds per commit, so a handful of short waits covers the watcher
#: committing a cross_refs update while the API commits a save. Base delays
#: in seconds, each jittered by up to +50%; the sum caps the worst case near
#: a second so a *stale* lock (a crashed git) still reports promptly. Module
#: constants so a test can shrink them.
_INDEX_LOCK_RETRIES = 5
_INDEX_LOCK_BACKOFF = (0.05, 0.1, 0.2, 0.3, 0.4)


def _is_index_lock_collision(result: subprocess.CompletedProcess) -> bool:
    """Git's "another process holds the index" signature, and nothing else.

    Exit 128 with ``index.lock`` in stderr. Other exit-128 reasons (not a
    repository, bad path spec) stay terminal — retrying those only delays
    the honest failure.
    """
    return result.returncode == 128 and "index.lock" in (result.stderr or "")


def _is_commit_lock_collision(result: subprocess.CompletedProcess) -> bool:
    """Only retry git's index/ref contention signatures, never other errors."""
    return _is_index_lock_collision(result) or (
        result.returncode == 128 and "cannot lock ref '" in (result.stderr or "")
    )


def _run_git_retrying_lock(*args: str) -> subprocess.CompletedProcess:
    """``_run_git`` that waits out transient index or reference contention.

    Returns the last result either way: a success, a non-lock failure on the
    first try, or the final lock failure after the retries are spent — the
    caller's error handling is unchanged.
    """
    result = _run_git(*args)
    for attempt in range(_INDEX_LOCK_RETRIES):
        if not _is_commit_lock_collision(result):
            break
        base = _INDEX_LOCK_BACKOFF[min(attempt, len(_INDEX_LOCK_BACKOFF) - 1)]
        delay = base * (1 + random.random() * 0.5)  # nosec B311 - jitter, not security
        logger.debug(
            "git %s hit %s (attempt %d/%d); retrying in %.0f ms",
            args[0], "index.lock" if _is_index_lock_collision(result) else "cannot lock ref",
            attempt + 1, _INDEX_LOCK_RETRIES, delay * 1000,
        )
        time.sleep(delay)
        result = _run_git(*args)
    return result


def try_commit_memory_files(file_paths: list[str], message: str) -> CommitOutcome:
    """Stage an explicit list of files and commit them in one commit.

    The single commit primitive. ``file_paths`` may be absolute or relative to
    the data repo; each is staged explicitly (never a ``git add *.md`` sweep),
    and the commit is limited to them with a pathspec (``git commit -m <msg>
    -- <paths>``), so it holds exactly the files this mutation touched:
    nothing else dirty in the working tree, and nothing another writer (an
    operator's hand edit) had already staged. Such unrelated staged changes
    stay staged, untouched. A path whose file is gone (a deletion, or the
    source side of :func:`move_memory_file`) commits as a removal, since it
    is still known to ``HEAD``.

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

    Concurrency: all commit sites share a process-wide lock per repository, so
    threads in one server never race each other for ``.git/index.lock``;
    index/ref contention with *another* process (the watcher committing while
    the API commits) is waited out with a short bounded backoff before it is
    reported. A whole-store ``bootstrap-ids`` racing the watcher's
    cross_refs commits stranded 82 files as dirty before either existed.
    """
    if not config.git.auto_commit or not file_paths:
        return CommitOutcome(False)

    rels = []
    for p in file_paths:
        rels.append(os.path.relpath(p, config.memory_dir) if os.path.isabs(p) else p)

    return _try_commit_paths(rels, message)


def _try_commit_paths(
    rels: list[str], message: str, *, add_paths: list[str] | None = None,
) -> CommitOutcome:
    """Shared stage/commit transaction; push can omit already-staged deletions.

    Each retry uses the original explicit paths. Failed changes remain on disk
    and in the index for the next commit of those paths; no unrelated file is
    swept in to recover them.
    """
    if add_paths is None:
        add_paths = rels
    try:
        with _repository_commit_lock():
            if add_paths:
                add = _run_git_retrying_lock("add", "--", *add_paths)
                if add.returncode != 0:
                    reason = _git_failure_reason(add)
                    logger.error("Git add failed for %r: %s", add_paths, reason)
                    return CommitOutcome(False, reason)
            commit = _run_git_retrying_lock("commit", "-m", message, "--", *rels)
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


# A ``git log --format=%h|%aI|%s`` header line. Anchored on the hash and the
# ISO-8601 date so a patch line can never be mistaken for one; the message is
# the remainder and may itself contain "|". The hash width spans what %h can
# actually produce -- core.abbrev goes down to 4, and an unabbreviated SHA-256
# is 64 -- because the old positional split accepted any width and narrowing
# it here would silently return no history at all.
_HISTORY_ENTRY_RE = re.compile(
    r"^(?P<hash>[0-9a-f]{4,64})\|(?P<date>\d{4}-\d{2}-\d{2}T[^|]*)\|(?P<message>.*)$"
)

# A ``--shortstat`` summary line, e.g. " 1 file changed, 2 insertions(+)".
_SHORTSTAT_RE = re.compile(r"^\s+\d+ files? changed")


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

    # One walk, not one spawn per commit: --shortstat yields the same summary
    # line the per-commit ``diff --stat`` tail used to, and -p yields what the
    # per-commit ``show`` did. Both are computed by the same --follow walk, so
    # they hold across renames -- a pathspec passed to a separate ``diff``/
    # ``show`` names the current path, which did not exist before the rename.
    # --no-color for the same reason diff() passes it: git honours
    # color.ui=always even down a pipe, and a painted "diff --git" line no
    # longer matches the prefix the shortstat guard keys on.
    args = ["log", "--no-color", f"-{limit}", "--format=%h|%aI|%s", "--shortstat"]
    if detail == "full":
        args += ["-p", "--unified=3"]
    args += ["--follow", "--", file_path]
    result = _run_git(*args)

    if not result.stdout.strip():
        return []

    commits: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    diff_lines: list[str] = []
    in_diff = False

    for line in result.stdout.split("\n"):
        header = _HISTORY_ENTRY_RE.match(line)
        if header:
            if current is not None:
                if detail == "full":
                    current["diff"] = "\n".join(diff_lines).strip()
                commits.append(current)
            current = {
                "hash": header.group("hash"),
                "date": header.group("date"),
                "message": header.group("message"),
                "stats": "",
            }
            diff_lines = []
            in_diff = False
            continue
        if current is None:
            continue
        if line.startswith("diff --git "):
            in_diff = True
        # Only before the patch body: a file's own content can contain a line
        # that reads like a shortstat, and under detail="full" that content is
        # in this same stream.
        elif not in_diff and _SHORTSTAT_RE.match(line):
            current["stats"] = line.strip()
            continue
        if detail == "full":
            diff_lines.append(line)

    if current is not None:
        if detail == "full":
            current["diff"] = "\n".join(diff_lines).strip()
        commits.append(current)

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
    with no git history. Single ``git log`` call, as :func:`history` now is.
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


def _retirement_markers(text: str, file_path: str) -> dict[str, dict[str, Any]]:
    """Every retirement one version of a memory file carries, keyed for diffing.

    Record-level: the shared lifecycle classifier's verdict on the frontmatter
    (``status: archived``, ``superseded_by``, retracted, expired, …) under one
    key, ``record``. In-body: each retracted mention (keyed by its opaque
    marker id) and each fact retired in place (keyed by its line). A key in
    the live file and absent from the rollback target is a retirement the
    rollback would undo.
    """
    import frontmatter as _frontmatter

    from palinode.core.lifecycle import (
        RETIRED_MENTION_RE,
        contains_retired_fact_text,
        eligibility,
    )

    try:
        post = _frontmatter.loads(text)
        meta, body = dict(post.metadata), post.content
    except Exception:
        # Unparseable frontmatter is not retired on any surface that reads it,
        # so it is not retired here either; the body is still scanned.
        meta, body = {}, text

    markers: dict[str, dict[str, Any]] = {}
    verdict = eligibility(meta, path=file_path)
    if verdict.retired:
        relation = verdict.reason
        if verdict.superseded_by and verdict.reason != "superseded_by":
            relation = f"{relation}, superseded_by: {verdict.superseded_by}"
        elif verdict.superseded_by:
            relation = f"superseded_by: {verdict.superseded_by}"
        markers["record"] = {
            "relation": relation,
            "superseded_by": verdict.superseded_by,
        }
    for match in RETIRED_MENTION_RE.finditer(body):
        marker_id = match.group(0).rsplit("r:", 1)[-1].rstrip("].")
        markers[f"mention:{marker_id}"] = {
            "relation": f"retracted mention r:{marker_id}",
            "superseded_by": None,
        }
    for line in body.splitlines():
        # A line carrying a mention marker is already counted, by its id.
        if contains_retired_fact_text(line) and not RETIRED_MENTION_RE.search(line):
            markers[f"fact:{line.strip()}"] = {
                "relation": "fact retired in place",
                "superseded_by": None,
            }
    return markers


def retirements_undone(file_path: str, target: str) -> list[dict[str, Any]]:
    """The retirements rolling ``file_path`` back to ``target`` would undo.

    ``rollback`` is a git-level revert and git does not know what a
    retirement is: reverting the commit that archived or superseded a memory
    deletes its ``status: archived`` / ``superseded_by`` with everything else,
    and the record comes back unmarked, current again. This compares
    the live file with its ``target`` version and returns one entry per
    retirement present now and absent there: ``record`` (the file),
    ``relation`` (what retired it), ``superseded_by``, and the ``commit`` /
    ``commit_subject`` that retired it (both ``None`` when the retirement is
    only in the uncommitted working tree).

    A target version git cannot read returns ``[]``: the ``target:path`` that
    ``git show`` cannot resolve, ``git checkout`` cannot restore either, so the
    rollback fails before anything is written.
    """
    file_path = _resolve_memory_path(file_path)
    shown = _run_git("show", f"{target}:{file_path}")
    if shown.returncode != 0 or not isinstance(shown.stdout, str):
        return []
    with open(os.path.join(config.memory_dir, file_path), encoding="utf-8") as fh:
        live = fh.read()
    target_markers = _retirement_markers(shown.stdout, file_path)
    live_markers = _retirement_markers(live, file_path)
    undone = [k for k in live_markers if k not in target_markers]
    if not undone:
        return []

    # Which commit in target..HEAD introduced each one: walk the file's
    # versions newest-first and take the first whose predecessor lacked it.
    log = _run_git("log", "--format=%h%x09%s", f"{target}..HEAD", "--", file_path)
    commits: list[tuple[str, str]] = []
    if log.returncode == 0 and isinstance(log.stdout, str):
        for line in log.stdout.splitlines():
            if "\t" in line:
                sha, subject = line.split("\t", 1)
                commits.append((sha, subject))
    versions: list[dict[str, dict[str, Any]]] = []
    for sha, _subject in commits:
        at = _run_git("show", f"{sha}:{file_path}")
        text = at.stdout if at.returncode == 0 and isinstance(at.stdout, str) else ""
        versions.append(_retirement_markers(text, file_path))
    versions.append(target_markers)  # the oldest commit's predecessor

    found: list[dict[str, Any]] = []
    for key in undone:
        retired_by: tuple[str, str] | None = None
        for i, (sha, subject) in enumerate(commits):
            if key in versions[i] and key not in versions[i + 1]:
                retired_by = (sha, subject)
                break
        found.append({
            "record": file_path,
            "relation": live_markers[key]["relation"],
            "superseded_by": live_markers[key]["superseded_by"],
            "commit": retired_by[0] if retired_by else None,
            "commit_subject": retired_by[1] if retired_by else None,
        })
    return found


def _render_retirements(retirements: list[dict[str, Any]]) -> str:
    lines = []
    for r in retirements:
        by = (
            f'retired by {r["commit"]} "{r["commit_subject"]}"'
            if r["commit"] else "retired in the uncommitted working tree"
        )
        lines.append(f'- {r["record"]} — {r["relation"]} — {by}')
    return "\n".join(lines)


_RETIREMENT_ALTERNATIVES = (
    "To bring a retired record back on purpose, use `palinode restore <file>` "
    "(or `palinode corrections undo` for a correction), which records that it "
    "was restored. To roll back anyway, pass --undo-retirements "
    "(undo_retirements=true)."
)


def rollback_report(
    file_path: str,
    commit: str | None = None,
    dry_run: bool = False,
    undo_retirements: bool = False,
) -> dict[str, Any]:
    """Revert a memory file to a previous version, lifecycle-aware.

    Creates a new commit that restores the file; the old version stays in git
    history. A rollback that would undo a retirement (see
    :func:`retirements_undone`) is named in the preview and **refused** unless
    ``undo_retirements`` acknowledges it; an acknowledged one names every
    record it resurrected and reports ``status: undid_retirements``, never
    plain success.

    Returns ``{"result": <text>, "status": ..., "retirements": [...],
    "resurrected": [...]}`` where ``status`` is one of ``not_found``,
    ``failed``, ``no_change``, ``preview``, ``refused``, ``rolled_back`` or
    ``undid_retirements``.
    """
    file_path = _resolve_memory_path(file_path)

    def _out(
        status: str,
        result: str,
        retirements: list[dict[str, Any]] | None = None,
        resurrected: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "result": result,
            "status": status,
            "retirements": retirements or [],
            "resurrected": resurrected or [],
        }

    if not os.path.exists(os.path.join(config.memory_dir, file_path)):
        return _out("not_found", f"File not found: {file_path}")

    target = commit or "HEAD~1"
    if target.startswith("-"):
        # The ref is handed to git positionally; it must never parse as an option.
        return _out("failed", f"Rollback failed: invalid commit {target!r}")

    retirements = retirements_undone(file_path, target)
    n = len(retirements)
    listing = _render_retirements(retirements)

    if dry_run:
        # Show what would change
        result = _run_git("diff", f"{target}..HEAD", "--", file_path)
        notice = ""
        if retirements:
            notice = (
                f"**This rollback would undo {n} retirement(s)** and bring the "
                f"record(s) back as current, unmarked:\n{listing}\n\n"
                f"{_RETIREMENT_ALTERNATIVES}\n\n"
            )
        if not result.stdout.strip():
            return _out(
                "no_change",
                f"{notice}No differences between {target} and HEAD for {file_path}",
                retirements,
            )
        lines = result.stdout.split("\n")
        preview = "\n".join(lines[:50])
        if len(lines) > 50:
            preview += f"\n... ({len(lines) - 50} more lines)"
        return _out(
            "preview",
            f"## Dry Run: Rollback {file_path} to {target}\n\n{notice}"
            f"```diff\n{preview}\n```",
            retirements,
        )

    if retirements and not undo_retirements:
        return _out(
            "refused",
            f"Refused: rolling back {file_path} to {target} would undo {n} "
            f"retirement(s) and bring the record(s) back as current:\n{listing}"
            f"\n\nNothing was written. {_RETIREMENT_ALTERNATIVES}",
            retirements,
        )

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
        return _out("failed", f"Rollback failed: {checkout.stderr}", retirements)

    # Commit the revert through the choke point (commit_memory_files) rather
    # than a raw add + commit — the fourth commit-message shape this module
    # used to carry alongside commit_memory_file/commit_memory_files/push().
    # Note: this now also respects config.git.auto_commit, same as every
    # other commit site in this module; the raw calls it replaces did not.
    message = f"palinode: rollback {file_path} to {target}"
    if retirements:
        message = f"{message} (undid {n} retirement(s))"
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

    if retirements:
        logger.warning(
            "rollback undid retirements op=rollback file_path=%s target=%s count=%d",
            file_path, target, n,
        )
        return _out(
            "undid_retirements",
            f"Rolled back {file_path} to {target} and UNDID {n} retirement(s); "
            f"resurrected as current:\n{listing}\n\nCommitted as: {message}",
            retirements,
            retirements,
        )
    return _out(
        "rolled_back",
        f"Rolled back {file_path} to {target}. Committed as: {message}",
    )


def rollback(
    file_path: str,
    commit: str | None = None,
    dry_run: bool = False,
    undo_retirements: bool = False,
) -> str:
    """Text form of :func:`rollback_report`: its ``result`` string."""
    return rollback_report(file_path, commit, dry_run, undo_retirements)["result"]


def push() -> str:
    """Push memory changes to the remote repository.

    Syncs the local data repo to GitHub for backup and cross-machine access.

    Returns:
        Push result or error message.
    """
    with _repository_commit_lock():
        # Check if there are unpushed commits
        status = _run_git("status", "--porcelain")
        dirty = status.stdout.strip()
        if dirty:
            # Auto-commit the dirty `.md` files first: an explicit file list,
            # never the repo-wide `*.md` / `**/*.md` sweep this module's own
            # docstring forbids elsewhere. The commit is limited to that list by
            # a pathspec, so it holds only those `.md` paths. Anything else that
            # was already staged (an operator's in-progress edit, any non-`.md`
            # file) is neither committed nor pushed; it stays staged.
            #
            # `git status --porcelain` prefixes each line with a two-character
            # status code. A rename entry reads "old -> new": only the
            # destination is on disk to add, but both sides go in the commit
            # pathspec so the rename lands whole rather than leaving the source's
            # deletion staged behind it. A staged deletion ("D ") is committed
            # without an add, since git cannot add a path that is in neither the
            # index nor the working tree. Quoted paths (spaces/unicode under
            # core.quotepath) are unquoted so the pathspec matches the real
            # filename. Lines come from the unstripped stdout: stripping it would
            # eat the leading space of the first line's " M" status code and
            # shift that path by one character.
            md_files: list[str] = []
            add_files: list[str] = []
            for line in status.stdout.split("\n"):
                if not line:
                    continue
                code, entry = line[:2], line[3:]
                source = None
                if " -> " in entry:
                    source, entry = entry.split(" -> ", 1)
                    source = source.strip('"')
                entry = entry.strip('"')
                if not entry.endswith(".md"):
                    continue
                md_files.append(entry)
                if code != "D ":
                    add_files.append(entry)
                if source and source.endswith(".md"):
                    md_files.append(source)

            if md_files:
                outcome = _try_commit_paths(
                    md_files,
                    f"palinode: auto-commit before push ({_utc_now().strftime('%Y-%m-%d %H:%M')})",
                    add_paths=add_files,
                )
                if not outcome.committed:
                    logger.warning("auto-commit before push failed: %s", outcome.error)

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
