"""Session-end and prompts routes (#314 Stage 3).

Extracted from palinode/api/server.py and palinode/api/routers/memory.py:
  POST /session-end    (from memory.py)
  GET  /prompts        (from server.py)
  GET  /prompts/{name} (from server.py)
  POST /prompts/{name}/activate  (from server.py)
"""
from __future__ import annotations

import glob
import hashlib
import logging
import os
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from palinode.core import git_tools
from palinode.core.config import config

from palinode.api._util import _project_from_cwd, _utc_now
from palinode.api.path_safety import _memory_base_dir
from palinode.api.memory_write import _resolve_source
from palinode.api.search_helpers import _check_session_end_dedup
from palinode.api.routers.memory import SaveRequest, save_api

logger = logging.getLogger("palinode.api")
router = APIRouter()


# ── Session-end ──────────────────────────────────────────────────────────────

class SessionEndRequest(BaseModel):
    summary: str
    decisions: list[str] | None = None
    blockers: list[str] | None = None
    project: str | None = None
    source: str | None = None
    # Push the memory repo after committing the note. None → fall back to
    # config.git.auto_push (legacy behavior). True → push regardless, so the wrap
    # flow ships the note in one call instead of needing a second palinode_push.
    # False → never push, even if auto_push is on. Because `git push` is
    # repo-wide, push=True also ships any earlier same-session /save commits,
    # which supersedes the old pre-push-first dance.
    push: bool | None = None
    # Structured metadata. All optional; existing callers keep working.
    harness: str | None = None  # e.g. "claude-code", "claude-desktop", "cowork", "openclaw", "cursor", "zed", "vscode", "cli", "api", "hook", "other"
    cwd: str | None = None  # fully-qualified path the session ran in
    model: str | None = None  # e.g. "claude-opus-4-7"
    trigger: str | None = None  # e.g. "manual", "wrap-slash", "ps-slash", "session-end-hook", "clear-fallback-hook", "sigterm", "exit", "other"
    session_id: str | None = None  # opaque from harness if available
    duration_seconds: int | None = None


