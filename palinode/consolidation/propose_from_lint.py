"""Lint findings become proposed operations — the ``detect → propose → dispose`` seam.

``palinode lint`` has always been report-only: it finds orphans, stale files,
contradictions, drifting wikilinks and withdrawn backing, prints them, and the
findings die in the terminal. This module is the missing middle step. It turns
the *deterministic* half of that report into operations in the executor's own
vocabulary, each carrying the finding it came from, and hands them to the same
deterministic write paths a consolidation pass uses.

The separation is the point, and it is deliberately narrower than an LLM pass:

* **detect** — :func:`palinode.core.lint.run_lint_pass`, deterministic.
* **propose** — this module. A finding maps to an op only when the mapping needs
  no judgement. Nothing that requires *wording* (a merged sentence, a rewritten
  fact, a chosen winner between two claims) is proposed here; those stay the
  LLM proposer's job, and this module emits an advisory ``PROPOSE_*`` note
  instead — the same vocabulary :mod:`palinode.core.review` already uses.
* **dispose** — the executor, unchanged. Nothing here writes files.

Dry-run is the default on every surface. ``apply`` is opt-in, and each applied
op is stamped with an actor of ``lint`` so the git history and the audit trail
say the executor ran *lint-proposed* ops, not LLM-proposed ones.

The mapping
-----------

============================  ==============================  ==========
lint finding                  proposed op                     applicable
============================  ==============================  ==========
``stale_files`` (eligible)    ``ARCHIVE`` (whole document)    yes
``stale_files`` (excluded)    none — recorded as skipped      --
deep contradiction pair       ``PROPOSE_CONTRADICTS``         yes
``stale_backing``             ``PROPOSE_UPDATE`` (advisory)   no
``orphaned_files``            ``PROPOSE_UPDATE`` (advisory)   no
``relative_dates`` (resolved) ``UPDATE`` (fact text)          yes
``relative_dates`` (vague)    none — recorded as skipped      --
============================  ==============================  ==========

Everything else in the lint report has no honest deterministic op and is left
to the human reading the report.

Why a relative date earns an ``UPDATE``
--------------------------------------

``UPDATE`` requires replacement text, which is the thing this module otherwise
refuses to invent — but "yesterday" in a memory written on 2026-09-10 means
2026-09-09 by arithmetic, not by judgement, and the same arithmetic runs at
write time (:mod:`palinode.core.relative_dates`). The proposal reuses that
normaliser rather than re-deriving the rewrite, so the two paths cannot drift
and the refusals are identical: a phrase inside quoted text, a blockquote or a
code span is reported by lint and rewritten by neither. Vague phrases
("recently") and intervals ("last week") name no day, so they are skipped with
the reason rather than guessed at.

Why orphans are advisory
------------------------

An orphan has no entity refs and nothing points at it. Making it reachable
means choosing *which* entity it belongs to — a judgement, and a wrong one
silently mis-files the memory. The op that would fix it (``UPDATE`` with new
text) is exactly the shape this module refuses to invent, so the orphan gets a
proposal a human can act on and nothing more.

Why age-based ARCHIVE is restricted
-----------------------------------

ADR-020: a fact's eligibility for age-based retirement is a property of the
*document* it lives in, not of the fact's age. "The dog is named Rex" does not
become false at 90 days. ARCHIVE is also the only operation that removes
information from default recall, so a false positive is unrecoverable at read
time.

Two layers decide, and a skipped finding always says which one stopped it:

* **The invariant** — :func:`palinode.consolidation.retirement.is_superseded_only`,
  the same classifier the executor's retirement guard and the TTL sweep read.
  A document it calls ``superseded-only`` retires by supersession or retraction
  and never by age, so proposing an age ARCHIVE against one would only produce
  an op the executor is obliged to reject. This module does not restate that
  rule; it asks.
* **Proposer-side conservatism** — :data:`_CONSERVATIVE_DIRS`, a deliberately
  short list of classes this proposer declines to nominate even though the
  invariant leaves them age-eligible. It is a preference about what an
  unattended deterministic pass should volunteer, not a claim about what the
  executor would allow, and the recorded reason says so.

The asymmetry is what justifies the second layer: a stale memory that keeps
being reported costs a line of output, a wrongly archived one costs the memory.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any

import frontmatter

from palinode.consolidation.op_parse import op_kind
from palinode.consolidation.retirement import SUPERSEDED_ONLY, classify
from palinode.core.config import config

logger = logging.getLogger("palinode.consolidation.propose_from_lint")

#: Stamped on every proposal and carried into the git message, the history
#: sibling and the status-document log, so an applied op is attributable to the
#: deterministic linter rather than to the LLM proposer.
LINT_SOURCE = "lint"

#: Ops this path is allowed to emit at all. A deliberately tiny vocabulary:
#: everything else in the executor's dispatch needs replacement text, which is
#: judgement, which is not this module's job. ``UPDATE`` is here for exactly
#: one finding class — a relative date, whose replacement is arithmetic (see
#: the module docstring).
PROPOSABLE_OPS: frozenset[str] = frozenset({"ARCHIVE", "PROPOSE_CONTRADICTS", "UPDATE"})

#: Advisory notes, in :mod:`palinode.core.review`'s vocabulary. Never applied —
#: the ``PROPOSE_`` prefix marks a suggestion for a human, not an instruction.
ADVISORY_OPS: frozenset[str] = frozenset({"PROPOSE_UPDATE"})

#: Top-level directories this proposer declines to nominate for age-based
#: ARCHIVE *on top of* the ADR-020 invariant — a proposer choice, not the
#: invariant itself.
#:
#: ``decisions/`` is the governing regime: ADR-020's table calls it
#: "conservative", not forbidden, so the classifier leaves it age-eligible and
#: the executor would apply an ARCHIVE against it. An unattended deterministic
#: pass still should not be the thing that volunteers to retire a decision — a
#: decision governs until something supersedes it, and age is not supersession.
#: Everything the *invariant* protects (``people/``, a project's profile
#: document, ``type: PersonMemory``, ``update_policy: replace``, ``core: true``,
#: an explicit ``retirement_policy: superseded-only``) is deliberately absent
#: here: it comes from :func:`palinode.consolidation.retirement.classify`, so
#: this list and the executor guard cannot disagree about what is protected.
_CONSERVATIVE_DIRS: frozenset[str] = frozenset({"decisions"})

#: A list-item fact and its executor-addressable id — the only body line an
#: ``UPDATE`` can aim at.
_FACT_LINE_RE = re.compile(
    r"^\s*[-*]\s+(?P<text>.*?)\s*<!-- fact:(?P<id>[^\s>]+) -->\s*$"
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _memory_dir() -> str:
    return getattr(config, "memory_dir", config.palinode_dir)


def _load_metadata(rel_path: str) -> dict[str, Any]:
    """Frontmatter of a memory-dir-relative path; ``{}`` when unreadable."""
    abs_path = os.path.join(_memory_dir(), rel_path)
    try:
        return dict(frontmatter.load(abs_path).metadata)
    except Exception:  # noqa: BLE001 — an unreadable file is a lint finding of its own
        return {}


def _finding_file(item: Any) -> str | None:
    """The rel path a lint finding names (findings are ``str`` or ``{"file": …}``)."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("file")
    return None


