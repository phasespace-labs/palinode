"""Selective project-memory handoff: export side.

A handoff bundle carries the project memory a sender picked out, as one JSON
file another store can preview and import. This module decides what goes into
the bundle and describes it for the preview. It never writes to the store.

The sender has to see exactly what leaves, so every choice is reported:

- **Selection.** By default this is the project's current Decision and Insight
  records. ``paths`` replaces that default with records the sender names.
- **Default exclusions.** ``people/``, ``daily/`` and ``inbox/`` records,
  ``core: true`` records, retired records, other projects' records and other
  types are never selected unless named. Each one the walk passes over is
  listed with the reason.
- **Never exported.** ``private`` and ``restricted`` records, history siblings
  and non-memory directories are left out even when named. Exact-path reads are
  deliberately ungated (ADR-009), so this module applies the access rule itself.
- **Dependencies.** Lineage, evidence and conflict links
  (:data:`LINK_FIELDS`), plus ``sources``/``claims`` refs, are followed one hop.
  A dependency is carried only if it passes the same rules or the sender names
  it in ``include``; otherwise it's listed as excluded. A retired dependency
  stays retired. Wikilinks are navigation, not evidence, so they're listed and
  never followed.
- **Fields.** Only :data:`KEPT_FIELDS` travel. Authority-bearing fields
  (``core``, ``authority``, ``update_policy``, ``scope``, ``visibility``, …) are
  dropped and named per record, so an import can't elevate anything.

The ``digest`` hashes everything the bundle would carry. A bundle is built only
when the caller presents the digest from its preview (:func:`export_bundle`), so
what's written is what was previewed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from palinode.core import lifecycle, scope
from palinode.core.agent_directed import agent_directed_spans
from palinode.core.config import config
from palinode.core.parser import parse_frontmatter, parse_sources
from palinode.core.revalidation import normalize_ref
from palinode.core.memory_files import list_memory_files
from palinode.core.skip_dirs import is_skipped_path

FORMAT = "palinode-handoff/1"

#: What the default selection takes from the project.
DEFAULT_TYPES: tuple[str, ...] = ("Decision", "Insight")

#: Directories whose records are personal or episodic: never selected by
#: default, and carried only when the sender names them.
PERSONAL_DIRS: frozenset[str] = frozenset({"people", "daily", "inbox"})

#: Typed links followed one hop as dependencies.
LINK_FIELDS: tuple[str, ...] = (
    "supersedes", "superseded_by", "backed_by", "contradicts", "falsified_by",
)

#: Frontmatter that travels. Everything else is dropped and reported.
KEPT_FIELDS: frozenset[str] = frozenset({
    "id", "category", "type", "title", "description", "entities",
    "created_at", "last_updated", "date", "expires_at",
    "status", "lifecycle", "epistemic", "confidence",
    "sources", "claims", "backing_policy", "auditable_memory",
    *LINK_FIELDS,
})

INDEPENDENT_COPY_NOTICE = (
    "A handoff bundle is an independent copy. Correcting, archiving or "
    "deleting a record in the sending store later does not change or erase "
    "this file or anything imported from it. To pass on a correction, export "
    "again; the recipient's import shows what changed."
)

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


class HandoffError(ValueError):
    """A refused handoff request; ``status_code`` is what the API returns."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ── reading the store ───────────────────────────────────────────────────────
def _root() -> str:
    return os.path.realpath(config.memory_dir)


def _rel(ref: str) -> str:
    """A ``category/slug`` ref or relative path as a store-relative ``.md`` path."""
    r = normalize_ref(ref)
    return f"{r}.md"


def _never_exportable(rel: str) -> str | None:
    """Why ``rel`` can't leave the store whatever the sender asks, or ``None``."""
    if is_skipped_path(rel):
        return "not a memory directory"
    if rel.endswith("-history.md"):
        return "history sibling"
    return None


def _read(root: str, rel: str) -> tuple[dict[str, Any], str, str] | None:
    """``(frontmatter, body, file_sha256)`` for a record inside the store, or ``None``."""
    path = os.path.join(root, rel)
    try:
        if os.path.commonpath([root, os.path.realpath(path)]) != root:
            return None
        with open(path, "rb") as handle:
            raw = handle.read()
        text = raw.decode("utf-8")
    except (OSError, ValueError):
        return None
    meta, body = parse_frontmatter(text)
    return meta, body, hashlib.sha256(raw).hexdigest()


def _walk(root: str) -> list[str]:
    return [
        f.rel_path for f in list_memory_files(root) if _never_exportable(f.rel_path) is None
    ]


def _in_project(meta: dict[str, Any], project: str, index: dict[str, str]) -> bool:
    return any(
        scope.canonical_project(e, index) == project for e in scope.project_entities(meta)
    )


def _moment(meta: dict[str, Any]) -> datetime | None:
    return lifecycle.parse_moment(meta.get("last_updated") or meta.get("created_at"))


