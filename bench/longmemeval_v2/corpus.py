"""Trajectory → markdown: the raw state-slice pool.

One file per trajectory. ``parse_markdown`` splits on ``##``/``###`` headings,
so every ``## State N`` section is one chunk — state-level retrieval, the
granularity the upstream ``rag_query_to_slice`` baseline uses. Accessibility
trees past the embedder window are cut into ``### State N (part k)`` sub-chunks
rather than truncated: static-recall questions ask about labels that can sit
anywhere in a 300k-char tree.

Schema (``SCHEMA.md``): a state's ``action`` is the action that *produced* it
(``None`` on state 0); ``thought`` is the agent's reasoning at that state about
what to do next.
"""
from __future__ import annotations

import re
from typing import Any

# bge-m3's window is 8192 tokens; a11y trees run ~4 chars/token, and the
# section header + fence + url/action/thought lines need headroom.
DEFAULT_SLICE_MAX_CHARS = 20_000

_UNSAFE_RE = re.compile(r"[^\w.-]+")


def trajectory_rel_path(trajectory_id: str) -> str:
    return f"trajectories/{_UNSAFE_RE.sub('_', str(trajectory_id))}.md"


def _yaml_str(value: Any) -> str:
    """Single-line, double-quoted YAML scalar."""
    s = str(value if value is not None else "").replace("\\", "\\\\").replace('"', '\\"')
    return '"' + " ".join(s.split()) + '"'


def _first_clause(text: str | None, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    clause = m.group(1) if m else text
    return clause if len(clause) <= limit else clause[: limit - 1] + "…"


def _split_tree(tree: str, max_chars: int) -> list[str]:
    """Cut a tree into parts on line boundaries, each at most *max_chars*."""
    if len(tree) <= max_chars:
        return [tree]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in tree.splitlines(keepends=True):
        if size + len(line) > max_chars and buf:
            parts.append("".join(buf))
            buf, size = [], 0
        if len(line) > max_chars:  # one pathological line: hard-cut it
            for i in range(0, len(line), max_chars):
                parts.append(line[i : i + max_chars])
            continue
        buf.append(line)
        size += len(line)
    if buf:
        parts.append("".join(buf))
    return parts


_LEADING_HASH_RE = re.compile(r"^(#{2,3})(?=\s)", re.M)


def _fence(text: str) -> str:
    """Fence the tree, with a fence longer than any backtick run it contains.
    ``parse_markdown`` splits on ``^##`` without honouring fences, so a
    line-leading ``##``/``###`` (a11y trees are tab-indented; it happens in
    free text) is escaped rather than allowed to open a section."""
    text = _LEADING_HASH_RE.sub(r"\\\1", text)
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text.rstrip()}\n{fence}"


def outline_lines(trajectory: dict[str, Any]) -> list[str]:
    lines = []
    for s in trajectory.get("states") or []:
        idx = s.get("state_index")
        action = s.get("action")
        thought = _first_clause(s.get("thought"))
        head = f"- State {idx}: " + (f"`{action}`" if action else "(initial state)")
        lines.append(head + (f" — {thought}" if thought else ""))
    return lines


def state_sections(state: dict[str, Any], *, slice_max_chars: int) -> list[tuple[str, str]]:
    """``(heading, body)`` pairs for one state: the header block, then the
    a11y tree as one section or as ``### … (part k)`` sub-sections."""
    idx = state.get("state_index")
    meta = [f"- **URL:** {state.get('url') or ''}"]
    action = state.get("action")
    meta.append(f"- **Action that led here:** `{action}`" if action else "- **Action that led here:** (initial state)")
    thought = " ".join((state.get("thought") or "").split())
    if thought:
        meta.append(f"- **Agent thought at this state:** {thought}")
    header = "\n".join(meta)
    tree = state.get("accessibility_tree") or ""
    budget = slice_max_chars - len(header) - 200
    parts = _split_tree(tree, max(budget, slice_max_chars // 2)) if tree else []
    if len(parts) <= 1:
        body = header + ("\n\n" + _fence(parts[0]) if parts else "")
        return [(f"## State {idx}", body)]
    out: list[tuple[str, str]] = [(f"## State {idx}", header + f"\n\n(accessibility tree in {len(parts)} parts below)")]
    for k, part in enumerate(parts, 1):
        out.append((f"### State {idx} (part {k} of {len(parts)})", header + "\n\n" + _fence(part)))
    return out


def trajectory_markdown(trajectory: dict[str, Any], *, slice_max_chars: int = DEFAULT_SLICE_MAX_CHARS) -> str:
    tid = str(trajectory.get("id"))
    goal = " ".join(str(trajectory.get("goal") or "").split())
    states = trajectory.get("states") or []
    last_action = next((s.get("action") for s in reversed(states) if s.get("action")), None)
    fm = "\n".join([
        "---",
        f"trajectory_id: {_yaml_str(tid)}",
        f"domain: {_yaml_str(trajectory.get('domain'))}",
        f"environment: {_yaml_str(trajectory.get('environment'))}",
        f"outcome: {_yaml_str(trajectory.get('outcome'))}",
        f"steps: {len(states)}",
        "type: Trajectory",
        "---",
    ])
    head = [
        f"# Trajectory {tid} — {_first_clause(goal, 120) or 'no goal'}",
        "",
        f"**Goal:** {goal}",
        f"**Outcome:** {trajectory.get('outcome')}",
        f"**Environment:** {trajectory.get('environment')} ({trajectory.get('domain')})",
        f"**Start URL:** {trajectory.get('start_url') or ''}",
        f"**Steps:** {len(states)}" + (f" — final action `{last_action}`" if last_action else ""),
    ]
    outline = outline_lines(trajectory)
    sections: list[str] = ["\n".join(head)]
    # The outline is one chunk; a 100-state trajectory's outline stays well
    # under the window, but split defensively on the same budget.
    outline_text = "\n".join(outline)
    if len(outline_text) <= slice_max_chars:
        sections.append(f"## Outline\n\n{outline_text}")
    else:
        for k, part in enumerate(_split_tree(outline_text + "\n", slice_max_chars), 1):
            sections.append(f"## Outline (part {k})\n\n{part.rstrip()}")
    for s in states:
        for heading, body in state_sections(s, slice_max_chars=slice_max_chars):
            sections.append(f"{heading}\n\n{body}")
    return fm + "\n" + "\n\n".join(sections) + "\n"


_HIT_RE = re.compile(r"trajectories/([^/]+)\.md$")
_STATE_RE = re.compile(r"^-\s+\*\*URL:\*\*|^\(accessibility tree", re.M)


def hit_trajectory_id(file_path: str) -> str | None:
    m = _HIT_RE.search(file_path.replace("\\", "/"))
    return m.group(1) if m else None
