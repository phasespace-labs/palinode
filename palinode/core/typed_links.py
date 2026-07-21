"""Typed relationship links (#533): ``contradicts`` + ``backed_by``.

Supersession (``consolidation/executor.py``) already expresses *resolved*
disagreement: one fact wins, the loser is struck through and logged to history.
Audit-grade provenance also needs to represent the two relationships
supersession can't:

- ``contradicts: [<ref>...]`` — "these conflict, neither wins yet — surface for
  review." No winner is picked; the conflict is recorded so ``lint`` can flag it.
- ``backed_by: [<ref>...]`` — "this fact is supported by that source/fact."

Both are plaintext frontmatter lists. Refs follow the same ``category/slug`` identity
convention used elsewhere — the path-relative id of a memory file.

This module is the single home for: validation, the soft-fail read accessor, the
content-level merge primitive (used by both the save surface's reciprocal
back-link and the consolidation executor's PROPOSE_CONTRADICTS op), and the
best-effort reciprocal back-link writer.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger("palinode.core.typed_links")

#: Frontmatter keys that hold a list of typed memory refs.
TYPED_LINK_FIELDS: tuple[str, ...] = ("contradicts", "backed_by")

# A ref is the path-relative identity of a memory: ``category/slug`` (optionally
# with nested subdirs or a trailing ``.md``). Reject traversal, absolute paths,
# and whitespace/control characters so a malformed ref can never escape the
# memory dir when later resolved to a file path.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class TypedLinkError(ValueError):
    """Raised when a typed-link ref list is malformed (caller maps to HTTP 400)."""


def normalize_link_refs(raw: Any, field: str) -> list[str]:
    """Validate and normalize a typed-link ref list.

    Accepts a list of refs, or a single ref string (coerced to a one-element
    list). Each ref must be a non-empty, well-formed ``category/slug`` string.
    Duplicates are dropped, order preserved. Returns ``[]`` for ``None``.

    Raises :class:`TypedLinkError` on any malformed input — the save surface
    wraps that as HTTP 400, mirroring how ``sources``/``external_refs`` reject
    malformed input at the boundary.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise TypedLinkError(f"{field} must be a list of refs (got {type(raw).__name__})")

    out: list[str] = []
    seen: set[str] = set()
    for i, ref in enumerate(raw):
        if not isinstance(ref, str) or not ref.strip():
            raise TypedLinkError(f"{field}[{i}] must be a non-empty string")
        r = ref.strip()
        if ".." in r or r.startswith("/") or "\n" in r or not _REF_RE.match(r):
            raise TypedLinkError(f"{field}[{i}] is not a well-formed ref: {ref!r}")
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def parse_link_refs(metadata: dict[str, Any], field: str) -> list[str]:
    """Soft-fail accessor for reading a typed-link list from parsed frontmatter.

    Consistent with the parser's soft-fail style (see ``parser.parse_sources``):
    a single string is coerced to a list, malformed entries are dropped, and a
    missing/non-list field returns ``[]`` so a file with no links round-trips as
    a clean no-op. Validation belongs at the save surface; reads never raise.
    """
    raw = metadata.get(field)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for ref in raw:
        if isinstance(ref, str) and ref.strip():
            out.append(ref.strip())
    return out


def merge_link_refs_into_content(content: str, field: str, refs: list[str]) -> str:
    """Return ``content`` with ``refs`` merged into frontmatter list ``field``.

    Idempotent: refs already present are not duplicated, and when nothing
    changes the original ``content`` is returned unchanged (so callers can skip a
    no-op write/commit). The body is preserved verbatim; only the frontmatter is
    re-dumped (key order preserved). Non-destructive — never removes existing
    refs or any other frontmatter field.
    """
    if not refs:
        return content

    import frontmatter as _frontmatter
    import yaml

    post = _frontmatter.loads(content)
    existing = parse_link_refs(post.metadata, field)
    merged = list(existing)
    changed = False
    for r in refs:
        if r not in merged:
            merged.append(r)
            changed = True
    if not changed:
        return content

    meta = dict(post.metadata)
    meta[field] = merged
    body = post.content
    dumped = yaml.safe_dump(
        meta, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return f"---\n{dumped}---\n\n{body}\n"


def _ref_to_path(base_dir: str, ref: str) -> str | None:
    """Resolve a ``category/slug`` ref to an absolute file path inside ``base_dir``.

    Returns ``None`` when the ref would escape ``base_dir`` (defense-in-depth on
    top of :func:`normalize_link_refs`'s traversal rejection).
    """
    rel = ref if ref.endswith(".md") else f"{ref}.md"
    candidate = os.path.realpath(os.path.join(base_dir, rel))
    base_real = os.path.realpath(base_dir)
    try:
        if os.path.commonpath([base_real, candidate]) != base_real:
            return None
    except ValueError:
        return None
    return candidate


def add_reciprocal_contradicts(
    base_dir: str,
    source_ref: str,
    target_refs: list[str],
    *,
    commit: bool = True,
) -> list[str]:
    """Best-effort: add ``source_ref`` to each target file's ``contradicts`` list.

    ``contradicts`` is a symmetric relationship — if A contradicts B, B
    contradicts A — so when A declares ``contradicts: [B]`` we add A back into
    B's list so the conflict surfaces from both sides in ``lint``. This is
    deliberately scoped to ``contradicts`` only; ``backed_by`` is directional
    (A cites B does not mean B cites A) and gets no reciprocal write.

    Idempotent (skips targets that already reference ``source_ref``) and never
    raises — a missing/unreadable target is logged and skipped so the back-link
    can never fail the originating save. Writes go through the
    ``git_tools.write_memory_file`` mutation choke point; modified files are
    committed in one commit when ``commit`` is True and git auto-commit is on.

    Returns the list of file paths actually modified.
    """
    from palinode.core import git_tools

    modified: list[str] = []
    for target in target_refs:
        if target == source_ref:
            continue  # a memory can't contradict itself
        try:
            target_path = _ref_to_path(base_dir, target)
            if not target_path or not os.path.exists(target_path):
                logger.debug("reciprocal contradicts: target %r not on disk; skipped", target)
                continue
            with open(target_path, encoding="utf-8") as f:
                existing_content = f.read()
            updated = merge_link_refs_into_content(
                existing_content, "contradicts", [source_ref]
            )
            if updated == existing_content:
                continue  # already linked — idempotent no-op
            git_tools.write_memory_file(target_path, updated)
            modified.append(target_path)
        except Exception as exc:  # noqa: BLE001 — best-effort, never fail the save
            logger.warning(
                "reciprocal contradicts back-link failed for target %r: %s",
                target, exc,
            )

    if modified and commit:
        try:
            from palinode.core import git_tools as _gt
            _gt.commit_memory_files(
                modified,
                f"{__commit_prefix()} contradicts back-link: {source_ref}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reciprocal contradicts commit failed: %s", exc)

    return modified


def __commit_prefix() -> str:
    """Resolve the configured git commit prefix (fallback when config is absent)."""
    try:
        from palinode.core.config import config

        return config.git.commit_prefix
    except Exception:  # noqa: BLE001
        return "palinode:"