# ── per-record rules ────────────────────────────────────────────────────────
def _hard_refusal(rel: str, meta: dict[str, Any]) -> str | None:
    """A reason no request can override: private, restricted, not memory."""
    reason = _never_exportable(rel)
    if reason:
        return reason
    if not scope.access_allows(meta, file_path=rel):
        return "private or restricted"
    return None


def _default_exclusion(
    rel: str,
    meta: dict[str, Any],
    *,
    project: str,
    index: dict[str, str],
    types: tuple[str, ...] | None,
    now: datetime,
    retired_ok: bool = False,
) -> str | None:
    """Why the default selection passes ``rel`` over, or ``None`` if it qualifies.

    ``types=None`` skips the type rule. ``retired_ok`` lets a retired record
    qualify: a dependency travels retired, so the recipient sees the history
    without reviving it.
    """
    if rel.split(os.sep)[0] in PERSONAL_DIRS:
        return f"personal or episodic ({rel.split(os.sep)[0]}/)"
    if meta.get("core") is True:
        return "core memory"
    if not _in_project(meta, project, index):
        return "not tagged to this project"
    if types is not None and str(meta.get("type") or "") not in types:
        return f"type {meta.get('type') or 'unset'} not selected"
    if not retired_ok:
        elig = lifecycle.eligibility(meta, path=rel, now=now)
        if elig.retired:
            return f"retired ({lifecycle.retired_reason(elig)})"
    return None


def _dependency_refs(meta: dict[str, Any], body: str) -> list[tuple[str, str]]:
    """``(relation, ref)`` pairs this record points at, typed links first."""
    out: list[tuple[str, str]] = []
    for field in LINK_FIELDS:
        raw = meta.get(field)
        for ref in [raw] if isinstance(raw, str) else (raw if isinstance(raw, list) else []):
            if isinstance(ref, str) and ref.strip():
                out.append((field, ref.strip()))
    for anchor in parse_sources(meta):
        out.append(("sources", anchor["ref"]))
    claims = meta.get("claims")
    for claim in claims if isinstance(claims, list) else []:
        if isinstance(claim, dict) and isinstance(claim.get("source_id"), str):
            out.append(("claims", claim["source_id"]))
    for name in _WIKILINK_RE.findall(body or ""):
        out.append(("wikilink", name.strip()))
    seen: set[tuple[str, str]] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def _envelope(rel: str, meta: dict[str, Any], body: str, sha: str, *, role: str,
              now: datetime) -> dict[str, Any]:
    kept = {k: meta[k] for k in sorted(meta) if k in KEPT_FIELDS}
    elig = lifecycle.eligibility(meta, path=rel, now=now)
    spans = agent_directed_spans(body)
    return {
        "origin_path": rel.replace(os.sep, "/"),
        "role": role,
        "frontmatter": json.loads(json.dumps(kept, default=str)),
        "body": body,
        "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "origin_revision": sha,
        "state": elig.state,
        "retired_reason": lifecycle.retired_reason(elig) if elig.retired else None,
        "dropped_fields": sorted(k for k in meta if k not in KEPT_FIELDS),
        "agent_directed": [body[s:e] for s, e in spans],
    }


