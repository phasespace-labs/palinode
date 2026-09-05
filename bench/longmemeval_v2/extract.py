"""The notes pool: LLM-proposed trajectory notes, written through ``save_memory``.

Insert-time only — LAFS scores query latency, and the point of the row is that
the LLM spends its effort at write time so the read path stays LLM-free. The
LLM proposes (``specs/prompts/trajectory-extraction.md``), the adapter
validates and writes each note as a real Palinode ``Insight`` file (one chunk
each, entity-tagged to its trajectory). Endpoint via ``LME_EXTRACT_*`` (the V1
convention: ``bench.longmemeval.llm.Endpoint.from_env("EXTRACT")``).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from bench.longmemeval import llm

NOTE_KINDS = ("fact", "transition", "procedure", "gotcha")
MAX_NOTES = 25
PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "specs", "prompts", "trajectory-extraction.md")

# a11y-tree lines worth showing the extractor: interactive and structural roles
# plus visible text. Everything else (generic containers, live regions) is noise.
_KEEP_ROLE_RE = re.compile(
    r"\b(RootWebArea|button|link|textbox|searchbox|combobox|listbox|option|menuitem|menu|tab|checkbox|radio|"
    r"switch|slider|spinbutton|heading|cell|columnheader|rowheader|row|StaticText|img|dialog|"
    r"alert|status|banner|navigation|main|table|list|listitem|treeitem|group|form|article|region)\b"
)
_NAME_RE = re.compile(r"'([^']*)'")


def digest_tree(tree: str, *, max_chars: int) -> str:
    """Interactive/structural lines, de-duplicated, stripped of ids and attrs,
    first *max_chars* worth. Keeps the label a question would ask about."""
    out: list[str] = []
    seen: set[str] = set()
    size = 0
    for line in tree.splitlines():
        s = line.strip()
        if not s or not _KEEP_ROLE_RE.search(s):
            continue
        s = re.sub(r"^\[\d+\]\s*", "", s)                      # element id
        s = re.sub(r",\s*(visible|focused|focusable|url=|live=|atomic|relevant=|expanded=|"
                   r"hasPopup=|selected=|checked=|required|readonly|disabled|pressed=|"
                   r"level=|orientation=|multiselectable|controls=|describedby=|haspopup=)[^,]*", "", s)
        s = " ".join(s.split())
        if s in seen:
            continue
        seen.add(s)
        if size + len(s) + 1 > max_chars:
            break
        out.append(s)
        size += len(s) + 1
    return "\n".join(out)


def trajectory_digest(trajectory: dict[str, Any], *, max_chars: int = 60_000) -> str:
    """The prompt input: goal/outcome/outline and one block per state. The
    per-state tree budget shrinks with the state count so a 100-state
    trajectory still fits."""
    states = trajectory.get("states") or []
    per_state = max(600, min(3_000, (max_chars - 4_000) // max(1, len(states))))
    head = [
        f"Environment: {trajectory.get('environment')} ({trajectory.get('domain')})",
        f"Goal: {' '.join(str(trajectory.get('goal') or '').split())}",
        f"Outcome: {trajectory.get('outcome')}",
        f"Start URL: {trajectory.get('start_url') or ''}",
        f"States: {len(states)}",
        "",
    ]
    blocks: list[str] = []
    for s in states:
        idx = s.get("state_index")
        action = s.get("action")
        thought = " ".join((s.get("thought") or "").split())
        if len(thought) > 300:
            thought = thought[:299] + "…"
        lines = [f"### State {idx}", f"URL: {s.get('url') or ''}",
                 f"Action that led here: {action if action else '(initial state)'}"]
        if thought:
            lines.append(f"Agent thought: {thought}")
        tree = digest_tree(s.get("accessibility_tree") or "", max_chars=per_state)
        if tree:
            lines.append("Page elements:")
            lines.append(tree)
        blocks.append("\n".join(lines))
    return "\n".join(head) + "\n\n".join(blocks)


def prompt_text() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"## System Instructions\s*(.*?)\n## Output", text, re.S)
    system = m.group(1).strip() if m else text
    out = text[text.find("## Output"):] if "## Output" in text else ""
    return system + "\n\n" + out.strip()


def extract_messages(trajectory: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": prompt_text()},
        {"role": "user", "content": "Trajectory:\n\n" + trajectory_digest(trajectory) + "\n\nReturn the JSON array now."},
    ]


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_notes(text: str) -> tuple[list[dict[str, Any]], bool]:
    """``(notes, parse_ok)``. Invalid items are dropped, not repaired."""
    raw = text.strip()
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    i = raw.find("[")
    if i > 0:
        raw = raw[i:]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json  # type: ignore[import-not-found]

            obj = json.loads(repair_json(raw))
        except Exception:  # noqa: BLE001 - no JSON at all
            return [], False
    if not isinstance(obj, list):
        return [], False
    notes: list[dict[str, Any]] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        content = " ".join(str(item.get("content") or "").split())
        title = " ".join(str(item.get("title") or "").split())
        if kind not in NOTE_KINDS or not content:
            continue
        states = [int(x) for x in (item.get("states") or []) if isinstance(x, (int, float)) or str(x).isdigit()]
        conf = item.get("confidence")
        try:
            conf = min(1.0, max(0.0, float(conf))) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        notes.append({"kind": kind, "title": title or content[:60], "content": content,
                      "page": " ".join(str(item.get("page") or "").split()), "states": states[:8],
                      "confidence": conf})
        if len(notes) >= MAX_NOTES:
            break
    return notes, True


@dataclass
class ExtractResult:
    trajectory_id: str
    notes: list[dict[str, Any]] = field(default_factory=list)
    parse_ok: bool = True
    error: str | None = None
    prompt_chars: int = 0
    usage: dict[str, int] = field(default_factory=dict)


def extract_trajectory(ep: llm.Endpoint, trajectory: dict[str, Any], *, max_tokens: int = 4000) -> ExtractResult:
    tid = str(trajectory.get("id"))
    messages = extract_messages(trajectory)
    res = ExtractResult(trajectory_id=tid, prompt_chars=sum(len(m["content"]) for m in messages))
    try:
        comp = llm.chat(ep, messages, temperature=0.0, max_tokens=max_tokens)
    except Exception as e:  # noqa: BLE001 - one trajectory's extraction failing must not abort the build
        res.error = str(e)[:300]
        return res
    res.usage = {"prompt_tokens": getattr(comp, "prompt_tokens", 0) or 0,
                 "completion_tokens": getattr(comp, "completion_tokens", 0) or 0}
    res.notes, res.parse_ok = parse_notes(comp.text)
    return res


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def note_slug(tid: str, i: int, title: str) -> str:
    base = _SLUG_RE.sub("-", title.lower()).strip("-")[:48] or "note"
    return f"traj-{tid}-{i:02d}-{base}"


def save_notes(result: ExtractResult, trajectory: dict[str, Any]) -> list[str]:
    """Write each note through the real ``save_memory``; returns the file paths."""
    from palinode.core.save import save_memory

    tid = result.trajectory_id
    env = str(trajectory.get("environment") or "")
    paths: list[str] = []
    for i, n in enumerate(result.notes):
        body = n["content"]
        if n["page"]:
            body += f"\n\nPage: {n['page']}"
        if n["states"]:
            body += f"\nGrounded in trajectory {tid}, states {', '.join(map(str, n['states']))}."
        out = save_memory(
            content=body,
            type="Insight",
            slug=note_slug(tid, i, n["title"]),
            title=f"[{n['kind']}] {n['title']}",
            entities=[f"trajectory/{tid}", f"env/{env}"] if env else [f"trajectory/{tid}"],
            metadata={"note_kind": n["kind"], "trajectory_id": tid, "states": n["states"],
                      "outcome": trajectory.get("outcome")},
            confidence=n["confidence"],
            epistemic="fact" if n["kind"] in ("fact", "transition") else "inference",
            source="lme-v2-extraction",
        )
        paths.append(str(out.get("file_path") or out.get("path") or ""))
    return paths
