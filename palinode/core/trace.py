"""Provenance composition — ``palinode trace <file>`` (#536, ADR-010).

The consumer that joins every provenance primitive Palinode already exposes into
one lineage view for a single memory file. It is *composition over plumbing*:
the net-new code here is the join + honest presentation over

- the parsed frontmatter axes (``epistemic`` / ``sources`` / ``claims`` /
  ``contradicts`` / ``backed_by``) — :mod:`palinode.core.parser`,
  :mod:`palinode.core.claims`, :mod:`palinode.core.typed_links`;
- git blame / history — :mod:`palinode.core.git_tools`;
- the supersession trail — the executor's ``<base>-history.md`` sibling plus the
  in-body ``[superseded]`` / ``[retracted]`` tombstones;
- the retrieval log — ``.audit/retrievals.jsonl``.

Each row carries an honest three-state ``status`` so the trail never overclaims:

``present``
    The capability has landed *and* this file carries data for it.
``none``
    The capability has landed but this file has nothing for it (no citations, no
    contradictions, never recalled) — an earned "nothing here", not a gap.
``not_captured``
    The provenance gap itself is not built yet, so the field renders an honest
    "not yet captured" placeholder. Today that is G2 (per-fact extraction
    metadata) and the G3 terminal consuming-action edge; as each gap lands, its
    row graduates from ``not_captured`` to ``present``/``none`` without a
    caller-visible shape change.

Rows name the gap by its stable public label (``G2`` / ``G3``) and never by a
tracker issue number: this text ships to users of the public package, where a
private issue number resolves to something else entirely.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import frontmatter

from palinode.core import git_tools
from palinode.core.claims import resolve_memory_claims
from palinode.core.parser import DEFAULT_EPISTEMIC, parse_sources
from palinode.core.typed_links import parse_link_refs

#: Row status vocabulary (see module docstring).
STATUS_PRESENT = "present"
STATUS_NONE = "none"
STATUS_NOT_CAPTURED = "not_captured"

#: In-body supersession/retraction tombstones the executor leaves behind.
_TOMBSTONE_RE = re.compile(r"\[(superseded|retracted)\b", re.IGNORECASE)

#: History-file entry line, e.g. ``- [2026-05-09 10:00] Superseded (…): …``.
_HISTORY_ENTRY_RE = re.compile(r"^-\s+\[[^\]]+\].*", re.MULTILINE)

_RETRIEVAL_LOG_REL = os.path.join(".audit", "retrievals.jsonl")


def _first_heading(body: str) -> str:
    """Return the text of the first ``# `` heading in *body*, or ``""``."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _fact_section(rel_path: str, metadata: dict[str, Any], body: str) -> dict[str, Any]:
    """The fact identity: title, path-relative id, epistemic marker, type, core."""
    fact_id = rel_path[:-3] if rel_path.endswith(".md") else rel_path
    title = str(metadata.get("title") or "").strip() or _first_heading(body)
    if not title:
        title = os.path.splitext(os.path.basename(rel_path))[0]
    return {
        "title": title,
        "id": fact_id,
        "epistemic": str(metadata.get("epistemic") or DEFAULT_EPISTEMIC),
        "type": metadata.get("type"),
        "core": bool(metadata.get("core", False)),
    }


def _source_section(rel_path: str, metadata: dict[str, Any], memory_dir: str) -> dict[str, Any]:
    """G1 (landed as ``sources:`` quote anchors + ``claims:``) — origin citations.

    Surfaces the document→span citations the file actually carries and resolves
    each ``claims:`` binding to its live integrity status, so ``trace`` shows not
    just *that* a source was cited but whether the cited span still hash-matches.
    """
    anchors = parse_sources(metadata)
    try:
        claims = resolve_memory_claims(rel_path, memory_dir)
    except (ValueError, FileNotFoundError):
        claims = []
    status = STATUS_PRESENT if (anchors or claims) else STATUS_NONE
    return {"status": status, "anchors": anchors, "claims": claims}


