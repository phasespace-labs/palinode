"""Plain-text renderings of lifecycle results, shared by the CLI and MCP.

The API returns structured blocks — a dry-run preview, a ``retained_copies``
list — and two text surfaces turn them into lines. One renderer keeps them
saying the same thing, which is the parity the lifecycle previews promise:
the record, the frontmatter delta, the relation recorded or removed, the
copies a retirement does not reach, the recovery path, and that nothing was
written. Pure: dicts in, strings out.
"""
from __future__ import annotations

from typing import Any


def render_retained_copies(block: dict[str, Any] | None) -> list[str]:
    """Plain-text lines for a ``retained_copies`` block, the same on every surface.

    The block is :func:`palinode.corrections.review.retained_copies`' output,
    as it crosses the API. Nothing is rendered when nothing was found — a
    retirement that reached every copy has nothing to warn about. Rows past the
    block's bound are counted as "N more"; records the caller may not see are
    counted and never named.
    """
    if isinstance(block, dict) and block.get("error"):
        return [f"Retained copies not checked: {block['error']}"]
    if not isinstance(block, dict) or not block.get("total"):
        return []
    lines = [
        f"Not reached, still in default recall ({block['total']}) — "
        "reported, never changed:"
    ]
    for row in block.get("records") or []:
        relations = ", ".join(row.get("relations") or [])
        lines.append(
            f"  - {row.get('file')} ({relations} → {row.get('of')}): "
            f"{row.get('action')}"
        )
    if block.get("more"):
        lines.append(f"  … and {block['more']} more")
    if block.get("not_visible"):
        lines.append(
            f"  {block['not_visible']} more you cannot see (counted, not named)"
        )
    return lines


_PREVIEW_VERBS = {
    "would_archive": "Would archive",
    "would_restore": "Would restore",
    "would_unretract": "Would unretract",
    "would_withdraw": "Would withdraw the forget request in",
}


def _render_value(value: Any) -> str:
    return "(unset)" if value is None else str(value)


def _render_delta(delta: dict[str, Any] | None) -> list[str]:
    return [
        f"  {field}: {_render_value(change.get('from'))} → "
        f"{_render_value(change.get('to'))}"
        for field, change in (delta or {}).items()
        if isinstance(change, dict)
    ]


def render_lifecycle_preview(data: dict[str, Any]) -> list[str]:
    """Plain-text lines for an archive / restore / unretract / forget-withdraw dry run.

    One renderer so the CLI and MCP say the same thing: the record, the
    frontmatter delta, the relation recorded or removed, the retained copies
    and the recovery path — and that nothing was written.
    """
    status = str(data.get("status") or "")
    verb = _PREVIEW_VERBS.get(status)
    if verb is None:
        # A no-op (already_archived / not_archived / not_retracted) previews
        # as itself: there is nothing that would change.
        return [
            f"Dry run — no change: {data.get('file')} is {status}. Nothing written."
        ]
    lines = [f"Dry run — {verb} {data.get('file')}. Nothing written."]
    if status == "would_unretract":
        lines.append(f"Spans to un-strike: {data.get('mentions', 0)}")
    delta = _render_delta(data.get("frontmatter_delta"))
    if delta:
        lines.append("Frontmatter delta:")
        lines.extend(delta)
    relation = data.get("relation") or {}
    for label in ("recorded", "removed"):
        for item in relation.get(label) or []:
            lines.append(f"Relation {label}: {item}")
    if data.get("stale_backing"):
        lines.append("Would flag stale backing: " + ", ".join(data["stale_backing"]))
    if data.get("expires_at"):
        lines.append(
            f"Note: expires_at is {data['expires_at']} — the TTL sweep will "
            "re-archive it unless the expiry is changed."
        )
    if status == "would_withdraw":
        restore = [str(r.get("file")) for r in data.get("would_restore") or []]
        lines.append(f"Would restore ({len(restore)}): {', '.join(restore) or 'none'}")
        unretract = [
            f"{u.get('file')} ({u.get('mentions', 0)} span(s))"
            for u in data.get("would_unretract") or []
        ]
        lines.append(
            f"Would unretract ({len(unretract)}): {', '.join(unretract) or 'none'}"
        )
        records = [
            f"{r.get('file')} ({r.get('status')})"
            for r in data.get("requests_to_archive") or []
        ]
        lines.append(f"Would archive request records: {', '.join(records) or 'none'}")
    lines.extend(render_retained_copies(data.get("retained_copies")))
    recovery = data.get("recovery") or {}
    if recovery.get("command"):
        lines.append(f"Recovery: {recovery['command']}")
    if recovery.get("note"):
        lines.append(f"Recovery note: {recovery['note']}")
    return lines
