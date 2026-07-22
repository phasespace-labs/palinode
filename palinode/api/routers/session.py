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

#: How much of the session summary the project-status one-liner keeps.
#: The status file is a longitudinal index — one dated line per session — so the
#: line has to stay scannable. The previous 200 was a bare magic number with no
#: measured constraint behind it and routinely cut a real /wrap summary in half.
#: 400 is the repo's existing answer to "how much text is enough to recognise a
#: memory at a glance" (``config.search.snippet_max_chars``); reusing it keeps
#: one number rather than two. The full text is never lost: it lives verbatim in
#: the daily note and in the indexed session-end file this line points at.
STATUS_SUMMARY_MAX_CHARS = 400


def _truncate_marked(text: str, limit: int) -> str:
    """Trim ``text`` to at most ``limit`` chars on a word boundary, marking the
    cut with ``"..."`` (the convention already used by ``palinode/cli/search.py``).

    Returns ``text`` unchanged when it already fits. A *silent* mid-word cut is
    what made the status file read as corrupted (#681): a reader could not tell
    "the summary ended there" from "the summary was cut".
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    if text[limit] != " ":
        # Fall back to the hard cut for a single unbroken token longer than the
        # limit — there is no word boundary to retreat to.
        head = head.rsplit(" ", 1)[0] or head
    return head.rstrip() + "..."


def _count_phrase(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _status_line(
    today: str,
    summary: str,
    decisions: list[str] | None,
    blockers: list[str] | None,
    pointer: str,
) -> str:
    """Render the one dated line a session-end appends to ``projects/<p>-status.md``.

    The status file is deliberately a longitudinal *index*, one line per session
    — so this does not inline decision/blocker prose, which is already stored
    verbatim in two other places (the daily note and the indexed session-end
    file). It records the **count** and a **pointer** to the durable copy, so a
    reader can see that the material exists and where to read it. Dropping the
    arrays with no trace at all — the pre-#681 behaviour — is what made the file
    look like session-end had corrupted its own input.
    """
    one_liner = _truncate_marked(" ".join(summary.split()), STATUS_SUMMARY_MAX_CHARS)
    counts = []
    if decisions:
        counts.append(_count_phrase(len(decisions), "decision"))
    if blockers:
        counts.append(_count_phrase(len(blockers), "blocker"))

    parts = [f"- [{today}]"]
    if one_liner:
        parts.append(one_liner)
    if counts:
        parts.append(f"({', '.join(counts)} → {pointer})")
    return " ".join(parts)


# ── Envelope-markup guard ────────────────────────────────────────────────────
#
# A session-end string parameter should never carry a *tool envelope*. Two
# entry points put one there:
#   1. a malformed model tool-call, whose tail is absorbed into the preceding
#      string parameter — which also swallows the arrays that followed it;
#   2. the SessionEnd hook's transcript extraction, which used to lift Claude
#      Code harness markup straight out of the first user turn (fixed at source
#      in examples/hooks/palinode-session-end.sh; this is the backstop).
#
# Detection deliberately is NOT a substring blacklist. Palinode is a memory
# system for developers, and a note *about* tool-call syntax is legitimate
# content — the investigation that produced this guard has to stay saveable. A
# fragment from the vocabulary below is only a *candidate*; rejection needs one
# of three corroborating signals, and anything inside a code fence or code span
# is exempt outright.

#: Tag names whose appearance in a session-end string is a candidate envelope
#: leak. Three groups: model tool-call syntax, this tool's own parameter names
#: (what mechanism-1 absorption leaves behind), and Claude Code harness markup.
#: `project` is deliberately absent — `<project>` is the root element of real
#: build files people write notes about, and it is not an absorption target for
#: `summary`.
_ENVELOPE_TAGS: tuple[str, ...] = (
    "invoke", "parameter", "function_calls", "tool_call", "tool_use",
    "summary", "decisions", "blockers",
    "system-reminder", "command-message", "command-name", "command-args",
    "local-command-stdout", "local-command-stderr", "user-prompt-submit-hook",
    "bash-input", "bash-stdout", "bash-stderr",
    "ide_selection", "ide_opened_file",
)

#: Matches `<tag>`, `</tag>`, `<tag …attrs>` and namespaced forms (`<invoke>`).
#: Group 1 = "/" for a closing tag, group 2 = the bare tag name.
_ENVELOPE_RE = re.compile(
    r"<(/?)(?:[A-Za-z][\w.-]*:)?(" + "|".join(_ENVELOPE_TAGS) + r")(?:\s[^<>]*)?/?>",
    re.IGNORECASE,
)

#: Fenced blocks and inline spans are the escape hatch: markup quoted as code is
#: always content. Replaced with a space so surrounding offsets stay meaningful.
_CODE_SPANS = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"~~~.*?~~~", re.DOTALL),
    re.compile(r"`[^`\n]+`"),
)


def _strip_code(text: str) -> str:
    for pattern in _CODE_SPANS:
        text = pattern.sub(" ", text)
    return text


def _envelope_complaint(text: str, field: str, *, arrays_present: bool) -> str | None:
    """Return an actionable rejection message when ``text`` carries a tool
    envelope rather than content, else ``None`` (#682).

    ``arrays_present`` reports whether *this request* delivered any
    ``decisions``/``blockers``; absent arrays alongside envelope markup is the
    mechanism-1 signature and the highest-signal discriminator available.
    """
    scrubbed = _strip_code(text)
    matches = list(_ENVELOPE_RE.finditer(scrubbed))
    if not matches:
        return None

    openers = {m.group(2).lower() for m in matches if not m.group(1)}
    unmatched = next(
        (m for m in matches if m.group(1) and m.group(2).lower() not in openers), None
    )
    # Absorption lands the envelope at the very tail of the value.
    tail_end = len(scrubbed.rstrip())
    trailing = next((m for m in matches if m.end() == tail_end), None)

    if not arrays_present:
        offender, why = (unmatched or trailing or matches[-1]), (
            "and no `decisions`/`blockers` arrays arrived with it — the signature "
            "of a tool envelope absorbed into the string parameter"
        )
    elif unmatched is not None:
        offender, why = unmatched, "as a closing tag with no matching opener"
    elif trailing is not None:
        offender, why = trailing, "at the very end of the value, where an absorbed envelope lands"
    else:
        return None

    return (
        f"Refusing to store `{field}`: it contains tool-envelope markup "
        f"{why} — {offender.group(0)!r}. Palinode fails loud here rather than "
        "indexing an envelope as if it were memory. Re-send with "
        "`decisions`/`blockers` as real JSON arrays. If the markup really is part "
        "of the note, put it in a fenced code block or backticks and it will pass."
    )


def _first_envelope_complaint(req: SessionEndRequest) -> str | None:
    """First envelope complaint across ``summary`` and every array entry."""
    arrays_present = bool(req.decisions or req.blockers)
    complaint = _envelope_complaint(req.summary, "summary", arrays_present=arrays_present)
    if complaint:
        return complaint
    for label, items in (("decisions", req.decisions), ("blockers", req.blockers)):
        for i, item in enumerate(items or []):
            complaint = _envelope_complaint(item, f"{label}[{i}]", arrays_present=True)
            if complaint:
                return complaint
    return None


def _status_pointer(individual_file: str | None, daily_rel: str) -> str:
    """Memory-relative path of the durable home for this session's full entry.

    Prefers the indexed session-end file: it is permanent and searchable. The
    daily note is the fallback, and only a fallback, because consolidation later
    moves ``daily/*.md`` into ``archive/<year>/`` — a pointer at it goes stale.
    """
    if individual_file:
        try:
            return os.path.relpath(individual_file, _memory_base_dir())
        except ValueError:  # different drive (Windows) — keep what we have
            return individual_file
    return daily_rel


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
    # Fail loud BEFORE any write: an envelope stored as memory is silent
    # corruption in a system whose whole claim is audit-grade recall. Safe to
    # 400 even though session-end is the last call of a session — the SessionEnd
    # hook's curl uses `-f`, so HTTP >=400 routes the payload to
    # .claude/session-floor-fallback.jsonl for replay rather than the void, and
    # an interactive MCP/CLI caller is still live and can re-send corrected.
    complaint = _first_envelope_complaint(req)
    if complaint:
        logger.warning("session-end rejected: %s", complaint)
        raise HTTPException(status_code=400, detail=complaint)

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

    # Append status to project file if specified (or auto-derived from cwd).
    # Deliberately runs *after* the individual-file save so the one-liner can
    # point at the durable indexed record rather than only at the daily note.
    # Safe to sequence here: `_check_session_end_dedup` swallows all its own
    # failures and the individual save is wrapped, so neither can skip this.
    status_file = None
    if project:
        status_path = os.path.join(_memory_base_dir(), "projects", f"{project}-status.md")
        if os.path.exists(status_path):
            line = _status_line(
                today,
                req.summary,
                req.decisions,
                req.blockers,
                _status_pointer(individual_file, f"daily/{today}.md"),
            )
            with open(status_path, "a") as f:
                f.write(f"\n{line}\n")
            status_file = f"projects/{project}-status.md"
            logger.info(
                "session_end status append: file=%s decisions=%d blockers=%d",
                status_file, len(req.decisions or []), len(req.blockers or []),
            )

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
