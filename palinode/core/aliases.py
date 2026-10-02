"""Human-curated entity aliases, resolved at QUERY time.

A subject written several ways becomes several nodes in the entity graph, and
every lookup then returns a plausible, non-empty, incomplete answer — under-recall
that never announces itself, because both halves look like success.

This is the resolution half. The detection half lives in `core.lint`
(`check_entity_aliases`), which reports candidates and never merges.

## Why query time, and not a migration

Because the failure mode of getting it wrong is asymmetric. A short form and a
longer form MAY BE DIFFERENT PEOPLE — two colleagues can share a given name. A
wrong join performed by rewriting files is unrecoverable from the merged data; a
wrong join performed at query time is undone by deleting one line.

So the files on disk keep their original refs, always. This layer only widens
what a lookup *matches*. Everything about it is additive and reversible:

* No mapping file  -> exact-match behaviour, unchanged.
* Empty mapping    -> exact-match behaviour, unchanged.
* Bad mapping      -> delete the entry; nothing to un-merge.

## The file

`<PALINODE_DIR>/entity-aliases.yaml`, git-versioned alongside the memories it
describes:

```yaml
# Each key is the canonical ref; the list is the OTHER spellings of that subject.
aliases:
  person/ada-lovelace:
    - person/ada
    - person/adalovelace
  project/analytical-engine:
    - project/AnalyticalEngine
```

Curated by a human, deliberately. `palinode lint` surfaces candidates ranked by
confidence; a human decides which are actually the same subject and writes them
here. Nothing populates this file automatically, and nothing should.

Lookup is symmetric: with the entry above, asking for `person/ada`,
`person/ada-lovelace` or `person/adalovelace` all return the same union. Which
spelling you happen to have is not something a caller should have to know.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import yaml

from palinode.core.config import config

logger = logging.getLogger("palinode.aliases")

ALIAS_FILENAME = "entity-aliases.yaml"

# (mtime_ns, size, inode) of the file the cache was built from — cheap staleness
# check so a hand-edit is picked up without a restart, without stat-ing on every
# lookup path more than once. The inode is what catches an edit landing in the
# same timestamp tick with the same size: `palinode aliases` writes by atomic
# replace, which always gives the file a new inode, so another process reading
# the map sees the change on its next lookup.
_cache: dict[str, frozenset[str]] | None = None
_cache_stamp: tuple[int, int, int] | None = None
#: member ref -> the canonical ref (the file's key) of its group, built with
#: ``_cache``. Lookup stays symmetric; this only answers "which spelling does
#: the file call canonical", for callers that must report one name.
_canonical: dict[str, str] = {}


def alias_file_path() -> str:
    base = getattr(config, "memory_dir", None) or config.palinode_dir
    return os.path.join(base, ALIAS_FILENAME)


def _parse(raw: Any) -> dict[str, frozenset[str]]:
    """Build ref -> equivalence-class from the file's `aliases:` mapping.

    Every member of a group maps to the SAME frozenset containing all members, so
    resolution is symmetric and needs no canonical-vs-alias distinction at lookup
    time. Malformed entries are skipped with a warning rather than raising: a
    typo in a curated data file must not take down entity lookup.
    """
    out: dict[str, frozenset[str]] = {}
    if not isinstance(raw, dict):
        return out
    groups = raw.get("aliases")
    if not isinstance(groups, dict):
        if groups is not None:
            logger.warning("entity-aliases: `aliases` is not a mapping — ignoring")
        return out

    for canonical, others in groups.items():
        if not isinstance(canonical, str) or not canonical.strip():
            logger.warning("entity-aliases: skipping a non-string canonical ref")
            continue
        if isinstance(others, str):
            others = [others]
        if not isinstance(others, list):
            logger.warning("entity-aliases: %r does not map to a list — skipping", canonical)
            continue
        members = {canonical.strip()}
        for other in others:
            if isinstance(other, str) and other.strip():
                members.add(other.strip())
            else:
                logger.warning("entity-aliases: skipping a non-string alias under %r", canonical)
        if len(members) < 2:
            continue  # a group of one aliases nothing
        group = frozenset(members)
        for member in members:
            if member in out and out[member] != group:
                # Two groups naming the same ref is a curation mistake with a real
                # consequence — it would make resolution order-dependent. Merge
                # them and say so, rather than silently picking one.
                merged = frozenset(out[member] | group)
                logger.warning(
                    "entity-aliases: %r appears in two groups — merging them", member
                )
                for m in merged:
                    out[m] = merged
                group = merged
            else:
                out[member] = group
    return out


def _canonicals(raw: Any, groups: dict[str, frozenset[str]]) -> dict[str, str]:
    """member -> canonical ref: the first key in the file whose group holds it."""
    out: dict[str, str] = {}
    keys = raw.get("aliases") if isinstance(raw, dict) else None
    if not isinstance(keys, dict):
        return out
    for canonical in keys:
        if not isinstance(canonical, str):
            continue
        for member in groups.get(canonical.strip(), ()):
            out.setdefault(member, canonical.strip())
    return out


def load_alias_map(*, force: bool = False) -> dict[str, frozenset[str]]:
    """Load (and cache) the alias map. Absent file -> empty map."""
    global _cache, _cache_stamp, _canonical
    path = alias_file_path()
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        _cache, _cache_stamp, _canonical = {}, None, {}
        return {}

    if not force and _cache is not None and _cache_stamp == stamp:
        return _cache

    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        # Unreadable/invalid: fall back to exact matching rather than failing the
        # query. The store is still correct — just not widened.
        logger.warning("entity-aliases: could not read %s (%s) — ignoring", path, exc)
        _cache, _cache_stamp, _canonical = {}, stamp, {}
        return {}

    _cache, _cache_stamp = _parse(raw), stamp
    _canonical = _canonicals(raw, _cache)
    if _cache:
        logger.info("entity-aliases: %d ref(s) in %d group(s)",
                    len(_cache), len(set(_cache.values())))
    return _cache


def resolve(entity_ref: str) -> list[str]:
    """Every ref that should be treated as `entity_ref`, itself included.

    Returns `[entity_ref]` when no mapping applies — so callers can use this
    unconditionally and get exact-match behaviour by default.
    """
    if not entity_ref:
        return []
    group = load_alias_map().get(entity_ref)
    if not group:
        return [entity_ref]
    # Deterministic order, with the requested ref first so callers that show the
    # query back to a user see what was asked for.
    rest = sorted(r for r in group if r != entity_ref)
    return [entity_ref, *rest]


def canonical_ref(entity_ref: str) -> str:
    """The file's canonical spelling for ``entity_ref``'s group, else the ref itself."""
    load_alias_map()
    return _canonical.get(entity_ref, entity_ref)


def reset_cache() -> None:
    """Drop the cached map (tests, and after an intentional edit)."""
    global _cache, _cache_stamp, _canonical
    _cache, _cache_stamp, _canonical = None, None, {}
