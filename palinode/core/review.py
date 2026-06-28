"""Periodic project-memory review — advisory audit + proposed ops (#366).

A first-class "step back and review the whole project's memory" pass. It
COMPOSES existing deterministic health primitives (the `lint` signals) scoped to
a project, and translates the findings into PROPOSED corrective operations in the
executor's vocabulary (KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE/RETRACT) — **without
applying any of them**. Like the executor's `PROPOSE_CONTRADICTS` op (#533), the
output is advice for a human/agent decision gate, never an auto-mutation.

Design (issue #366):
  - **Advisory / read-only.** `run_review` writes nothing. It only reports.
  - **Deterministic + offline.** It composes the `lint` pass (orphans, stale
    files, stale open-questions #72, open contradictions #533, missing
    descriptions, wiki drift) — none of which need the embedder — so it is safe
    to run on a nightly/weekly cron without Ollama. Embedding-based near-duplicate
    detection stays the explicit `dedup_suggest` call (surfaced as a hint in the
    report), not folded into the offline pass.
  - **Project-scoped.** Given a project, findings are filtered to memories tagged
    with the `project/<slug>` entity (read from frontmatter — no index/DB
    dependency). With no project, it reviews the whole store.
"""
from __future__ import annotations

import glob
import os
from typing import Any

import frontmatter

from palinode.core.config import config
from palinode.core.lint import run_lint_pass

# Directories that are not first-class project memories (mirrors lint/cross_refs).
_SKIP_DIRS: frozenset[str] = frozenset(
    {"daily", "archive", "logs", "inbox", "prompts", ".obsidian", ".git"}
)


def _normalize_project_ref(project: str | None) -> str | None:
    """``"palinode"`` → ``"project/palinode"``; an already-typed ref is kept."""
    if not project:
        return None
    return project if "/" in project else f"project/{project}"


def _scope_files(project_ref: str | None) -> set[str] | None:
    """Rel paths of memories tagged with ``project_ref`` (None ⇒ no filter).

    Reads frontmatter ``entities`` directly from disk so scoping does not depend
    on the index/DB being populated.
    """
    if project_ref is None:
        return None
    base = getattr(config, "memory_dir", config.palinode_dir)
    scope: set[str] = set()
    for filepath in glob.glob(os.path.join(base, "**", "*.md"), recursive=True):
        rel = os.path.relpath(filepath, base)
        if rel.split(os.sep)[0] in _SKIP_DIRS:
            continue
        try:
            entities = frontmatter.load(filepath).metadata.get("entities", [])
        except Exception:
            continue
        if isinstance(entities, list) and project_ref in [str(e) for e in entities]:
            scope.add(rel)
    return scope


def _finding_file(item: Any) -> str | None:
    """Extract the rel path from a lint finding (str or ``{"file": ...}``)."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("file")
    return None


def _filter(items: list[Any], scope: set[str] | None) -> list[Any]:
    """Keep findings whose file is in scope (or all, when scope is None)."""
    if scope is None:
        return list(items)
    return [it for it in items if _finding_file(it) in scope]


def _propose_ops(findings: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Translate scoped findings into PROPOSED ops (advisory; never applied).

    The op vocabulary mirrors the deterministic executor; the ``PROPOSE_`` prefix
    marks each as a suggestion for a human/agent to act on, not an instruction.
    """
    ops: list[dict[str, Any]] = []
    for it in findings.get("stale", []):
        f = _finding_file(it)
        ops.append({
            "op": "PROPOSE_ARCHIVE", "file": f,
            "reason": f"Active but not updated in {it.get('days_old', '?')} days — "
                      "archive if no longer current, or refresh last_updated.",
        })
    for it in findings.get("open_questions", []):
        f = _finding_file(it)
        ops.append({
            "op": "PROPOSE_UPDATE", "file": f,
            "reason": f"Open question unresolved for {it.get('days_old', '?')} days — "
                      "resolve into a fact/inference, or supersede it.",
        })
    for it in findings.get("contradictions", []):
        f = _finding_file(it)
        refs = ", ".join(it.get("contradicts", [])) if isinstance(it, dict) else ""
        ops.append({
            "op": "PROPOSE_SUPERSEDE", "file": f,
            "reason": f"Unresolved contradiction with [{refs}] — pick a winner "
                      "(supersede) or keep both intentionally. Never auto-resolved.",
        })
    for it in findings.get("orphaned", []):
        f = _finding_file(it)
        ops.append({
            "op": "PROPOSE_UPDATE", "file": f,
            "reason": "Orphaned — no entities and unreferenced. Add entity tags or "
                      "wikilinks so it is reachable, or archive it.",
        })
    return ops


def run_review(project: str | None = None) -> dict[str, Any]:
    """Advisory project-memory review.

    Args:
        project: project slug (``"palinode"``) or typed ref (``"project/palinode"``).
            When omitted, reviews the whole store.

    Returns a read-only report:
        ``project``           — the normalized ref reviewed (or None for whole store)
        ``scope_file_count``  — memories in scope
        ``findings``          — scoped health findings (stale / open_questions /
                                contradictions / orphaned / missing_descriptions /
                                wiki_drift)
        ``proposed_ops``      — advisory PROPOSE_* ops (never applied)
        ``summary``           — counts
        ``hints``             — complementary explicit passes (e.g. dedup_suggest)
    """
    project_ref = _normalize_project_ref(project)
    scope = _scope_files(project_ref)
    lint = run_lint_pass()

    findings: dict[str, list[Any]] = {
        "stale": _filter(lint.get("stale_files", []), scope),
        "open_questions": _filter(lint.get("stale_open_questions", []), scope),
        "contradictions": _filter(lint.get("open_contradictions", []), scope),
        "orphaned": _filter(lint.get("orphaned_files", []), scope),
        "missing_descriptions": _filter(lint.get("missing_descriptions", []), scope),
        "wiki_drift": _filter(lint.get("wiki_drift", []), scope),
    }
    proposed_ops = _propose_ops(findings)

    scope_file_count = len(scope) if scope is not None else lint.get("total_files", 0)
    finding_count = sum(len(v) for v in findings.values())

    return {
        "project": project_ref,
        "scope_file_count": scope_file_count,
        "findings": findings,
        "proposed_ops": proposed_ops,
        "summary": {
            "scope_file_count": scope_file_count,
            "finding_count": finding_count,
            "proposed_op_count": len(proposed_ops),
        },
        # Embedding-based passes are explicit (need the embedder) — not folded
        # into this offline review. Surface them so the operator knows to run them.
        "hints": [
            "Near-duplicate detection: run `palinode dedup-suggest` per draft "
            "(embedding-based; not included in this offline review).",
            "Topic-coverage gaps: run `palinode topic-coverage <phrase>`.",
        ],
    }
