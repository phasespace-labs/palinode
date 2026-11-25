"""Search row shaping: the filters and enrichment run over a slate.

What :func:`palinode.core.search.search` does to the rows the store returns:
the date window, the type and priority filters, the query-centered snippet,
the tier view and the memory-relative path. Moved from ``api/search_helpers.py``
with the search module, which re-exports these names.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from palinode.core.agent_directed import withhold_agent_directed
from palinode.core.config import config
from palinode.core.path_guard import to_rel_path
from palinode.core.ranker import parse_iso_utc
from palinode.core.tiers import apply_tier


def _compute_effective_date_after(req: Any) -> str | None:
    """Combine explicit date_after with since_days; pick the more restrictive.

    Returns the ISO-8601 string (UTC, "Z" suffix) representing the earliest
    creation/update time a result is allowed to have. ``since_days`` derives
    `now - since_days days`. If both are set, takes the later (later → more
    restrictive). If neither is set, returns None.
    """
    derived = None
    if req.since_days and req.since_days > 0:
        threshold_dt = datetime.now(UTC) - timedelta(days=req.since_days)
        derived = threshold_dt.isoformat().replace("+00:00", "Z")
    explicit = req.date_after
    if derived and explicit:
        explicit_dt = parse_iso_utc(explicit)
        return derived if explicit_dt is None or threshold_dt > explicit_dt else explicit
    return derived or explicit


def _filter_types(results: list[dict[str, Any]], types: list[str] | None) -> list[dict[str, Any]]:
    """Drop results whose frontmatter `type` isn't in the allowed list.

    Empty / None ``types`` is a no-op. Filter is OR-style: a result keeps if its
    type matches any of the values.
    """
    if not types:
        return results
    allowed = set(types)
    return [r for r in results if r.get("metadata", {}).get("type") in allowed]


def _filter_type_deny(
    results: list[dict[str, Any]], type_deny: list[str] | None
) -> list[dict[str, Any]]:
    """Exclude results whose frontmatter `type` is in the deny list.

    Empty / None ``type_deny`` is a no-op. Takes precedence over the allow-list:
    if a type is in both ``types`` and ``type_deny``, the result is dropped.
    """
    if not type_deny:
        return results
    denied = set(type_deny)
    return [r for r in results if r.get("metadata", {}).get("type") not in denied]


def _priority_value(metadata: Any) -> int:
    if not isinstance(metadata, dict):
        return 3
    try:
        priority = int(metadata.get("priority", 3))
    except (TypeError, ValueError):
        return 3
    return priority if 1 <= priority <= 5 else 3


def _filter_min_priority(
    results: list[dict[str, Any]], min_priority: int | None
) -> list[dict[str, Any]]:
    if min_priority is None:
        return results
    return [
        r for r in results
        if _priority_value(r.get("metadata", {})) >= min_priority
    ]


def _resolve_snippet_max_chars(req_max_chars: int | None) -> int:
    """Return the effective snippet cap for a request.

    Uses the per-request override when supplied (clamped to [1, 8000]),
    falling back to the config default.
    """
    if req_max_chars is not None:
        return max(1, min(req_max_chars, 8000))
    return config.search.snippet_max_chars


def _windowed_snippet(content: str, query: str, max_chars: int) -> str:
    """Return a query-centered window of ``content`` no longer than ``max_chars``.

    Strategy: find the earliest case-insensitive substring hit for any
    whitespace-split token of ``query`` (len >= 3 to skip noise like "to"/"in"),
    then slice a window centered on that hit. Falls back to the leading
    ``max_chars`` when nothing matches — which is the correct vector-only
    behavior, since the chunk itself is already the relevant semantic window.

    No FTS5 round-trip: the chunk content is already in memory.
    """
    if len(content) <= max_chars:
        return content
    tokens = [t for t in re.split(r"\s+", query.strip()) if len(t) >= 3]
    lower = content.lower()
    hit = -1
    for tok in tokens:
        idx = lower.find(tok.lower())
        if idx != -1 and (hit == -1 or idx < hit):
            hit = idx
    if hit == -1:
        # Leading window — ellipsis suffix only (no prefix needed).
        return content[:max_chars].rstrip() + "…"
    # Center the window on the match, but clamp to content bounds.
    half = max_chars // 2
    start = max(0, hit - half)
    end = min(len(content), start + max_chars)
    # If we hit the right edge, shift the window left so we still fill it.
    if end - start < max_chars:
        start = max(0, end - max_chars)
    snippet = content[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{snippet}{suffix}"


def _enrich_with_snippets(
    results: list[dict[str, Any]], query: str, max_chars: int
) -> None:
    """In-place add ``snippet`` and ``content_truncated`` to each result.

    The ``content`` field keeps the full chunk body so API/CLI consumers that
    legitimately want it are unchanged. MCP callers render ``snippet`` by
    default to stay within MCP tool-result budgets.

    Both are delivery, so text addressed to AI agents is withheld from both
    before the window is cut, and ``agent_directed_withheld`` says whether any
    was. ``palinode_read`` shows the record whole.
    """
    for r in results:
        content, withheld = withhold_agent_directed(r.get("content") or "")
        if withheld:
            r["content"] = content
        r["agent_directed_withheld"] = withheld
        if len(content) <= max_chars:
            r["snippet"] = content
            r["content_truncated"] = False
        else:
            r["snippet"] = _windowed_snippet(content, query, max_chars)
            r["content_truncated"] = True


def _apply_tier(results: list[dict[str, Any]], tier: str | None) -> None:
    """In-place render each hit at ``tier``.

    ``None`` and ``"full"`` are no-ops, so a caller that says nothing keeps the
    snippet-plus-content shape search has always returned. For the two cheaper
    tiers both ``content`` and ``snippet`` become the view — a caller that asked
    for an abstract does not want the full chunk arriving under another key —
    and ``content_truncated`` reports whether anything was dropped.

    Search hits are chunks, not whole files, so the view is computed over the
    chunk body: the abstract of a hit is the abstract of the part that matched.
    """
    if tier is None or tier == "full":
        return
    for r in results:
        content = r.get("content") or ""
        view = apply_tier(tier, content, r.get("metadata"))
        r["content"] = view
        r["snippet"] = view
        r["content_truncated"] = len(view) < len(content)
        r["tier"] = tier


def _enrich_with_rel_path(results: list[dict[str, Any]]) -> None:
    """In-place add ``rel_path`` (memory-relative) alongside each ``file_path``.

    ``chunks.file_path`` is always stored absolute (see ``core/store.py``),
    so every result carries a filesystem path. ``rel_path`` is the
    server-computed relative spelling — derived from ``config.memory_dir``,
    the one place that knows it — so MCP/CLI never have to guess it from a
    hardcoded literal. Additive: ``file_path`` is left untouched.
    """
    for r in results:
        fp = r.get("file_path")
        if fp:
            r["rel_path"] = to_rel_path(fp)
