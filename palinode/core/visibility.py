"""The visibility enforcement choke point (ADR-009 Layer 2, #108).

Every recall surface — ``GET /list``, ``POST /search`` (both the semantic and
the empty-query recency branch), ``POST /search-associative``, and the
``/context/prime`` session-start digest — decides "may this session see this
memory?" here, and nowhere else. Two properties are the reason this is one
module instead of a filter per caller:

**Live frontmatter, never the DB cache.** The indexer's unchanged-content fast
path (``indexer/index_file.py``) skips the chunk upsert when a file's *section
body* hash is unchanged, and frontmatter is not part of that hash. Marking an
existing memory ``visibility: private`` — the canonical way, since files are
the source of truth — therefore leaves ``chunks.metadata`` holding the old,
non-private frontmatter indefinitely. A search that filtered on the cached
metadata would keep serving the memory it was just told to hide, while
``/list`` and the digest (which read files directly) correctly hid it. So
enforcement always reads the file, unless the caller has *already* read it
this request and passes ``metadata`` explicitly.

**One path format.** ``chunks.file_path`` is absolute; the digest scanner
yields memory-dir-relative paths. ``parser._default_scope_from_path`` reads
the parent directory name, so the same root-level memory infers
``project/<memory-dir-basename>`` from one and nothing from the other — a
divergence that hides a memory on one surface and leaks it on the next. Every
path is normalized to memory-dir-relative before any inference.

Internal / maintenance callers (consolidation, dedup-suggest, orphan-repair,
``search_internal``) deliberately do **not** route through here: they must see
every memory to do their job, and they never return content to a session.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Sequence

from palinode.core.config import config
from palinode.core.scope import ScopeChain, access_allows, visible_on_chain

logger = logging.getLogger("palinode.visibility")

#: Sentinel for "frontmatter could not be read" — distinct from empty
#: frontmatter, which is a legitimate (and visible) state.
_UNREADABLE: dict[str, Any] = {"__palinode_unreadable__": True}


def _memory_root() -> str:
    return os.path.realpath(getattr(config, "memory_dir", None) or config.palinode_dir)


def normalize_memory_path(file_path: str) -> str | None:
    """Return ``file_path`` as a memory-dir-relative path.

    Absolute paths under the memory dir are made relative; already-relative
    paths are normalized. Returns ``None`` when the path is absolute but falls
    outside the memory dir (nothing sane can be inferred from it, and callers
    treat that as "no path information" rather than guessing a scope).
    """
    if not file_path:
        return None
    if not os.path.isabs(file_path):
        return os.path.normpath(file_path)
    try:
        rel = os.path.relpath(os.path.realpath(file_path), _memory_root())
    except ValueError:
        # Different drives on Windows — no meaningful relative form.
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel


def _read_frontmatter(file_path: str) -> dict[str, Any]:
    """Parse a memory file's live frontmatter, or ``_UNREADABLE`` on failure."""
    from palinode.core import parser

    try:
        with open(file_path, encoding="utf-8") as fh:
            meta, _ = parser.parse_markdown(fh.read())
    except (OSError, ValueError, UnicodeDecodeError):
        return _UNREADABLE
    return meta if isinstance(meta, dict) else {}


def is_visible(
    chain: ScopeChain | None,
    file_path: str | None,
    *,
    metadata: dict[str, Any] | None = None,
    fallback_metadata: dict[str, Any] | None = None,
    cache: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """May a session on ``chain`` see the memory at ``file_path``? (#108)

    ``chain`` semantics:

    - **A ScopeChain** (including a deliberately empty one): full ADR-009
      Layer 2 evaluation via :func:`visible_on_chain` — scope isolation *and*
      access control. Passing an empty chain means "filter against nothing",
      which correctly hides every explicitly-scoped memory; that is the Layer 1
      selection contract and it is preserved.
    - **``None``** — the caller has no scope context at all (``GET /list``,
      classic priming, a search whose chain resolved to no identity): access
      control only via :func:`access_allows`. ``private`` and ``restricted``
      memories are never returned; ``inherited`` memories pass untouched,
      including explicitly-scoped ones.

    Deciding *whether* a caller has scope context is the caller's job — see
    ``_resolve_search_scope_chain``, which returns ``None`` unless the chain
    carries a real identity level so that a bare ADR-007 ``session_id``
    (telemetry, not identity) cannot silently hide every scoped memory.

    Metadata resolution, in precedence order:

    1. ``metadata`` — for callers that already parsed this file's live
       frontmatter during this request (the listing helper, the digest
       scanner). **Never pass DB-cached metadata here**: see the module
       docstring.
    2. The file's live frontmatter, read from disk. Authoritative.
    3. ``fallback_metadata`` — the row's cached metadata, used **only** when
       the file cannot be read. This keeps index/disk divergence (a deleted
       file behind a stale index) behaving exactly as it did before this
       layer existed, rather than silently emptying result sets. It is a
       last resort, never a shortcut: when the file is readable its live
       frontmatter always wins, which is what closes the stale-cache leak.
    4. Nothing at all → hidden. We cannot prove a memory is not private
       without something to read.
    """
    rel = normalize_memory_path(file_path) if file_path else None

    meta = metadata
    if meta is None and file_path:
        if cache is not None and file_path in cache:
            meta = cache[file_path]
        else:
            meta = _read_frontmatter(file_path)
            if cache is not None:
                cache[file_path] = meta
    if meta is None or meta is _UNREADABLE:
        if fallback_metadata is not None:
            logger.debug(
                "visibility: %r unreadable — falling back to cached metadata",
                file_path,
            )
            meta = fallback_metadata
        else:
            logger.debug("visibility: nothing to evaluate for %r — hiding", file_path)
            return False

    if chain is None:
        return access_allows(meta, file_path=rel)
    return visible_on_chain(chain, meta, file_path=rel)


def filter_visible(
    chain: ScopeChain | None,
    rows: Iterable[dict[str, Any]],
    *,
    path_key: str = "file_path",
    metadata_key: str = "metadata",
) -> list[dict[str, Any]]:
    """Filter search-result rows through :func:`is_visible`.

    Live frontmatter decides (one read per distinct file, cached across the
    batch). The row's own ``metadata`` is used **only** as the unreadable-file
    fallback — never as the primary source, because for search rows it is a DB
    cache the indexer leaves stale after a frontmatter-only edit.

    A row whose file is unreadable *and* which carries no cached metadata
    falls back to empty frontmatter (i.e. visible), not to hidden. For a row
    to be wrongly shown that way, the file must be gone **and** the index must
    have recorded no frontmatter — which is the pre-existing "indexed row with
    no file behind it" state, whose behavior this layer deliberately does not
    change. Whenever the file is readable — the normal case — its live
    frontmatter is authoritative.
    """
    cache: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        cached = row.get(metadata_key)
        if is_visible(
            chain,
            row.get(path_key),
            fallback_metadata=cached if isinstance(cached, dict) else {},
            cache=cache,
        ):
            out.append(row)
    return out


def visible_count(chain: ScopeChain | None, paths: Sequence[str]) -> int:
    """Count how many of ``paths`` are visible — for diagnostics/tests."""
    cache: dict[str, dict[str, Any]] = {}
    return sum(1 for p in paths if is_visible(chain, p, cache=cache))