def _saved_and_changed(rel_path: str, metadata: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split the git log into the creation commit (``saved``) and later changes."""
    first = git_tools.first_commit(rel_path)
    recent = git_tools.history(rel_path, limit=20)

    origin_date = str(metadata.get("created_at") or "").strip() or None
    origin_source = str(metadata.get("source") or "").strip() or None

    if first:
        saved = {
            "status": STATUS_PRESENT,
            "commit": first["hash"],
            "date": first["date"],
            "author": first.get("author"),
            "message": first.get("message"),
            "origin_date": origin_date,
            "origin_source": origin_source,
        }
    else:
        saved = {
            "status": STATUS_NONE,
            "commit": None,
            "date": None,
            "author": None,
            "message": None,
            "origin_date": origin_date,
            "origin_source": origin_source,
        }

    first_hash = first["hash"] if first else None
    changed_commits = [c for c in recent if c.get("hash") != first_hash]
    changed = {
        "status": STATUS_PRESENT if changed_commits else STATUS_NONE,
        "commits": changed_commits,
    }
    return saved, changed


def _supersession_section(rel_path: str, body: str, memory_dir: str) -> dict[str, Any]:
    """The supersession trail: the ``<base>-history.md`` sibling + in-body tombstones.

    Palinode's supersession is intra-file (the executor strikes the loser through
    and appends it to a ``-history.md`` sibling), so the honest cross-file signal
    is that sibling plus the ``[superseded]`` / ``[retracted]`` markers left in
    the body — not a frontmatter ``supersedes:`` list (which does not exist).
    """
    base = re.sub(r"\.md$", "", rel_path)
    history_rel = f"{base}-history.md"
    history_abs = os.path.join(memory_dir, history_rel)

    entries: list[str] = []
    history_file: str | None = None
    if os.path.exists(history_abs):
        history_file = history_rel
        try:
            with open(history_abs, encoding="utf-8") as fh:
                hist_text = fh.read()
            entries = [m.group(0).strip() for m in _HISTORY_ENTRY_RE.finditer(hist_text)]
        except OSError:
            entries = []

    tombstones = len(_TOMBSTONE_RE.findall(body))
    status = STATUS_PRESENT if (history_file or tombstones) else STATUS_NONE
    return {
        "status": status,
        "history_file": history_file,
        "entries": entries,
        "in_file_tombstones": tombstones,
    }


def _typed_link_section(metadata: dict[str, Any], field: str) -> dict[str, Any]:
    """G4 (landed) — a typed relationship list (``contradicts`` / ``backed_by``)."""
    refs = parse_link_refs(metadata, field)
    return {"status": STATUS_PRESENT if refs else STATUS_NONE, "refs": refs}


def _canonical_ref(path: str, memory_dir: str) -> str:
    """Reduce any spelling of a memory path to one comparable identity.

    Retrieval events are written by several producers and are logged as given —
    absolute, or relative but not necessarily canonical (``./decisions/x.md``).
    Comparing raw strings silently undercounts recall, so both sides of the
    match go through here: absolute paths are made relative to ``memory_dir``,
    the result is ``normpath``-ed, and the ``.md`` suffix is dropped.
    """
    p = path
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, memory_dir)
        except ValueError:
            pass
    p = os.path.normpath(p)
    return p[:-3] if p.endswith(".md") else p


def _recall_events(rel_path: str, memory_dir: str) -> list[dict[str, Any]]:
    """Read the retrieval log and return every event referencing *rel_path*."""
    log_path = os.path.join(memory_dir, _RETRIEVAL_LOG_REL)
    if not os.path.exists(log_path):
        return []
    target = _canonical_ref(rel_path, memory_dir)
    matches: list[dict[str, Any]] = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fp = str(event.get("file_path") or "")
                if not fp:
                    continue
                if _canonical_ref(fp, memory_dir) == target:
                    matches.append(event)
    except OSError:
        return []
    return matches


def _recalled_section(rel_path: str, memory_dir: str) -> dict[str, Any]:
    """G3 (partial) — the retrieval log records *that* a fact was recalled.

    The consuming-action / terminal edge half of G3 (#535) is not built yet;
    :func:`_used_in_section` renders that as the honest placeholder.
    """
    events = _recall_events(rel_path, memory_dir)
    if not events:
        return {
            "status": STATUS_NONE,
            "count": 0,
            "sessions": [],
            "dates": [],
            "last": None,
        }
    sessions: list[str] = []
    dates: list[str] = []
    timestamps: list[str] = []
    for event in events:
        sid = event.get("session_id")
        if sid and sid not in sessions:
            sessions.append(str(sid))
        ts = str(event.get("timestamp") or "")
        if ts:
            timestamps.append(ts)
            day = ts[:10]
            if day not in dates:
                dates.append(day)
    return {
        "status": STATUS_PRESENT,
        "count": len(events),
        "sessions": sessions,
        "dates": dates,
        "last": max(timestamps) if timestamps else None,
    }


def _used_in_section() -> dict[str, Any]:
    """G3 terminal edge (open) — the consuming-action edge is not built yet.

    ``gap`` carries the stable public gap label rather than a tracker issue
    number, so the payload stays meaningful in the public package.
    """
    return {
        "status": STATUS_NOT_CAPTURED,
        "note": "consuming-action edge not yet captured",
        "gap": "G3",
    }


def _extracted_section() -> dict[str, Any]:
    """G2 per-fact extraction metadata (open) — not built yet."""
    return {
        "status": STATUS_NOT_CAPTURED,
        "note": "per-fact extraction metadata not yet captured",
        "gap": "G2",
    }


def compose_trace(file_path: str, memory_dir: str) -> dict[str, Any]:
    """Compose the full provenance lineage for one memory file.

    ``file_path`` is a path relative to ``memory_dir`` (already traversal-checked
    by the caller). Returns the structured lineage object — the same shape the
    CLI renders and the review UI consumes. Raises :class:`FileNotFoundError`
    when the path is not a readable regular file (a directory included — the
    caller maps that to the same clean 404 as a missing file, rather than
    letting ``IsADirectoryError`` escape as a 500).

    Malformed YAML frontmatter degrades rather than raising, matching
    ``parser.parse_markdown``'s soft-fail contract: the memory is traced with
    empty metadata and its full text as the body, so a broken-frontmatter file
    is still auditable.
    """
    full_path = file_path if os.path.isabs(file_path) else os.path.join(memory_dir, file_path)
    # isfile (not exists): a directory passes exists() and would then raise
    # IsADirectoryError out of open().
    if not os.path.isfile(full_path):
        raise FileNotFoundError(file_path)
    rel_path = os.path.relpath(os.path.realpath(full_path), os.path.realpath(memory_dir))

    with open(full_path, encoding="utf-8") as fh:
        raw = fh.read()
    # One guarded parse yields both halves: the metadata the axes read and the
    # raw body the heading/tombstone scans need (parse_markdown returns chunked
    # sections, not the whole body). Soft-fail mirrors parse_markdown exactly.
    try:
        post = frontmatter.loads(raw)
        metadata: dict[str, Any] = post.metadata
        body: str = post.content
    except Exception:
        metadata, body = {}, raw

    saved, changed = _saved_and_changed(rel_path, metadata)
    return {
        "file": rel_path,
        "fact": _fact_section(rel_path, metadata, body),
        "source": _source_section(rel_path, metadata, memory_dir),
        "extracted": _extracted_section(),
        "saved": saved,
        "changed": changed,
        "supersession": _supersession_section(rel_path, body, memory_dir),
        "contradicts": _typed_link_section(metadata, "contradicts"),
        "backed_by": _typed_link_section(metadata, "backed_by"),
        "recalled": _recalled_section(rel_path, memory_dir),
        "used_in": _used_in_section(),
    }


# ── Text rendering (shared by CLI + MCP) ──────────────────────────────────────

_PLACEHOLDER = "— not yet captured"


def _fmt_source(source: dict[str, Any]) -> list[str]:
    if source["status"] == STATUS_NONE:
        return ["Source:       — none recorded"]
    lines: list[str] = []
    claims = source.get("claims") or []
    anchors = source.get("anchors") or []
    if claims:
        for c in claims:
            span = c.get("span", {})
            lines.append(
                f"Source:       {c.get('source_id', '?')} :: "
                f"\"{span.get('quote', '')}\"  [{c.get('span_status', '?')}]"
            )
    if anchors:
        for a in anchors:
            lines.append(f"Source:       {a.get('ref', '?')} :: \"{a.get('quote', '')}\"")
    lines.append(f"              ({len(anchors)} anchor(s), {len(claims)} claim(s))")
    return lines


def _fmt_saved(saved: dict[str, Any]) -> str:
    if saved["status"] == STATUS_NONE or not saved.get("commit"):
        return "Saved:        — not under version control"
    line = f"Saved:        {saved['commit']}  {str(saved.get('date') or '')[:10]}"
    origin = saved.get("origin_date")
    if origin and origin != str(saved.get("date") or "")[:10]:
        line += f"  (origin: {origin}"
        if saved.get("origin_source"):
            line += f" via {saved['origin_source']}"
        line += ")"
    return line


def _fmt_changed(changed: dict[str, Any]) -> str:
    commits = changed.get("commits") or []
    if not commits:
        return "Changed:      — no changes since creation"
    head = commits[0]
    line = f"Changed:      {head['hash']}  {str(head.get('date') or '')[:10]}  {head.get('message', '')}"
    if len(commits) > 1:
        line += f"  (+{len(commits) - 1} more)"
    return line


def _fmt_supersession(sup: dict[str, Any]) -> str:
    if sup["status"] == STATUS_NONE:
        return "Supersedes:   —"
    parts: list[str] = []
    if sup.get("history_file"):
        parts.append(sup["history_file"])
    detail = f"{len(sup.get('entries') or [])} archived, {sup.get('in_file_tombstones', 0)} in-file tombstone(s)"
    label = parts[0] if parts else ""
    return f"Supersedes:   {label}  ({detail})".rstrip()


def _fmt_recalled(recalled: dict[str, Any]) -> str:
    if recalled["status"] == STATUS_NONE:
        return "Recalled:     — never recalled"
    line = f"Recalled:     {recalled['count']}×"
    if recalled.get("sessions"):
        line += f" · sessions: {', '.join(recalled['sessions'])}"
    elif recalled.get("dates"):
        line += f" · dates: {', '.join(recalled['dates'])}"
    if recalled.get("last"):
        line += f" · last {str(recalled['last'])[:10]}"
    return line


def format_trace_text(trace: dict[str, Any]) -> str:
    """Render a composed trace as human-readable text.

    Shared by the CLI and MCP surfaces. The output carries ``[status]`` and
    ``[fact:id]`` brackets, so terminal callers must print it with Rich markup
    disabled (``console.print(..., markup=False)``) — mirroring the blame/claims
    render (#262).
    """
    fact = trace["fact"]
    lines = [f"## Trace: {trace['file']}", ""]
    lines.append(
        f"Fact:         {fact['title']}  [fact:{fact['id']}]  (epistemic: {fact['epistemic']})"
    )
    lines.extend(_fmt_source(trace["source"]))
    lines.append(f"Extracted:    {_PLACEHOLDER} ({trace['extracted']['gap']})")
    lines.append(_fmt_saved(trace["saved"]))
    lines.append(_fmt_changed(trace["changed"]))
    lines.append(_fmt_supersession(trace["supersession"]))

    contradicts = trace["contradicts"]["refs"]
    lines.append(f"Contradicts:  {', '.join(contradicts) if contradicts else '—'}")
    backed_by = trace["backed_by"]["refs"]
    lines.append(f"Backed by:    {', '.join(backed_by) if backed_by else '—'}")

    lines.append(_fmt_recalled(trace["recalled"]))
    lines.append(f"Used in:      {_PLACEHOLDER} ({trace['used_in']['gap']})")
    return "\n".join(lines)
