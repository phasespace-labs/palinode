"""The visibility enforcement choke point (ADR-009 Layer 2).

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

**Project isolation lives here too.** A chain carrying a project hides
records tagged to a *different* project (:func:`palinode.core.scope.
other_project`); records naming no project stay visible as global. Search,
resolve (seeds and every evidence expansion) and the prime all reach it
through this module, so no surface reimplements it. A request opts out with
``ScopeChain.include_other_projects``; :func:`filter_visible` counts what
isolation withheld so a scoped empty result can say why.

Internal / maintenance callers (consolidation, dedup-suggest, orphan-repair,
``search_internal``) deliberately do **not** route through here: they must see
every memory to do their job, and they never return content to a session.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

from palinode.core.config import config
from palinode.core.scope import (
    ScopeChain,
    access_allows,
    other_project,
    project_entities,
    visible_on_chain,
)

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
            meta, _ = parser.parse_frontmatter(fh.read())
    except (OSError, ValueError, UnicodeDecodeError):
        return _UNREADABLE
    return meta if isinstance(meta, dict) else {}


def _evaluated_metadata(
    file_path: str | None,
    *,
    metadata: dict[str, Any] | None,
    fallback_metadata: dict[str, Any] | None,
    cache: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """The frontmatter :func:`is_visible` decides on, or ``None`` for none.

    Precedence as :func:`is_visible` documents it: caller-supplied live
    metadata, then the file, then the cached fallback.
    """
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
            return fallback_metadata
        return None
    return meta


def withheld_as_other_project(
    chain: ScopeChain | None,
    file_path: str | None,
    *,
    metadata: dict[str, Any] | None = None,
    fallback_metadata: dict[str, Any] | None = None,
    cache: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Was this memory left out *because* it is tagged to another project?

    The count a delivery reports so that an empty scoped result explains
    itself. Only project isolation is counted — a ``private`` or
    ``restricted`` memory hidden by access control is never counted or named
    here, exactly as before. Call it for a record :func:`is_visible` refused.
    """
    if chain is None or chain.include_other_projects:
        return False
    meta = _evaluated_metadata(
        file_path, metadata=metadata, fallback_metadata=fallback_metadata, cache=cache,
    )
    return meta is not None and other_project(chain, meta)


def other_project_refs(
    chain: ScopeChain | None,
    file_path: str | None,
    *,
    fallback_metadata: dict[str, Any] | None = None,
) -> list[str]:
    """The record's own ``project/*`` refs when it belongs to another project.

    Empty when the record is global, names the chain's project, or the chain
    has no project. The label an opted-in (``include_other_projects``)
    delivery puts beside such a record; the opt-in itself is not consulted.
    """
    if chain is None or not chain.project:
        return []
    meta = _evaluated_metadata(
        file_path, metadata=None, fallback_metadata=fallback_metadata, cache=None,
    )
    if meta is None or not other_project(chain, meta):
        return []
    return project_entities(meta)


def is_visible(
    chain: ScopeChain | None,
    file_path: str | None,
    *,
    metadata: dict[str, Any] | None = None,
    fallback_metadata: dict[str, Any] | None = None,
    cache: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """May a session on ``chain`` see the memory at ``file_path``?

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

    meta = _evaluated_metadata(
        file_path, metadata=metadata, fallback_metadata=fallback_metadata, cache=cache,
    )
    if meta is None:
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
    other_projects: set[str] | None = None,
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

    ``other_projects``, when given, collects the memory-dir-relative path of
    every row left out by project isolation (a record tagged to a project
    other than the chain's), so the caller can report how many it withheld.
    """
    cache: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        cached = row.get(metadata_key)
        fallback = cached if isinstance(cached, dict) else {}
        path = row.get(path_key)
        if is_visible(chain, path, fallback_metadata=fallback, cache=cache):
            out.append(row)
        elif other_projects is not None and withheld_as_other_project(
            chain, path, fallback_metadata=fallback, cache=cache,
        ):
            other_projects.add(normalize_memory_path(path) or str(path))
    return out