def _archive_exclusion(rel_path: str, meta: dict[str, Any]) -> str | None:
    """Why this document must not be age-archived, or ``None`` if it may be.

    The invariant is asked first, so the recorded reason names the layer that
    stopped the proposal: ``retirement_policy: superseded-only (<signal>)`` is
    the ADR-020 classification the executor would enforce anyway, and
    ``proposer: conservative class`` is this module declining to nominate a
    document the executor would in fact accept.
    """
    policy, signal = classify(os.path.join(_memory_dir(), rel_path), meta)
    if policy == SUPERSEDED_ONLY:
        return (
            f"retirement_policy: superseded-only ({signal}) — age is not a "
            "retirement reason for this document; it retires by SUPERSEDE, "
            "RETRACT or an ARCHIVE naming superseded_by (ADR-020)"
        )
    top = rel_path.split(os.sep)[0]
    if top in _CONSERVATIVE_DIRS:
        return (
            f"proposer: conservative class ({top}/) — age-eligible under "
            "ADR-020, so the executor would apply this, but the lint proposer "
            "does not volunteer age ARCHIVEs against governing documents"
        )
    return None


def _archive_allowed_by_config() -> bool:
    """Whether the operator's consolidation policy still permits ARCHIVE.

    The lint actor honours the same ``consolidation.allowed_ops`` restriction
    the LLM proposer does: an operator who has taken ARCHIVE away from
    consolidation has taken it away from every proposer, not just the
    nondeterministic one. ``PROPOSE_CONTRADICTS`` is deliberately not gated by
    it — it is outside that vocabulary and mutates nothing but a typed link.
    """
    return "ARCHIVE" in set(config.consolidation.allowed_ops)