@router.post("/session-end")
def session_end_api(req: SessionEndRequest, request: Request = None) -> dict[str, Any]:
    """Capture session outcomes to daily notes and project status files."""
    today = _utc_now().strftime("%Y-%m-%d")
    now_iso = _utc_now().isoformat().replace("+00:00", "Z")
    # ADR-010: same precedence as save_api — explicit > header > env > default.
    source = _resolve_source(req.source, request)

    # Auto-derive project from cwd if caller didn't pass one.
    project = req.project or _project_from_cwd(req.cwd)

    # Build session entry
    parts = [f"## Session End — {now_iso}\n"]
    parts.append(f"**Source:** {source}\n")
    parts.append(f"**Summary:** {req.summary}\n")
    if req.decisions:
        parts.append("**Decisions:**")
        for d in req.decisions:
            parts.append(f"- {d}")
        parts.append("")
    if req.blockers:
        parts.append("**Blockers/Next:**")
        for b in req.blockers:
            parts.append(f"- {b}")
        parts.append("")

    # Structured metadata footer. Only emit lines that are populated so
    # the daily note stays uncluttered for callers that don't supply metadata.
    meta_lines: list[str] = []
    if req.harness:
        meta_lines.append(f"**Harness:** {req.harness}")
    if req.cwd:
        meta_lines.append(f"**CWD:** {req.cwd}")
    if req.model:
        meta_lines.append(f"**Model:** {req.model}")
    if req.trigger:
        meta_lines.append(f"**Trigger:** {req.trigger}")
    if req.session_id:
        meta_lines.append(f"**Session ID:** {req.session_id}")
    if req.duration_seconds is not None:
        meta_lines.append(f"**Duration:** {req.duration_seconds}s")
    if meta_lines:
        parts.extend(meta_lines)
        parts.append("")

    session_entry = "\n".join(parts)

    # Write to daily notes
    daily_dir = os.path.join(_memory_base_dir(), "daily")
    os.makedirs(daily_dir, exist_ok=True)
    daily_path = os.path.join(daily_dir, f"{today}.md")
    with open(daily_path, "a") as f:
        f.write(f"\n{session_entry}\n")

    # Append status to project file if specified (or auto-derived from cwd).
    status_file = None
    if project:
        status_path = os.path.join(_memory_base_dir(), "projects", f"{project}-status.md")
        if os.path.exists(status_path):
            one_liner = req.summary.replace("\n", " ").strip()[:200]
            with open(status_path, "a") as f:
                f.write(f"\n- [{today}] {one_liner}\n")
            status_file = f"projects/{project}-status.md"

    # Semantic dedup against recent saves. The daily note + project
    # status file are append-only logs we always write — only the indexed
    # individual file is suppressed when a near-duplicate already exists,
    # because that file's value is the standalone embedding/searchable record
    # which we'd otherwise have twice for the same content.
    deduplicated_against, dedup_similarity = _check_session_end_dedup(session_entry)

    # Also save as an individual indexed memory file (M0: dual-write).
    # This gives each session-end its own frontmatter, entities, description,
    # and embedding — searchable and retractable independently.
    individual_file = None
    if deduplicated_against is not None:
        logger.info(
            f"session_end dedup: matched {deduplicated_against} (sim={dedup_similarity:.2f}) "
            f"— skipping individual file"
        )
    else:
        try:
            short_hash = hashlib.sha256(req.summary.encode()).hexdigest()[:8]
            # Pass structured metadata through to the indexed file's frontmatter so
            # it's queryable later. Only include fields the caller set.
            extra_meta: dict[str, Any] = {}
            if req.harness:
                extra_meta["harness"] = req.harness
            if req.cwd:
                extra_meta["cwd"] = req.cwd
            if req.model:
                extra_meta["model"] = req.model
            if req.trigger:
                extra_meta["trigger"] = req.trigger
            if req.session_id:
                extra_meta["session_id"] = req.session_id
            if req.duration_seconds is not None:
                extra_meta["duration_seconds"] = req.duration_seconds
            save_req = SaveRequest(
                content=session_entry,
                type="ProjectSnapshot" if project else "Insight",
                slug=f"session-end-{today}-{project}-{short_hash}" if project else f"session-end-{today}-{short_hash}",
                entities=[f"project/{project}"] if project else [],
                source=source,
                metadata=extra_meta or None,
            )
            save_result = save_api(save_req)
            individual_file = save_result.get("file_path")
        except Exception as e:
            logger.error(f"Individual session-end file save failed (non-fatal): {e}")

    # Git commit (covers daily + status). One session-end is one logical event,
    # so daily + status stay in a single commit — but it stages an explicit file
    # list via the git_tools choke point, never a repo-wide sweep.
    committed = False
    if config.git.auto_commit:
        files_to_add = [daily_path]
        if status_file:
            files_to_add.append(os.path.join(_memory_base_dir(), status_file))
        commit_msg = f"{config.git.commit_prefix} session-end: {today}"
        committed = git_tools.commit_memory_files(files_to_add, commit_msg)

    # Push. An explicit req.push overrides config.git.auto_push so the
    # wrap flow ships the just-committed note without a second palinode_push;
    # push=None preserves the legacy auto_push default. git_tools.push() targets
    # origin/main and is repo-wide, so this also ships any earlier same-session
    # /save commits. pushed reflects whether the push actually succeeded so the
    # caller can report "saved + pushed" vs "saved (push pending)" honestly.
    should_push = req.push if req.push is not None else config.git.auto_push
    pushed = False
    if should_push:
        try:
            push_result = git_tools.push()
            pushed = not push_result.lower().startswith("push failed")
            if not pushed:
                logger.warning(f"session-end push did not succeed: {push_result}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Git push failed for session-end: {e}")

    response: dict[str, Any] = {
        "daily_file": f"daily/{today}.md",
        "status_file": status_file,
        "individual_file": individual_file,
        "entry": session_entry,
        "committed": committed,
        "pushed": pushed,
    }
    if deduplicated_against is not None:
        response["deduplicated_against"] = deduplicated_against
    return response


# ── Prompts ──────────────────────────────────────────────────────────────────

PROMPT_TASKS = {"compaction", "extraction", "update", "classification"}


def _prompts_dir() -> str:
    return os.path.join(_memory_base_dir(), "prompts")