# ── the plan ────────────────────────────────────────────────────────────────
def plan_export(
    project: str,
    *,
    types: list[str] | None = None,
    paths: list[str] | None = None,
    include: list[str] | None = None,
    since: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What a bundle for ``project`` would carry, and everything it leaves out.

    Writes nothing. The returned ``records`` hold the full envelopes, so the
    preview shows the exact bytes that would leave. ``digest`` identifies that
    content for :func:`export_bundle`.
    """
    if not isinstance(project, str) or not project.strip():
        raise HandoffError("project is required")
    clock = now or datetime.now(UTC)
    root = _root()
    index = scope.alias_index()
    proj = scope.canonical_project(project, index)
    wanted_types = tuple(types) if types else DEFAULT_TYPES
    since_at = None
    if since:
        since_at = lifecycle.parse_moment(since)
        if since_at is None:
            raise HandoffError(f"since is not an ISO date: {since!r}")
    included_refs = {normalize_ref(p) for p in include or []}

    selected: dict[str, dict[str, Any]] = {}
    exclusions: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    def exclude(rel: str, reason: str, via: str | None = None) -> None:
        row = {"path": rel.replace(os.sep, "/"), "reason": reason}
        if via:
            row["via"] = via
        exclusions.append(row)

    if paths:
        for ref in paths:
            rel = _rel(ref)
            loaded = _read(root, rel)
            if loaded is None:
                raise HandoffError(f"no readable memory at {rel}", 404)
            meta, body, sha = loaded
            refusal = _hard_refusal(rel, meta)
            if refusal:
                raise HandoffError(f"{rel} can't be exported: {refusal}", 403)
            note = _default_exclusion(
                rel, meta, project=proj, index=index, types=wanted_types if types else None,
                now=clock,
            )
            if note:
                warnings.append({"path": rel, "warning": f"named explicitly; {note}"})
            selected[rel] = _envelope(rel, meta, body, sha, role="selected", now=clock)
    else:
        for rel in _walk(root):
            loaded = _read(root, rel)
            if loaded is None:
                continue
            meta, body, sha = loaded
            if not _in_project(meta, proj, index):
                continue  # other projects' records aren't listed, only skipped
            refusal = _hard_refusal(rel, meta)
            if refusal:
                exclude(rel, refusal)
                continue
            reason = _default_exclusion(
                rel, meta, project=proj, index=index, types=wanted_types, now=clock,
            )
            if reason is None and since_at is not None:
                moment = _moment(meta)
                if moment is None or moment < since_at:
                    reason = "older than since"
            if reason:
                exclude(rel, reason)
                continue
            selected[rel] = _envelope(rel, meta, body, sha, role="selected", now=clock)

    dependencies: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, str]] = []
    for rel, record in list(selected.items()):
        for relation, ref in _dependency_refs(record["frontmatter"], record["body"]):
            if relation == "wikilink":
                unresolved.append({"from": rel, "relation": relation, "ref": ref,
                                   "reason": "wikilinks are navigation and aren't followed"})
                continue
            dep = _rel(ref)
            if dep in selected or dep in dependencies:
                continue
            loaded = _read(root, dep)
            if loaded is None:
                unresolved.append({"from": rel, "relation": relation, "ref": ref,
                                   "reason": "missing from the sending store"})
                continue
            dmeta, dbody, dsha = loaded
            refusal = _hard_refusal(dep, dmeta)
            if refusal:
                exclude(dep, refusal, via=f"{relation} from {rel}")
                warnings.append({
                    "path": rel,
                    "warning": f"its {relation} names {ref}, which is {refusal}; "
                               "the record stays home but its path travels in the link",
                })
                unresolved.append({"from": rel, "relation": relation, "ref": ref,
                                   "reason": f"excluded: {refusal}"})
                continue
            reason = _default_exclusion(
                dep, dmeta, project=proj, index=index, types=None, now=clock,
                retired_ok=True,
            )
            if reason and normalize_ref(dep) in included_refs:
                warnings.append({"path": dep, "warning": f"named with include; {reason}"})
            elif reason:
                exclude(dep, reason, via=f"{relation} from {rel}")
                unresolved.append({"from": rel, "relation": relation, "ref": ref,
                                   "reason": f"excluded: {reason}"})
                continue
            dependencies[dep] = _envelope(dep, dmeta, dbody, dsha, role="dependency",
                                          now=clock)

    records = [selected[r] for r in sorted(selected)] + [
        dependencies[r] for r in sorted(dependencies)
    ]
    carried = {normalize_ref(r["origin_path"]) for r in records}
    # The walk lists what the default selection passed over; a record carried
    # as a dependency after all isn't left out.
    exclusions = [row for row in exclusions if normalize_ref(row["path"]) not in carried]
    for record in records:
        for anchor in parse_sources(record["frontmatter"]):
            if normalize_ref(anchor["ref"]) not in carried:
                warnings.append({
                    "path": record["origin_path"],
                    "warning": f"quotes {anchor['ref']}, which isn't in the bundle; "
                               "the quoted text travels anyway",
                })

    selection = {
        "project": proj,
        "types": list(wanted_types),
        "paths": sorted(_rel(p) for p in paths or []),
        "include": sorted(included_refs),
        "since": since,
    }
    content = {
        "format": FORMAT,
        "selection": selection,
        "records": records,
        "exclusions": sorted(exclusions, key=lambda r: (r["path"], r["reason"])),
        "unresolved": unresolved,
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {**content, "digest": digest, "warnings": warnings,
            "notice": INDEPENDENT_COPY_NOTICE}


def _exclusion_counts(exclusions: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in exclusions:
        reason = row["reason"]
        if reason.startswith("retired ("):
            reason = "retired"
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def export_bundle(expect_digest: str, project: str, **selection: Any) -> dict[str, Any]:
    """The bundle for a previewed selection, refused if the store moved since.

    Re-plans and compares digests: any change to a selected or dependent
    record, or to what's excluded, between preview and export is a 409, and the
    caller previews again.
    """
    if not isinstance(expect_digest, str) or not expect_digest.strip():
        raise HandoffError("expect_digest is required: preview the export first")
    plan = plan_export(project, **selection)
    if plan["digest"] != expect_digest.strip():
        raise HandoffError(
            "the store changed since the preview (digest differs); preview again", 409,
        )
    return {
        "manifest": {
            "format": FORMAT,
            "bundle_id": f"hb-{plan['digest'][:16]}",
            "digest": plan["digest"],
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "selection": plan["selection"],
            # Counts only. The preview names every path for the sender, but a
            # left-out record's path can itself be private, so it stays home.
            "exclusions": _exclusion_counts(plan["exclusions"]),
            "unresolved": plan["unresolved"],
            "warnings": plan["warnings"],
            "notice": INDEPENDENT_COPY_NOTICE,
        },
        "records": plan["records"],
    }