def _proposal(
    *,
    op: str,
    file: str,
    rationale: str,
    check: str,
    detail: Any,
    applicable: bool,
    scope: str,
    blocked_by: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One proposal: a valid executor op dict plus its provenance.

    The op-carrying keys (``op``, and whatever ``extra`` supplies) are exactly
    what the executor reads, so a proposal can be handed to
    ``apply_operations`` unchanged; ``rationale`` is read by
    :func:`palinode.consolidation.op_parse.op_reason` and by the status
    document's log renderer. The remaining keys are provenance the executor
    ignores and a reviewer needs.
    """
    proposal: dict[str, Any] = {
        "op": op,
        "file": file,
        "rationale": rationale,
        "scope": scope,
        "source": LINT_SOURCE,
        "applicable": applicable,
        "finding": {"check": check, "detail": detail},
    }
    if blocked_by:
        proposal["blocked_by"] = blocked_by
    proposal.update(extra)
    return proposal


# ─────────────────────────────────────────────────────────────────────────────
# Mappings
# ─────────────────────────────────────────────────────────────────────────────


def _stale_proposals(
    report: dict[str, Any], skipped: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``stale_files`` → whole-document ``ARCHIVE`` where the class allows it."""
    archive_allowed = _archive_allowed_by_config()
    proposals: list[dict[str, Any]] = []
    for item in report.get("stale_files", []) or []:
        rel = _finding_file(item)
        if not rel:
            continue
        days_old = item.get("days_old", "?") if isinstance(item, dict) else "?"
        exclusion = _archive_exclusion(rel, _load_metadata(rel))
        if exclusion is not None:
            skipped.append({
                "check": "stale_files",
                "file": rel,
                "reason": f"no ARCHIVE proposed: {exclusion}",
            })
            continue
        proposals.append(_proposal(
            op="ARCHIVE",
            file=rel,
            scope="document",
            rationale=(
                f"lint: status is active but the memory has not been updated in "
                f"{days_old} days — retire the document (reversible with "
                f"`palinode restore`)"
            ),
            check="stale_files",
            detail={"days_old": days_old},
            applicable=archive_allowed,
            blocked_by=None if archive_allowed else "consolidation.allowed_ops",
        ))
    return proposals


def _contradiction_proposals(
    deep: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Deep-check contradiction pairs → ``PROPOSE_CONTRADICTS`` on both sides.

    The finding is the LLM's; the op is not. Recording that two memories
    disagree is deterministic once the pair is known, picks no winner, and forks
    nothing into history — which is why it is the only contradiction op this
    path will emit. SUPERSEDE and RETRACT stay human decisions.

    Idempotent by construction: a link already present in the target's
    frontmatter is not re-proposed.
    """
    if not deep:
        return []
    from palinode.core.typed_links import parse_link_refs

    proposals: list[dict[str, Any]] = []
    for finding in deep.get("contradictions", []) or []:
        pair = (finding.get("file_a"), finding.get("file_b"))
        if not all(pair):
            continue
        explanation = finding.get("llm_explanation") or ""
        similarity = finding.get("similarity")
        for this_file, other_file in (pair, tuple(reversed(pair))):
            ref = _ref_for(other_file)
            existing = set(parse_link_refs(_load_metadata(this_file), "contradicts"))
            if ref in existing or f"{ref}.md" in existing:
                continue
            proposals.append(_proposal(
                op="PROPOSE_CONTRADICTS",
                file=this_file,
                scope="frontmatter",
                contradicts=[ref],
                rationale=(
                    f"lint: semantic contradiction with {other_file} "
                    f"(similarity {similarity}) — record the disagreement; "
                    f"picking a winner stays a human decision"
                    + (f". Explanation: {explanation}" if explanation else "")
                ),
                check="deep_contradictions",
                detail={
                    "other": other_file,
                    "similarity": similarity,
                    "llm_explanation": explanation,
                },
                applicable=True,
            ))
    return proposals


def _ref_for(rel_path: str) -> str:
    """``insights/foo.md`` → ``insights/foo`` — the typed-link ref form."""
    return rel_path[:-3] if rel_path.endswith(".md") else rel_path


def _advisory_proposals(report: dict[str, Any]) -> list[dict[str, Any]]:
    """``stale_backing`` and ``orphaned_files`` → advisory ``PROPOSE_UPDATE``.

    Word-for-word the vocabulary :mod:`palinode.core.review` already emits, so
    an operator reading a review and a lint proposal set sees one language.
    """
    proposals: list[dict[str, Any]] = []
    for item in report.get("stale_backing", []) or []:
        rel = _finding_file(item)
        if not rel:
            continue
        entries = item.get("stale_backing", []) if isinstance(item, dict) else []
        refs = ", ".join(f"{e.get('ref')} ({e.get('op')})" for e in entries)
        proposals.append(_proposal(
            op="PROPOSE_UPDATE",
            file=rel,
            scope="document",
            rationale=(
                f"lint: backing withdrawn: [{refs}] — re-verify against the "
                "retired source's history and re-save, or supersede/retract the "
                "dependent. Never auto-retracted."
            ),
            check="stale_backing",
            detail={"stale_backing": entries},
            applicable=False,
            blocked_by="advisory",
        ))
    for item in report.get("orphaned_files", []) or []:
        rel = _finding_file(item)
        if not rel:
            continue
        proposals.append(_proposal(
            op="PROPOSE_UPDATE",
            file=rel,
            scope="document",
            rationale=(
                "lint: orphaned — no entities and unreferenced. Add entity tags "
                "or wikilinks so it is reachable, or archive it. Which entity it "
                "belongs to is a judgement, so no op is proposed."
            ),
            check="orphaned_files",
            detail={},
            applicable=False,
            blocked_by="advisory",
        ))
    return proposals


def _fact_line(rel_path: str, line_number: int) -> tuple[str, str] | None:
    """``(fact id, fact text)`` for a body line, or ``None`` if it is not a fact.

    ``UPDATE`` addresses a fact by its ``<!-- fact:<id> -->`` marker, so a
    relative date in ordinary prose — a heading, a paragraph, an unmarked
    bullet — has nothing for the executor to aim at.
    """
    abs_path = os.path.join(_memory_dir(), rel_path)
    try:
        body = frontmatter.load(abs_path).content
    except Exception:  # noqa: BLE001 — unreadable is a lint finding of its own
        return None
    lines = body.splitlines()
    if not 1 <= line_number <= len(lines):
        return None
    match = _FACT_LINE_RE.match(lines[line_number - 1])
    if not match:
        return None
    return match.group("id"), match.group("text").strip()


def _relative_date_proposals(
    report: dict[str, Any], skipped: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``relative_dates`` → ``UPDATE`` where the phrase resolves to a date.

    One proposal per fact line, not per phrase: the normaliser rewrites every
    resolvable phrase on the line in one pass, and two UPDATEs against the same
    fact id would have the second one operating on text the first invalidated.
    """
    from palinode.core.relative_dates import NO_ANCHOR, UNRESOLVABLE, normalize_text

    update_allowed = "UPDATE" in set(config.consolidation.allowed_ops)
    proposals: list[dict[str, Any]] = []
    for item in report.get("relative_dates", []) or []:
        rel = _finding_file(item)
        if not rel:
            continue
        matches = item.get("matches", []) if isinstance(item, dict) else []
        by_line: dict[str, list[dict[str, Any]]] = {}
        for match in matches:
            if match.get("resolved", UNRESOLVABLE) == UNRESOLVABLE:
                skipped.append({
                    "check": "relative_dates",
                    "file": rel,
                    "reason": (
                        f"no UPDATE proposed for {match.get('expression')!r} "
                        f"(line {match.get('line')}): "
                        f"{match.get('reason') or 'it resolves to no date'}"
                    ),
                })
                continue
            by_line.setdefault(str(match.get("line")), []).append(match)

        for line_number, line_matches in by_line.items():
            anchor_text = line_matches[0].get("anchor", NO_ANCHOR)
            fact = _fact_line(rel, int(line_number))
            if fact is None:
                skipped.append({
                    "check": "relative_dates",
                    "file": rel,
                    "reason": (
                        f"no UPDATE proposed: line {line_number} carries no "
                        f"<!-- fact:… --> marker, and UPDATE addresses a fact by "
                        f"its id — rewrite it by hand or re-save the memory"
                    ),
                })
                continue
            fact_id, fact_text = fact
            anchor = date.fromisoformat(anchor_text)
            new_text, rewritten = normalize_text(fact_text, anchor)
            if not rewritten:
                skipped.append({
                    "check": "relative_dates",
                    "file": rel,
                    "reason": (
                        f"no UPDATE proposed: the phrase on line {line_number} is "
                        f"inside quoted text or code, where a rewrite would change "
                        f"what was said rather than when it was said"
                    ),
                })
                continue
            phrases = ", ".join(repr(m.get("expression")) for m in line_matches)
            proposals.append(_proposal(
                op="UPDATE",
                file=rel,
                scope="fact",
                id=fact_id,
                new_text=new_text,
                rationale=(
                    f"lint: relative date {phrases} rots — resolved against the "
                    f"memory's own anchor ({anchor_text}) and rewritten absolute, "
                    f"per PROGRAM.md § Resolve dates"
                ),
                check="relative_dates",
                detail={
                    "line": line_number,
                    "anchor": anchor_text,
                    "expressions": [m.get("expression") for m in line_matches],
                    "resolved": [m.get("resolved") for m in line_matches],
                },
                applicable=update_allowed,
                blocked_by=None if update_allowed else "consolidation.allowed_ops",
            ))
    return proposals


# ─────────────────────────────────────────────────────────────────────────────
# Propose
# ─────────────────────────────────────────────────────────────────────────────


def propose_from_lint(
    report: dict[str, Any],
    *,
    deep_contradictions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate a lint report into a proposal set. Pure — writes nothing.

    Args:
        report: the dict :func:`palinode.core.lint.run_lint_pass` returns.
        deep_contradictions: the dict
            :func:`palinode.lint.contradictions.run_deep_contradiction_check`
            returns, when the caller opted into that pass. Omitted, no
            contradiction ops are proposed.

    Returns a dict with ``proposals`` (every op, applicable or advisory),
    ``skipped`` (findings deliberately left alone, with the reason) and
    ``summary`` counts.
    """
    skipped: list[dict[str, Any]] = []
    proposals = (
        _stale_proposals(report, skipped)
        + _contradiction_proposals(deep_contradictions)
        + _relative_date_proposals(report, skipped)
        + _advisory_proposals(report)
    )
    applicable = [p for p in proposals if p["applicable"]]
    return {
        "source": LINT_SOURCE,
        "proposals": proposals,
        "skipped": skipped,
        "summary": {
            "proposed": len(proposals),
            "applicable": len(applicable),
            "advisory": len(proposals) - len(applicable),
            "skipped": len(skipped),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dispose
# ─────────────────────────────────────────────────────────────────────────────


def apply_proposals(proposals: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the applicable proposals through the existing deterministic writers.

    No new write path: a whole-document ``ARCHIVE`` goes through
    :func:`palinode.consolidation.archive.archive_memory` (the sanctioned
    on-demand retirement — frontmatter flip, the executor's own history
    sibling, the index status push, one commit) and a ``PROPOSE_CONTRADICTS``
    goes through the executor via
    :func:`palinode.consolidation.runner.apply_proposed_operations`. Both are
    stamped with an actor of ``lint``.

    Applies nothing that :func:`propose_from_lint` did not mark ``applicable``,
    re-checking rather than trusting the caller's copy of the proposal set.
    """
    from palinode.consolidation import runner
    from palinode.consolidation.archive import archive_memory

    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    stats: dict[str, int] = {}

    for proposal in proposals:
        if not isinstance(proposal, dict) or not proposal.get("applicable"):
            continue
        kind = op_kind(proposal)
        rel = proposal.get("file")
        if kind not in PROPOSABLE_OPS or not rel:
            logger.warning("lint proposal skipped (not applicable): %r", proposal)
            continue
        try:
            if kind == "ARCHIVE":
                result = archive_memory(
                    rel,
                    reason=proposal.get("rationale"),
                    actor=LINT_SOURCE,
                )
                applied.append({"op": kind, "file": rel, "result": result})
                stats["archived"] = stats.get("archived", 0) + (
                    1 if result.get("status") != "already_archived" else 0
                )
            else:
                target = os.path.join(_memory_dir(), rel)
                op_stats = runner.apply_proposed_operations(
                    target, [proposal], source=LINT_SOURCE
                )
                applied.append({"op": kind, "file": rel, "result": op_stats})
                for key, value in op_stats.items():
                    stats[key] = stats.get(key, 0) + value
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the pass
            logger.error("lint-proposed %s failed on %s: %s", kind, rel, exc)
            failed.append({"op": kind, "file": rel, "error": str(exc)})

    return {
        "source": LINT_SOURCE,
        "applied": applied,
        "failed": failed,
        "stats": stats,
    }


def attach_proposals(
    report: dict[str, Any],
    *,
    apply: bool = False,
    deep_contradictions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a ``proposals`` block to a lint report; optionally apply it.

    The report itself is returned unchanged apart from the added key, so every
    existing consumer of the lint payload (the quality queues, the CLI's text
    renderer) keeps working. ``apply=False`` — the default on every surface —
    writes nothing and commits nothing.
    """
    proposal_set = propose_from_lint(report, deep_contradictions=deep_contradictions)
    proposal_set["dry_run"] = not apply
    if apply:
        proposal_set["applied"] = apply_proposals(proposal_set["proposals"])
    report = dict(report)
    report["proposals"] = proposal_set
    return report