def _read_prompt_file(file_path: str) -> dict[str, Any]:
    """Read a prompt file and return its metadata + content."""
    from palinode.core import parser
    with open(file_path, "r") as f:
        raw = f.read()
    metadata, sections = parser.parse_markdown(raw)
    # Reconstruct body from sections
    body = "\n\n".join(s["content"] for s in sections if s.get("content"))
    name = os.path.basename(file_path).replace(".md", "")
    return {
        "name": name,
        "file": os.path.relpath(file_path, _memory_base_dir()),
        "model": metadata.get("model", ""),
        "task": metadata.get("task", ""),
        "version": metadata.get("version", ""),
        "active": bool(metadata.get("active", False)),
        "content": body.strip(),
        "size_bytes": os.path.getsize(file_path),
    }


@router.get("/prompts")
def list_prompts_api(task: str | None = None) -> list[dict[str, Any]]:
    """List all prompt files, optionally filtered by task."""
    prompts_dir = _prompts_dir()
    if not os.path.exists(prompts_dir):
        return []

    results = []
    for filepath in glob.glob(os.path.join(prompts_dir, "*.md")):
        try:
            if os.path.commonpath([_memory_base_dir(), os.path.realpath(filepath)]) != _memory_base_dir():
                continue
            info = _read_prompt_file(filepath)
            if task and info["task"] != task:
                continue
            results.append(info)
        except Exception:
            pass

    results.sort(key=lambda x: (x["task"], x["name"]))
    return results


@router.get("/prompts/{name}")
def get_prompt_api(name: str) -> dict[str, Any]:
    """Read a specific prompt by name."""
    prompts_dir = _prompts_dir()
    candidates = [
        os.path.join(prompts_dir, name),
        os.path.join(prompts_dir, f"{name}.md"),
    ]
    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        try:
            within = os.path.commonpath([_memory_base_dir(), resolved]) == _memory_base_dir()
        except ValueError:
            continue
        if within and os.path.exists(resolved):
            return _read_prompt_file(resolved)

    raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")


@router.post("/prompts/{name}/activate")
def activate_prompt_api(name: str) -> dict[str, Any]:
    """Set active=true on this prompt and active=false on all others with the same task."""
    prompts_dir = _prompts_dir()
    if not os.path.exists(prompts_dir):
        raise HTTPException(status_code=404, detail="No prompts directory found")

    # Resolve target file
    candidates = [
        os.path.join(prompts_dir, name),
        os.path.join(prompts_dir, f"{name}.md"),
    ]
    target_path = None
    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        try:
            within = os.path.commonpath([_memory_base_dir(), resolved]) == _memory_base_dir()
        except ValueError:
            continue
        if within and os.path.exists(resolved):
            target_path = resolved
            break

    if not target_path:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")

    target_info = _read_prompt_file(target_path)
    task = target_info["task"]

    changed_files: list[str] = []

    def _set_active(file_path: str, active: bool) -> None:
        with open(file_path, "r") as f:
            text = f.read()
        # Replace active: field in frontmatter
        new_text = re.sub(
            r'^(active:\s*).*$',
            f'active: {"true" if active else "false"}',
            text,
            flags=re.MULTILINE,
        )
        if new_text == text:
            # Field missing — inject before closing ---
            pattern = re.compile(r'^(---\n.*?\n)(---\n)', re.DOTALL)
            m = pattern.match(text)
            if m:
                new_text = m.group(1) + f'active: {"true" if active else "false"}\n' + m.group(2) + text[m.end():]
        git_tools.write_memory_file(file_path, new_text)
        changed_files.append(file_path)

    # Deactivate all prompts of the same task
    for filepath in glob.glob(os.path.join(prompts_dir, "*.md")):
        try:
            resolved = os.path.realpath(filepath)
            within = os.path.commonpath([_memory_base_dir(), resolved]) == _memory_base_dir()
            if not within:
                continue
            info = _read_prompt_file(resolved)
            if info["task"] == task and resolved != target_path:
                _set_active(resolved, False)
        except Exception:
            pass

    # Activate target
    _set_active(target_path, True)

    # One activation toggle = a per-file commit for each prompt actually changed
    # stage only the prompts this toggle rewrote, never a repo-wide
    # `git add prompts/*.md` sweep.
    if config.git.auto_commit:
        for fp in changed_files:
            git_tools.commit_memory_file(
                fp,
                f"palinode: activate prompt {name} for task={task} [{os.path.basename(fp)}]",
            )

    return {"activated": name, "task": task}
