"""
Canonical YAML frontmatter parsing and serialization for the consolidation package.

Deduplicates the ``---``-delimited frontmatter handling that was previously
inlined in runner.py and layer_split.py.  Uses ``yaml.safe_load`` /
``yaml.safe_dump`` directly (not the ``python-frontmatter`` library) to
match the consolidation package's existing convention.

Public API:
    parse_frontmatter(text) -> (dict, str)
    serialize_frontmatter(meta, body) -> str
"""
from __future__ import annotations

import yaml


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a markdown file's YAML frontmatter.

    Returns:
        (meta, body) -- meta is the parsed dict (empty if no frontmatter),
        body is the remaining markdown after the closing ``---``.

    The function is lenient: malformed YAML returns ``({}, original_text)``.
    Files without a leading ``---`` return ``({}, original_text)``.
    """
    if not text.startswith("---"):
        return {}, text

    # Split at most twice on '---' to get [before_first, yaml_block, rest].
    # The first element is always empty (everything before the opening '---').
    parts = text.split("---", 2)
    if len(parts) < 3:
        # Opening '---' found but no closing '---' -- treat as no frontmatter.
        return {}, text

    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}, text

    if not isinstance(meta, dict):
        # YAML parsed to a non-dict (e.g. a bare string) -- not frontmatter.
        return {}, text

    body = parts[2]
    # Strip exactly one leading newline that follows the closing '---'.
    # Preserve all other whitespace so callers that join body back get
    # semantically equivalent output.
    if body.startswith("\n"):
        body = body[1:]

    return meta, body


def serialize_frontmatter(meta: dict, body: str) -> str:
    """Combine frontmatter dict and body back into file text.

    If *meta* is empty, returns *body* unchanged (no frontmatter wrapper).
    Otherwise emits ``---\\n<yaml>\\n---\\n<body>``.
    Uses block-style YAML (``default_flow_style=False``) for stable diffs.
    """
    if not meta:
        return body

    yaml_str = yaml.safe_dump(
        meta,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return f"---\n{yaml_str}---\n{body}"
