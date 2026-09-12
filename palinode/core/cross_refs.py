"""Mechanical, untyped cross-linking between memory files.

The deterministic complement to the LLM-generated ``[[wikilinks]]`` /
``entities`` machinery (PROGRAM.md wiki-maintenance contract). During indexing,
the watcher scans a memory's body for mentions of OTHER memory files — by their
path ref (``category/slug``), distinctive slug, or distinctive title — and
records the matches in an untyped ``cross_refs`` frontmatter list.

Design (see the mechanical cross-linking work, and its deconfliction with the
typed contradiction/evidence links):
  - **Untyped only.** ``cross_refs`` says "this memory mentions that one"; it does
    NOT say *how* they relate. Typed relations (``contradicts`` / ``backed_by``)
    are handled by the typed contradiction/evidence links. Untyped refs are deterministic and "always correct".
  - **Conservative matching.** Better to miss a link than mint a false one, so
    short/generic slugs and titles are skipped (the fuzzy layer is the LLM
    wikilink path). Matching is whole-token / whole-phrase, case-insensitive.
  - **Directional.** Only the scanned file's own outbound mentions are recorded;
    no back-references are written into the mentioned file.
  - **Idempotent.** The file is rewritten + committed only when the computed
    ``cross_refs`` differs from what's already there — so a watcher re-processing
    its own write terminates after one pass (no write-amplification loop).

Ref identity is the memory's path-relative ``category/slug`` (matching the on-disk
layout and the ``/ui/memory/<path>`` route), e.g. ``decisions/drop-legacy``.
"""
from __future__ import annotations

import functools
import glob
import logging
import os
import re
from typing import Any

import frontmatter

from palinode.core import git_tools
from palinode.core import parser as _parser
from palinode.core.config import config

logger = logging.getLogger("palinode.cross_refs")

# Directories that are not first-class memories to cross-link (mirrors the
# watcher ignore set + lint skip set). ``daily`` notes are excluded as both
# source and target — they are episodic and would create churn. Matched against
# EVERY directory segment of a path, not just the first — see :func:`_is_skipped`.
SKIP_DIRS: frozenset[str] = frozenset(
    {"daily", "archive", "logs", "inbox", "prompts", ".obsidian", ".git"}
)

# Generic single-word titles that would false-match common prose. A memory
# titled exactly one of these is matched only by its ref/slug, never its title.
STOPWORD_TITLES: frozenset[str] = frozenset(
    {
        "decision", "decisions", "insight", "insights", "status", "note",
        "notes", "project", "projects", "person", "people", "daily", "research",
        "todo", "summary", "overview", "readme", "inbox", "memory", "log",
    }
)


def _is_distinctive_title(title: str, min_token_len: int) -> bool:
    """A title is distinctive enough to match in prose if it is multi-word or
    reasonably long, and is not a generic single keyword."""
    t = title.strip()
    if not t or t.lower() in STOPWORD_TITLES:
        return False
    return len(t.split()) >= 2 or len(t) >= min_token_len


@functools.lru_cache(maxsize=16384)
def _candidate_pattern(candidate: str) -> re.Pattern[str]:
    """Compiled whole-token pattern for one candidate. Cached across calls: a
    registry of N memories yields up to 3N patterns, past the ``re`` module's
    own 512-entry cache, so without this every scan recompiled all of them."""
    return re.compile(r"(?<![\w-])" + re.escape(candidate) + r"(?![\w-])")


def _whole_match(candidate: str, text_lower: str) -> bool:
    """Whole-token / whole-phrase, case-insensitive match.

    Word/hyphen characters on either side disqualify a hit, so ``api`` does not
    match inside ``rapidly`` and ``drop-legacy`` matches as a unit. ``candidate``
    is already lowercased; ``text_lower`` is the lowercased body.
    """
    # A regex hit implies a plain substring hit, so the fast C substring scan
    # gates the regex — most candidates are absent from most bodies.
    if candidate not in text_lower:
        return False
    return _candidate_pattern(candidate).search(text_lower) is not None


def _is_skipped(rel_path: str) -> bool:
    """True when ANY directory segment of ``rel_path`` is in :data:`SKIP_DIRS`.

    Testing only the first segment let the store's own prompt copies at
    ``specs/prompts/*.md`` through — ``parts[0] == "specs"`` — so the auto-updater
    rewrote their frontmatter and committed it, and ``prompt sync``, which compares
    a whole-file sha256 against ``shipped-hashes.json``, then read every store
    prompt as operator-edited and refused to refresh it.

    Only *directory* segments are considered, so a memory named ``logs.md`` is
    still linkable; ``daily/2026-06-28.md`` and everything else previously
    skipped is skipped exactly as before.
    """
    return any(part in SKIP_DIRS for part in rel_path.split(os.sep)[:-1])


def path_to_ref(rel_path: str) -> str:
    """``decisions/drop-legacy.md`` → ``decisions/drop-legacy`` (OS-agnostic)."""
    stem = rel_path[:-3] if rel_path.endswith(".md") else rel_path
    return stem.replace(os.sep, "/")


#: Per-``memory_dir`` registry cache: ``{ref: ((mtime_ns, size), slug, title)}``.
#: A file's entry is reused while its stat stamp is unchanged and re-read
#: otherwise, so a rebuild costs one ``stat`` per memory plus one parse per
#: *changed* memory — instead of one parse per memory per call, which made a
#: reindex of N files O(N²) parses. Whole-dict replacement per call, so a
#: concurrent caller sees either the previous or the new snapshot.
_registry_cache: dict[str, dict[str, tuple[tuple[int, int] | None, str, str]]] = {}


