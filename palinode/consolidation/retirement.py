"""Document-relative retirement policy (ADR-020).

A fact's eligibility for **age-based** retirement is a property of the document
it lives in, not of the fact's age. "The dog is named Rex" does not become false
at 60 days; a milestone log entry does go stale. Two regimes:

``age-eligible``
    Episodic documents — daily notes, insights, research, status documents,
    inbox items. The TTL sweep may archive them when their ``expires_at``
    passes, and consolidation may propose a staleness ARCHIVE against them.
    This is the default, and today's behaviour for every document.

``superseded-only``
    Identity / profile documents — a person, a long-lived project's profile, a
    living (``update_policy: replace``) document, anything injected at every
    session start (``core: true``). Retirement is still available, but only
    with a *reason other than age*: SUPERSEDE (the fact changed), RETRACT (the
    fact was never true), an on-demand ``archive_memory`` with an explicit
    reason, or ``forget``. Age alone never retires them.

The classification is deterministic and reads only the document's own path and
frontmatter, so the TTL sweep and the executor cannot disagree about what a
document is. A document may declare its own regime with a
``retirement_policy:`` frontmatter field, in either direction — that
declaration wins over every inferred signal.

This module decides *what a document is*. What follows from that lives with the
caller: :mod:`palinode.consolidation.ttl` skips superseded-only documents in the
expiry sweep, and :func:`palinode.consolidation.executor.apply_operations`
refuses an ARCHIVE op against one unless the op names a successor.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger("palinode.consolidation.retirement")

#: Frontmatter field a document uses to declare its own regime.
POLICY_FIELD = "retirement_policy"

#: Age may retire this document (TTL sweep, staleness ARCHIVE). The default.
AGE_ELIGIBLE = "age-eligible"

#: Only a stated supersession/retraction may retire this document — never age.
SUPERSEDED_ONLY = "superseded-only"

VALID_POLICIES = (AGE_ELIGIBLE, SUPERSEDED_ONLY)

#: Top-level memory directories whose ``.md`` files are identity documents.
_IDENTITY_DIRS = frozenset({"people"})

#: Top-level directories holding *profile* documents alongside episodic
#: siblings: ``projects/<slug>.md`` is the project's identity (PROGRAM.md's
#: "What This Is / People / Architecture" sections), while
#: ``projects/<slug>-status.md`` is the weekly status layer, which ages.
_PROFILE_DIRS = frozenset({"projects"})

_STATUS_SUFFIX = "-status.md"


def _memory_relpath(path: str | os.PathLike[str]) -> str:
    """Return ``path`` relative to the memory dir, in posix form.

    Falls back to the path as given when it is outside (or unresolvable
    against) the memory dir — classification then reads whatever directory
    structure the path itself carries, which is what test fixtures and
    absolute paths outside the store need.
    """
    raw = os.fspath(path)
    try:
        from palinode.core.config import config

        rel = os.path.relpath(os.path.abspath(raw), os.path.abspath(config.memory_dir))
    except (OSError, ValueError, AttributeError):
        return raw.replace(os.sep, "/")
    if rel.startswith(".." + os.sep) or rel == "..":
        return raw.replace(os.sep, "/")
    return rel.replace(os.sep, "/")


def _declared(frontmatter: Mapping[str, Any]) -> str | None:
    """Return an explicit, valid ``retirement_policy`` declaration, or ``None``."""
    raw = frontmatter.get(POLICY_FIELD)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in VALID_POLICIES:
        return value
    logger.warning(
        "retirement: unknown %s value %r (expected one of %s) — falling back "
        "to the inferred regime",
        POLICY_FIELD, raw, ", ".join(VALID_POLICIES),
    )
    return None


def _is_true(value: Any) -> bool:
    """YAML-ish truth: ``True`` or the string ``"true"``, nothing looser."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def _frontmatter_signal(frontmatter: Mapping[str, Any]) -> str | None:
    """Name the frontmatter field marking this as identity/profile, if any."""
    if frontmatter.get("update_policy") == "replace":
        # A living document holds one current state; ADR-015's executor guard
        # already refuses to fork it into history. Age must not do it either.
        return "update_policy:replace"
    if _is_true(frontmatter.get("core")):
        # Injected at every session start — its content is the working set,
        # not a log. Expiry stops it *acting*; it does not retire it.
        return "core:true"
    if str(frontmatter.get("type", "")).strip().lower() == "personmemory":
        return "type:PersonMemory"
    if str(frontmatter.get("category", "")).strip().lower() == "person":
        return "category:person"
    return None


def _path_signal(rel_path: str) -> str | None:
    """Name the path rule marking this as identity/profile, if any."""
    parts = [p for p in rel_path.split("/") if p and p != "."]
    if len(parts) < 2:
        return None
    top, name = parts[0], parts[-1]
    if not name.endswith(".md"):
        return None
    if top in _IDENTITY_DIRS:
        return f"path:{top}/"
    if top in _PROFILE_DIRS and not name.endswith(_STATUS_SUFFIX):
        return f"path:{top}/ profile document"
    return None


def classify(
    path: str | os.PathLike[str],
    frontmatter: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Classify a document's retirement regime.

    Returns ``(policy, signal)`` where ``policy`` is one of
    :data:`AGE_ELIGIBLE` / :data:`SUPERSEDED_ONLY` and ``signal`` names the
    rule that decided it — for log lines that have to say *why* a document is
    protected, not merely that it is.

    Precedence: an explicit ``retirement_policy`` declaration, then the
    frontmatter class signals, then the path. Anything unrecognised is
    ``age-eligible`` — the pre-ADR-020 default, so an unclassifiable document
    behaves exactly as it did before.
    """
    fm: Mapping[str, Any] = frontmatter if isinstance(frontmatter, Mapping) else {}

    declared = _declared(fm)
    if declared is not None:
        return declared, f"declared:{POLICY_FIELD}"

    signal = _frontmatter_signal(fm) or _path_signal(_memory_relpath(path))
    if signal is not None:
        return SUPERSEDED_ONLY, signal
    return AGE_ELIGIBLE, "default"


def retirement_policy(
    path: str | os.PathLike[str],
    frontmatter: Mapping[str, Any] | None = None,
) -> str:
    """Return this document's retirement regime (see :func:`classify`)."""
    return classify(path, frontmatter)[0]


def is_superseded_only(
    path: str | os.PathLike[str],
    frontmatter: Mapping[str, Any] | None = None,
) -> bool:
    """True iff age alone may never retire this document."""
    return retirement_policy(path, frontmatter) == SUPERSEDED_ONLY
