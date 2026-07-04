"""palinode worktree-reconcile — reclaim stale dead-PID-locked git worktrees (#448).

Claude Code (and similar agents) create isolated worktrees under
``.claude/worktrees/`` and mark them ``locked`` with the owning session's PID.
When a session crashes the lock outlives the process, so the worktree is never
reclaimed — it bloats the repo on disk and pollutes file-watch / index globs
(Palinode's own watcher sees ghost branches).

This reclaims them SAFELY. A locked worktree is removed only when ALL hold:
  * its lock PID is dead (``os.kill(pid, 0)`` raises ``ProcessLookupError``),
  * its working tree is clean (``git status --porcelain`` empty), and
  * its branch has an upstream (so nothing unpushed is lost).
``git worktree remove`` drops only the working directory — the branch and its
commits are preserved. Anything alive, dirty, or lacking an upstream is skipped
with a stated reason for human review.

Dry-run by default; pass ``--execute`` to actually unlock + remove + prune.
"""
from __future__ import annotations

import json as _json
import os
import re
import subprocess  # nosec B404 - argv-form git calls, no shell
from dataclasses import asdict, dataclass
from pathlib import Path

import click

# A PID in a Claude Code lock reason. 2–7 digits avoids matching a stray single
# digit or an over-long id that isn't a PID.
_PID_RE = re.compile(r"\b(\d{2,7})\b")


def _git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603,B607 - argv form, no shell
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )


@dataclass
class WorktreeVerdict:
    path: str
    branch: str | None
    pid: int | None
    pid_alive: bool | None
    clean: bool | None
    has_upstream: bool | None
    action: str  # "remove" | "skip"
    reason: str


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists.

    A ``PermissionError`` means the process exists but is owned by another user;
    any other OS error means we can't tell — both are treated as alive so we
    never remove a worktree whose owner might still be running.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _parse_porcelain(text: str) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into per-worktree dicts."""
    entries: list[dict] = []
    cur: dict = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):]}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):]
        elif line == "locked" or line.startswith("locked "):
            cur["locked"] = True
            cur["lock_reason"] = line[len("locked "):].strip() if line != "locked" else ""
    if cur:
        entries.append(cur)
    return entries


def _lock_reason(repo_root: str, wt_path: str, porcelain_reason: str) -> str:
    """Prefer the porcelain reason; fall back to the on-disk lock file."""
    if porcelain_reason:
        return porcelain_reason
    name = Path(wt_path).name
    try:
        return (
            Path(repo_root) / ".git" / "worktrees" / name / "locked"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _under_claude_worktrees(repo_root: str, wt_path: str) -> bool:
    marker = str(Path(repo_root) / ".claude" / "worktrees") + os.sep
    return (str(Path(wt_path)) + os.sep).startswith(marker)


def reconcile(repo_root: str) -> list[WorktreeVerdict]:
    """Compute the reconcile verdict for every locked worktree under
    ``.claude/worktrees/`` — pure inspection, no mutation."""
    porcelain = _git(["worktree", "list", "--porcelain"], cwd=repo_root)
    verdicts: list[WorktreeVerdict] = []
    for wt in _parse_porcelain(porcelain.stdout):
        path = wt.get("path", "")
        if not wt.get("locked") or not _under_claude_worktrees(repo_root, path):
            continue
        branch = wt.get("branch")
        reason = _lock_reason(repo_root, path, wt.get("lock_reason", ""))
        m = _PID_RE.search(reason)
        pid = int(m.group(1)) if m else None

        def skip(why: str, **kw) -> WorktreeVerdict:
            return WorktreeVerdict(
                path=path, branch=branch, pid=pid, action="skip", reason=why,
                pid_alive=kw.get("pid_alive"), clean=kw.get("clean"),
                has_upstream=kw.get("has_upstream"),
            )

        if pid is None:
            verdicts.append(skip("no PID in lock reason — manual review"))
            continue
        alive = _pid_alive(pid)
        if alive:
            verdicts.append(skip(f"lock owner pid {pid} still alive", pid_alive=True))
            continue
        clean = _git(["status", "--porcelain"], cwd=path).stdout.strip() == ""
        if not clean:
            verdicts.append(skip("working tree dirty — uncommitted changes", pid_alive=False, clean=False))
            continue
        upstream = _git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=path
        )
        has_upstream = upstream.returncode == 0
        if not has_upstream:
            verdicts.append(skip("branch has no upstream — unpushed work", pid_alive=False, clean=True, has_upstream=False))
            continue
        verdicts.append(WorktreeVerdict(
            path=path, branch=branch, pid=pid, pid_alive=False, clean=True,
            has_upstream=True, action="remove",
            reason=f"dead lock pid {pid}, clean, upstream present",
        ))
    return verdicts


def _apply(repo_root: str, verdicts: list[WorktreeVerdict]) -> list[str]:
    """Unlock + force-remove every ``remove`` verdict, then prune. Returns the
    paths actually removed."""
    removed: list[str] = []
    for v in verdicts:
        if v.action != "remove":
            continue
        _git(["worktree", "unlock", v.path], cwd=repo_root)
        res = _git(["worktree", "remove", "--force", v.path], cwd=repo_root)
        if res.returncode == 0:
            removed.append(v.path)
    if removed:
        _git(["worktree", "prune", "-v"], cwd=repo_root)
    return removed


@click.command("worktree-reconcile")
@click.option("--execute", is_flag=True, default=False,
              help="Actually unlock + remove stale worktrees (default: dry-run).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the verdicts as JSON.")
def worktree_reconcile(execute: bool, as_json: bool) -> None:
    """Reclaim stale dead-PID-locked git worktrees under .claude/worktrees/.

    SAFE: only removes a worktree whose lock PID is dead, whose tree is clean,
    and whose branch has an upstream — the branch and commits are preserved.
    Dry-run by default.
    """
    top = _git(["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        raise click.ClickException("not inside a git repository")
    repo_root = top.stdout.strip()

    verdicts = reconcile(repo_root)
    to_remove = [v for v in verdicts if v.action == "remove"]

    removed: list[str] = []
    if execute and to_remove:
        removed = _apply(repo_root, to_remove)

    if as_json:
        click.echo(_json.dumps({
            "dry_run": not execute,
            "verdicts": [asdict(v) for v in verdicts],
            "removed": removed,
        }, indent=2))
        return

    if not verdicts:
        click.echo("No locked worktrees under .claude/worktrees/ to reconcile.")
        return
    for v in verdicts:
        if v.action == "remove":
            verb = "REMOVED" if v.path in removed else ("WOULD REMOVE" if not execute else "REMOVE FAILED")
            click.echo(f"  [{verb}] {v.path}  ({v.reason})")
        else:
            click.echo(f"  [skip] {v.path}  ({v.reason})")
    n = len(to_remove)
    if not execute and n:
        click.echo(f"\n{n} stale worktree(s) would be removed. Re-run with --execute.")
    elif execute:
        click.echo(f"\nRemoved {len(removed)} of {n} stale worktree(s).")