def _read_title(filepath: str) -> str:
    """The memory's ``title`` (or ``name``) from its frontmatter, ``""`` when
    unreadable or unparseable — a bad file still cross-links by ref/slug."""
    try:
        with open(filepath, encoding="utf-8") as fh:
            meta, _ = _parser.parse_frontmatter(fh.read())
        return str(meta.get("title") or meta.get("name") or "").strip()
    except Exception:
        return ""


def build_registry(
    memory_dir: str, *, exclude_ref: str | None = None
) -> dict[str, dict[str, str]]:
    """Map every linkable memory's ``category/slug`` ref → ``{slug, title}``.

    Reads each memory's frontmatter (title/name). Files with any directory
    segment in :data:`SKIP_DIRS` are excluded (see :func:`_is_skipped`), so
    ``specs/prompts/compaction.md`` is never offered as a link target.
    ``exclude_ref`` (the scanned file's own ref) is omitted so a
    memory never cross-links to itself.

    O(N) ``stat`` calls per call; frontmatter is parsed only for files whose
    ``(mtime_ns, size)`` changed since the last call in this process (see
    :data:`_registry_cache`). Returns a fresh dict each time.
    """
    previous = _registry_cache.get(memory_dir, {})
    current: dict[str, tuple[tuple[int, int] | None, str, str]] = {}
    pattern = os.path.join(memory_dir, "**", "*.md")
    for filepath in glob.glob(pattern, recursive=True):
        rel = os.path.relpath(filepath, memory_dir)
        parts = rel.split(os.sep)
        if _is_skipped(rel):
            continue
        ref = path_to_ref(rel)
        slug = parts[-1][:-3] if parts[-1].endswith(".md") else parts[-1]
        try:
            st = os.stat(filepath)
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        cached = previous.get(ref)
        if stamp is not None and cached is not None and cached[0] == stamp:
            title = cached[2]
        else:
            title = _read_title(filepath)
        current[ref] = (stamp, slug, title)
    _registry_cache[memory_dir] = current
    return {
        ref: {"slug": slug, "title": title}
        for ref, (_stamp, slug, title) in current.items()
        if ref != exclude_ref
    }


def detect_refs(
    body: str,
    registry: dict[str, dict[str, str]],
    *,
    min_token_len: int = 6,
) -> list[str]:
    """Return the sorted ``category/slug`` refs mentioned in ``body``.

    For each candidate memory, matches (whole-token, case-insensitive) on its
    full path ref, its slug (only if hyphenated or ``>= min_token_len``), or its
    distinctive title. Conservative by design — see module docstring.
    """
    body_lower = body.lower()
    found: set[str] = set()
    for ref, info in registry.items():
        candidates: list[str] = [ref.lower()]
        slug = info.get("slug", "").lower()
        if slug and ("-" in slug or len(slug) >= min_token_len):
            candidates.append(slug)
        title = info.get("title", "")
        if title and _is_distinctive_title(title, min_token_len):
            candidates.append(title.lower())
        for cand in candidates:
            if _whole_match(cand, body_lower):
                found.add(ref)
                break
    return sorted(found)


def _normalize_existing(value: Any) -> list[str]:
    """Existing ``cross_refs`` frontmatter → sorted list of strings (for diffing)."""
    if isinstance(value, list):
        return sorted(str(x) for x in value)
    return []


def update_file_cross_refs(
    filepath: str, *, content: str | None = None
) -> dict[str, Any]:
    """Compute ``cross_refs`` for one file and persist them iff they changed.

    Reads the file (or uses ``content``), scans the body against every other
    memory, and rewrites + commits the file only when the computed refs differ
    from the existing frontmatter (idempotent — terminates the watcher's
    re-process-its-own-write loop). Returns
    ``{"changed": bool, "refs": list[str], "error": str | None}``.
    """
    result: dict[str, Any] = {"changed": False, "refs": [], "error": None}

    if not config.capture.cross_refs.enabled:
        return result

    memory_dir = config.memory_dir
    try:
        rel = os.path.relpath(filepath, memory_dir)
    except ValueError:
        result["error"] = "outside memory_dir"
        return result
    parts = rel.split(os.sep)
    if not parts or _is_skipped(rel) or parts[0].startswith(".."):
        return result

    try:
        post = frontmatter.load(filepath) if content is None else frontmatter.loads(content)
    except Exception as e:
        result["error"] = f"parse failed: {e}"
        return result

    self_ref = path_to_ref(rel)
    min_token_len = config.capture.cross_refs.min_token_len
    registry = build_registry(memory_dir, exclude_ref=self_ref)
    refs = detect_refs(post.content, registry, min_token_len=min_token_len)
    result["refs"] = refs

    if refs == _normalize_existing(post.metadata.get("cross_refs")):
        return result  # unchanged — no write, no commit

    if refs:
        post.metadata["cross_refs"] = refs
    else:
        post.metadata.pop("cross_refs", None)

    # Match the save path's frontmatter convention (sorted keys via the default
    # YAML handler) so the diff is just the cross_refs line, then route through
    # the mutation choke point (atomic write + single-file commit).
    new_content = frontmatter.dumps(post)
    if not new_content.endswith("\n"):
        new_content += "\n"
    try:
        git_tools.write_memory_file(filepath, new_content)
        git_tools.commit_memory_file(filepath, f"palinode: auto-update cross_refs for {rel}")
    except OSError as e:
        result["error"] = f"write failed: {e}"
        return result

    result["changed"] = True
    logger.info("cross_refs updated op=cross_refs file_path=%s count=%d", rel, len(refs))
    return result
