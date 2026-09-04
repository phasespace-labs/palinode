from __future__ import annotations

import os
import glob
import re
from datetime import datetime, timezone
from typing import Any

import frontmatter as _frontmatter

from palinode.core.config import config
from palinode.core import parser

# Marker written by Deliverable C (palinode_save auto-footer plumbing).
# Wikilinks that appear under this marker count as satisfying the entity
# requirement — the auto-footer is a derived view of ``entities:`` and
# deliberately links every frontmatter entity that has no inline body link.
_AUTO_FOOTER_MARKER = "<!-- palinode-auto-footer -->"

_RELATIVE_DATE_NUMBER = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
)
_RELATIVE_DATE_RE = re.compile(
    rf"(?<!\w)(?:"
    rf"{_RELATIVE_DATE_NUMBER} (?:days|weeks|months|years) ago|"
    r"last (?:week|month|year)|"
    r"next (?:week|month|year)|"
    r"this (?:week|month)|"
    r"right now|these days|"
    r"yesterday|today|tomorrow|recently|lately|currently"
    r")(?!\w)",
    re.IGNORECASE,
)

def _alias_key(name: str) -> str:
    """Separator-and-case-insensitive form of an entity ref's name part.

    ``alpha-bravo``, ``Alpha_Bravo`` and ``alphabravo`` all reduce to
    ``alphabravo``.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _is_token_prefix(short: str, long: str) -> bool:
    """True when `short` is `long` truncated at a token boundary.

    ``alpha`` vs ``alpha-bravo`` -> True (the longer adds a whole token).
    ``alpha`` vs ``alphabravo``  -> False (mid-token; a different name entirely).

    The token-boundary requirement is what keeps this from flagging every name
    that merely starts with the same letters.
    """
    if short == long or not long.startswith(short):
        return False
    return long[len(short)] in "-_/. "


def _independent_identity(name: str, category: str, names_by_category: dict[str, set[str]]) -> list[str]:
    """Other categories that carry this same name.

    A ref the store references under SEVERAL categories has an identity of its
    own — `project/x`, `insight/x` and `decision/x` all pointing at the same
    subject means "x" is an established thing, not a stray spelling of something
    else. That distinction is what separates a genuine alias split from two
    deliberately different entries.

    Validated against a live store (2026-07-24) on eight hand-labelled prefix
    pairs, separating all eight. Eight pairs from one store is a real signal, not
    a proof — which is why this DEMOTES confidence rather than suppressing the
    finding.

    It is also INCOMPLETE on its own, which is why it is no longer the only
    signal: a sibling repo (`project/alpha-os` beside `project/alpha`) lives
    under exactly one category, so it has nothing for this to catch. See
    `_is_stray_short_form` and `_NAME_CATEGORIES` for the other two.
    """
    return sorted(c for c in names_by_category.get(name, set()) if c != category)


# Categories whose members are PEOPLE. The distinction earns its place because
# the two populations name themselves by opposite conventions:
#
#   * A person's longer form is nearly always the same person written out in
#     full — `ada` -> `ada-lovelace`. Adding a token is how a name completes.
#   * A project's, model's or host's longer form is nearly always a DIFFERENT
#     artifact in the same family — a repo beside its `-os` sibling, a model
#     beside its quantized variant, `m9` beside `m9.1`. Adding a token is how a
#     family branches.
#
# So a bare prefix match means opposite things in the two populations, and
# outside this set it is not on its own evidence of anything. Extend this set
# only for categories that name individuals the way `person` does.
_NAME_CATEGORIES = frozenset({"person", "people"})

# At or below this many files, a ref is a straggler rather than a subject.
_STRAGGLER_MAX_FILES = 2

# ...and the other side has to dwarf it for the asymmetry to mean anything: one
# file beside one file is two equally thin refs, not a stray beside an anchor.
_ESTABLISHED_RATIO = 3


def _is_stray_short_form(short_files: int, long_files: int) -> bool:
    """True when the SHORT form looks like a slip of the pen, not a subject.

    The direction is the whole point, and it is the signal that survived contact
    with a 700-ref store:

    * A rare SHORT form beside an established long one (`project/alpha` in 1
      file, `project/alpha-mcp` in 17) is somebody abbreviating a name the store
      already owns — a one-line merge.
    * A rare LONG form beside an established short one (`project/alpha-trial` in
      1 file, `project/alpha` in 72) is the opposite: adding a qualifier is
      precisely how a new, distinct artifact enters the store.

    Reading the asymmetry without its direction promotes the second case as
    eagerly as the first, which is what turned a repo family into a
    high-confidence merge candidate.
    """
    return (
        short_files <= _STRAGGLER_MAX_FILES
        and long_files >= short_files * _ESTABLISHED_RATIO
    )


def check_entity_aliases(
    entity_references: dict[str, int],
) -> list[dict[str, Any]]:
    """Flag entity refs that look like aliases of one another.

    The problem: refs are free text written per save, with no canonicalization.
    The same subject ends up as several nodes, and every lookup returns a
    plausible, non-empty, INCOMPLETE result — under-recall presenting as success.
    Nothing in the output says anything is missing, so it never announces itself.

    **This detects and reports. It never merges, and must never learn to.** A
    short form and a longer form MAY BE DIFFERENT PEOPLE — two colleagues can
    share a given name. A wrong join is unrecoverable from the merged data,
    whereas a split is merely invisible until inspected, so the ordering has to
    stay detect -> propose -> human confirms -> alias map. This function is the
    first step only; it hands the operator a question, not an answer.

    Two deliberately HIGH-PRECISION signals, both matching the shapes actually
    observed in a live store:

    * ``separator`` — identical once case and separators are stripped
      (``a-b`` vs ``ab``). Effectively always the same subject.
    * ``prefix`` — one ref is the other truncated at a token boundary
      (``a`` vs ``a-b``). The common short-form/full-form split.

    Free-form edit distance is deliberately NOT used. It would flag every pair of
    similar-looking names, and a check an operator learns to ignore is worse than
    no check. Precision matters more than recall here because the output is a
    prompt for human judgement.

    Only refs in the SAME category are compared: ``person/alpha`` and
    ``project/alpha`` are different namespaces, not aliases.

    ``confidence`` is the field an operator sorts and acts on, so a prose
    disclaimer in ``detail`` does not pay for a wrong ``high``. ``separator``
    matches are always high; a ``prefix`` match has to EARN it, because on its
    own it is equally the shape of one subject spelled twice and of two siblings
    in a family. It is high only when the short form is a stray beside an
    established long one (`_is_stray_short_form`) or the category names people
    (`_NAME_CATEGORIES`), and low otherwise — including the case that used to
    reach high by default, a longer form seen nowhere else.

    Returns one entry per candidate cluster, each carrying every member ref with
    its file count so the operator can see the shape of the split (a near-even
    split reads very differently from a long tail).
    """
    by_category: dict[str, list[tuple[str, str, int]]] = {}
    for ref, count in entity_references.items():
        category, _, name = ref.partition("/")
        if not name:
            continue  # not a category/name ref — nothing to compare within
        by_category.setdefault(category, []).append((ref, name, count))

    # Which categories each name appears under — the independence signal below.
    names_by_category: dict[str, set[str]] = {}
    for cat, members in by_category.items():
        for _, name, _ in members:
            names_by_category.setdefault(name.lower(), set()).add(cat)

    clusters: list[dict[str, Any]] = []

    for category, members in sorted(by_category.items()):
        # Group by the separator-insensitive key first: everything in one bucket
        # is the same subject spelled differently.
        buckets: dict[str, list[tuple[str, str, int]]] = {}
        for ref, name, count in members:
            buckets.setdefault(_alias_key(name), []).append((ref, name, count))

        for _key, bucket in sorted(buckets.items()):
            if len(bucket) > 1:
                clusters.append({
                    "kind": "separator",
                    "confidence": "high",
                    "category": category,
                    "refs": [
                        {"ref": r, "files": c}
                        for r, _, c in sorted(bucket, key=lambda b: -b[2])
                    ],
                    "detail": (
                        f"{len(bucket)} refs in {category!r} are identical once case and "
                        f"separators are ignored — almost certainly one subject"
                    ),
                })

        # Then prefix containment (short form vs full form).
        #
        # Compared on the ORIGINAL names, not the separator-stripped keys: the
        # key for `alpha-bravo` is `alphabravo`, which no longer contains the
        # boundary `_is_token_prefix` looks for, so running this on keys silently
        # matches nothing. Buckets are still used to avoid re-reporting refs that
        # the separator pass already grouped.
        reported: set[frozenset[str]] = set()
        ordered = sorted(members, key=lambda m: len(m[1]))
        for i, (short_ref, short_name, _) in enumerate(ordered):
            for long_ref, long_name, _ in ordered[i + 1:]:
                if not _is_token_prefix(short_name.lower(), long_name.lower()):
                    continue
                pair = frozenset(
                    {_alias_key(short_name), _alias_key(long_name)}
                )
                if pair in reported:
                    continue
                reported.add(pair)
                short_bucket = buckets[_alias_key(short_name)]
                long_bucket = buckets[_alias_key(long_name)]
                group = short_bucket + long_bucket

                # A prefix match on its own says almost nothing: it is equally
                # the shape of one subject spelled two ways and of two siblings
                # in a family. So `high` has to be EARNED, by one of two
                # positive signals, and everything else is demoted.
                short_elsewhere = _independent_identity(
                    short_name.lower(), category, names_by_category
                )
                elsewhere = _independent_identity(
                    long_name.lower(), category, names_by_category
                )
                stray_short = not short_elsewhere and _is_stray_short_form(
                    sum(c for _, _, c in short_bucket),
                    sum(c for _, _, c in long_bucket),
                )

                if stray_short:
                    # Signal 1 — a thin short form with no identity of its own,
                    # beside an anchor. (Observed: a 1-file `x` beside an
                    # established `x-mcp` was demoted purely because `x-mcp` is
                    # established, hiding a real one-line merge.) Deliberately
                    # ahead of the cross-category demotion: an established
                    # longer form is what makes the short one look like a slip.
                    confidence = "high"
                    detail = (
                        f"{short_ref!r} is referenced far less than {long_ref!r} and "
                        f"nowhere outside {category!r} — the shape of a stray short "
                        f"form of an established name, i.e. a one-line merge. Still a "
                        f"question: a thin ref can also be a subject nobody wrote up yet."
                    )
                elif elsewhere:
                    confidence = "low"
                    detail = (
                        f"{long_ref!r} is also referenced as "
                        + ", ".join(f"{c}/{long_name}" for c in elsewhere)
                        + " — the store already treats it as its own subject, so this is "
                        "more likely two distinct entries than one split. Demoted, not "
                        "hidden: check it, but expect the answer to be 'leave them'."
                    )
                elif category in _NAME_CATEGORIES:
                    # Signal 2 — in a category that names people, a longer form
                    # seen nowhere else is the ordinary short-form/full-form
                    # split, whatever the file counts look like on either side.
                    confidence = "high"
                    detail = (
                        f"a short form and a longer form of a name coexist in "
                        f"{category!r} and the longer one appears nowhere else — the "
                        f"common short-form/full-form split. Still a question: two "
                        f"people can share a given name."
                    )
                else:
                    confidence = "low"
                    detail = (
                        f"{long_ref!r} adds a token to {short_ref!r} and both are in "
                        f"real use in {category!r} — outside a category that names "
                        f"people, that is how a FAMILY branches (a sibling repo, a "
                        f"model variant, a later milestone), not how one subject gets "
                        f"spelled twice. Demoted, not hidden: merge only if you already "
                        f"know they are the same thing."
                    )
                clusters.append({
                    "kind": "prefix",
                    "confidence": confidence,
                    "category": category,
                    "refs": [
                        {"ref": r, "files": c}
                        for r, _, c in sorted(group, key=lambda b: -b[2])
                    ],
                    "detail": detail,
                })

    # High-confidence first: the operator should meet the near-certain merges
    # before the ones the store says are probably distinct.
    clusters.sort(key=lambda c: (c["confidence"] != "high", c["kind"], c["category"]))
    return clusters


def check_wiki_drift(
    metadata: dict[str, Any],
    body: str,
) -> list[dict[str, str]]:
    """Check for drift between frontmatter ``entities:`` and body ``[[wikilinks]]``.

    Returns a (possibly empty) list of warning dicts, each with keys:
    ``kind`` (``"body_not_in_frontmatter"`` or ``"frontmatter_not_in_body"``) and
    ``detail`` (a human-readable description).

    Auto-footer-aware: wikilinks that appear after the
    ``<!-- palinode-auto-footer -->`` marker count as satisfying the frontmatter
    entity requirement.  Body wikilinks under the auto-footer are NOT flagged as
    missing from frontmatter — the auto-footer is derived from frontmatter, so
    those links are guaranteed to correspond to frontmatter entries.

    Args:
        metadata: Parsed frontmatter dict (from ``parser.parse_markdown``).
        body: Markdown body text (frontmatter stripped).

    Returns:
        List of warning dicts (empty list if surfaces are aligned).
    """
    entity_info = parser.parse_entities(metadata, body)
    fm_entities: list[str] = entity_info["entities_frontmatter"]
    body_entities: list[str] = entity_info["entities_body"]

    fm_set = set(fm_entities)
    body_set = set(body_entities)

    # Determine which wikilinks live under the auto-footer.
    auto_footer_entities: set[str] = set()
    if _AUTO_FOOTER_MARKER in body:
        _, _, footer_text = body.partition(_AUTO_FOOTER_MARKER)
        footer_labels = re.findall(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', footer_text)
        for label in footer_labels:
            canonical = parser.canonicalize_wikilink(label.strip(), known_entities=fm_entities)
            auto_footer_entities.add(canonical)

    warnings: list[dict[str, str]] = []

    # 1. Body wikilinks not in frontmatter (skip auto-footer ones — they come from FM)
    for ent in body_entities:
        if ent not in fm_set and ent not in auto_footer_entities:
            warnings.append({
                "kind": "body_not_in_frontmatter",
                "detail": (
                    f"body wikilink not in entities frontmatter: {ent!r}"
                ),
            })

    # 2. Frontmatter entities not in body and not covered by auto-footer
    for ent in fm_entities:
        if ent not in body_set and ent not in auto_footer_entities:
            warnings.append({
                "kind": "frontmatter_not_in_body",
                "detail": (
                    f"entity not in body or see-also: {ent!r}"
                ),
            })

    return warnings


def check_relative_dates(body: str) -> list[dict[str, str]]:
    """Return relative time expressions whose meaning will drift over time."""
    findings: list[dict[str, str]] = []
    for line_number, line in enumerate(body.splitlines(), start=1):
        findings.extend(
            {"line": str(line_number), "expression": match.group(0)}
            for match in _RELATIVE_DATE_RE.finditer(line)
        )
    return findings


def run_lint_pass() -> dict[str, Any]:
    """Return every deterministic lint finding and the scanned-file total."""
    base_dir = getattr(config, 'memory_dir', config.palinode_dir)
    pattern = os.path.join(base_dir, "**/*.md")
    
    orphaned_files = []
    stale_files = []
    missing_fields = []
    contradictions = []  # Heuristic placeholder
    missing_entities: list[str] = []
    missing_descriptions: list[str] = []
    missing_priority: list[str] = []
    wiki_drift: list[dict[str, Any]] = []
    relative_dates: list[dict[str, Any]] = []
    source_anchor_issues: list[dict[str, Any]] = []
    claim_anchor_issues: list[dict[str, Any]] = []
    # (ADR-018): an `epistemic: open_question` that has gone unresolved for a
    # long time is a staleness signal — it wants resolution into fact/inference
    # or supersession. Reuses the stale threshold (90 days).
    stale_open_questions: list[dict[str, Any]] = []
    open_contradictions: list[dict[str, Any]] = []  # (G4)
    core_count = 0

    now = datetime.now(timezone.utc)

    entity_references: dict[str, int] = {}
    all_files = []
    
    skip_dirs = {"archive", "logs", ".obsidian"}
    
    for filepath in glob.glob(pattern, recursive=True):
        rel_path = os.path.relpath(filepath, base_dir)
        parts = rel_path.split(os.sep)
        if parts[0] in skip_dirs:
            continue
            
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            metadata, _ = parser.parse_markdown(content)

            # Extract body (strip frontmatter) for wiki_drift check.
            try:
                _post = _frontmatter.loads(content)
                body_text: str = _post.content
            except Exception:
                body_text = content

            entities = metadata.get("entities", [])
            for e in entities:
                entity_references[e] = entity_references.get(e, 0) + 1

            all_files.append({
                "path": rel_path,
                "metadata": metadata,
                "body": body_text,
            })
        except Exception:
            pass

    for f in all_files:
        path = f["path"]
        meta = f["metadata"]
        
        # 1. Missing fields — memory files only.
        #
        # `daily/` notes are the structural log tier, not a memory tier: they
        # are append-only (N sessions per file), and session-end already
        # persists each session separately as a typed memory. They are exempt
        # from the required-frontmatter contract — see PROGRAM.md § File tiers.
        # Every sibling check below already skips them; this one didn't, so it
        # counted `daily/` violations in a numerator whose denominator
        # (`total_files`) excludes `daily/`. The exemption makes the report
        # internally consistent and restores its value as a regression signal:
        # a flagged file is now always a real problem.
        if not path.startswith("daily/"):
            missing = []
            if not meta.get("id"):
                missing.append("id")
            if not meta.get("type"):
                missing.append("type")
            if not meta.get("category"):
                missing.append("category")
            if missing:
                missing_fields.append({"file": path, "missing": missing})
            
        # 2. Orphans
        category = meta.get("category", "")
        if category and not path.startswith("daily/"):
            slug = path.split(os.sep)[-1].replace(".md", "")
            # Removing any layer suffixes like -status or -history
            if slug.endswith("-status"):
                slug = slug[:-7]
            if slug.endswith("-history"):
                slug = slug[:-8]
            
            own_entity_ref = f"{category}/{slug}"
            has_entities = len(meta.get("entities", [])) > 0
            is_referenced = entity_references.get(own_entity_ref, 0) > 0
            
            # An orphan has NO entities AND is not referenced by anything else
            if not has_entities and not is_referenced:
                orphaned_files.append(path)
                
        # 3. Missing entities (non-daily files with empty entities list)
        if not path.startswith("daily/") and not meta.get("entities"):
            missing_entities.append(path)

        # 4. Missing description
        if not path.startswith("daily/") and not meta.get("description"):
            missing_descriptions.append(path)

        # 5. Core count
        if meta.get("core"):
            core_count += 1

        # 6. Missing human priority on core and decision memories.
        if (meta.get("core") is True or meta.get("type") == "Decision") and "priority" not in meta:
            missing_priority.append(path)

        # 6. Stale
        if meta.get("status") == "active":
            last_updated = meta.get("last_updated") or meta.get("created_at")
            if last_updated:
                if isinstance(last_updated, str):
                    try:
                        dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                    except ValueError:
                        dt = None  # malformed date string — skip this file
                elif isinstance(last_updated, datetime):
                    dt = last_updated
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = None  # frontmatter field is neither string nor datetime

                if dt is not None:
                    days_old = (now - dt).days
                    if days_old > 90:
                        stale_files.append({"file": path, "days_old": days_old})

        # 6b. Stale open questions — an unresolved open_question that's
        # months old wants attention. Independent of `status` (an open question
        # is about epistemic state, not lifecycle), so checked separately from
        # the status==active stale check above.
        if meta.get("epistemic") == "open_question":
            oq_updated = meta.get("last_updated") or meta.get("created_at")
            if oq_updated:
                if isinstance(oq_updated, str):
                    try:
                        oq_dt = datetime.fromisoformat(oq_updated.replace('Z', '+00:00'))
                    except ValueError:
                        oq_dt = None  # malformed date string — skip this file
                elif isinstance(oq_updated, datetime):
                    oq_dt = oq_updated
                    if oq_dt.tzinfo is None:
                        oq_dt = oq_dt.replace(tzinfo=timezone.utc)
                else:
                    oq_dt = None  # frontmatter field is neither string nor datetime

                if oq_dt is not None:
                    oq_age = (now - oq_dt).days
                    if oq_age > 90:
                        stale_open_questions.append({"file": path, "days_old": oq_age})

        # 7. Wiki drift — frontmatter entities vs. body wikilinks.
        # Skipped for daily/ logs, the second outlier found in this pass: they
        # are a structural tier with no `entities:` contract (PROGRAM.md
        # § File tiers), so a [[wikilink]] written in a session summary gets
        # reported as drifting from a list the file will never carry. The
        # check measures agreement between two halves of the wiki contract;
        # a file outside that contract has nothing to disagree with.
        if not path.startswith("daily/"):
            body = f.get("body", "")
            drift_warnings = check_wiki_drift(meta, body)
            if drift_warnings:
                wiki_drift.append({"file": path, "warnings": drift_warnings})

            relative_date_matches = check_relative_dates(body)
            if relative_date_matches:
                relative_dates.append({"file": path, "matches": relative_date_matches})

        # 8. Source-citation anchors — verify each ``sources:`` anchor's
        # integrity hash and that the cited quote still appears in its source.
        # Clean no-op for files with no anchors (verify returns []). Only
        # non-OK results are reported as health findings.
        if meta.get("sources"):
            from palinode.core.quote_verify import verify_memory_sources
            try:
                results = verify_memory_sources(path, base_dir)
            except OSError:
                results = []
            bad = [
                {"ref": r.ref, "status": r.status.value, "detail": r.message}
                for r in results
                if not r.ok
            ]
            if bad:
                source_anchor_issues.append({"file": path, "anchors": bad})

        # 8b. Claim-level anchors — resolve each ``claims:`` binding and
        # surface the ones that no longer hold: a span failing its integrity
        # check, a claim_id that no longer matches its content-addressed
        # derivation, or a claim citing a source_id absent from the memory's
        # own ``sources:`` anchors (advisory — a claim with no backing
        # source anchor). Clean no-op for files with no claims.
        if meta.get("claims"):
            from palinode.core.claims import resolve_memory_claims
            try:
                claim_results = resolve_memory_claims(path, base_dir)
            except OSError:
                claim_results = []
            bad_claims = []
            for r in claim_results:
                issues = []
                if r.get("span_status") != "ok":
                    issues.append(r["span_status"])
                if r.get("claim_id_status") != "ok":
                    issues.append("claim_id_mismatch")
                if not r.get("source_declared", False):
                    issues.append("source_undeclared")
                if issues:
                    bad_claims.append({
                        "claim_id": r.get("claim_id", ""),
                        "source_id": r.get("source_id", ""),
                        "issues": issues,
                        "detail": r.get("span_detail", ""),
                    })
            if bad_claims:
                claim_anchor_issues.append({"file": path, "claims": bad_claims})

        # 9. Open contradictions (G4) — a non-empty `contradicts` link is
        # an UNRESOLVED disagreement (supersession resolves; a contradicts link
        # deliberately does not pick a winner). Surface every file that still
        # carries one as a health signal so reviewers can adjudicate.
        from palinode.core.typed_links import parse_link_refs
        _contradicts = parse_link_refs(meta, "contradicts")
        if _contradicts:
            open_contradictions.append({"file": path, "contradicts": _contradicts})

    # 4. Contradictions heuristics
    # Simple check: Any entity that has multiple active files
    file_statuses = {}
    for f in all_files:
         cat = f["metadata"].get("category", "")
         if not cat or f["path"].startswith("daily/"):
             continue
         slug = f["path"].split(os.sep)[-1].replace(".md", "")
         if slug.endswith("-status"):
             slug = slug[:-7]
         if slug.endswith("-history"):
             slug = slug[:-8]
         ent = f"{cat}/{slug}"
         
         status = f["metadata"].get("status", "active")
         if status == "active":
             file_statuses[ent] = file_statuses.get(ent, 0) + 1
             if file_statuses[ent] > 1:
                 contradictions.append({
                     "entity": ent, 
                     "issue": "Multiple 'active' files detected for the same entity."
                 })

    # Deduplicate contradictions
    unique_contradictions = [dict(t) for t in {tuple(d.items()) for d in contradictions}]

    # Count of memory files scanned, excluding daily/ logs — the same set the
    # orphaned / stale / missing-description counts above are derived from. A UI
    # showing this as the "memories" total stays coherent with those counts
    # regardless of index state (markdown is the source of truth, not the DB).
    total_files = sum(1 for f in all_files if not f["path"].startswith("daily/"))

    return {
        "total_files": total_files,
        "orphaned_files": orphaned_files,
        "stale_files": stale_files,
        "missing_fields": missing_fields,
        "contradictions": unique_contradictions,
        "missing_entities": missing_entities,
        "missing_descriptions": missing_descriptions,
        "missing_priority": missing_priority,
        "wiki_drift": wiki_drift,
        "relative_dates": relative_dates,
        "source_anchor_issues": source_anchor_issues,
        "claim_anchor_issues": claim_anchor_issues,
        "stale_open_questions": stale_open_questions,
        "open_contradictions": open_contradictions,
        # Refs that look like aliases of one another. Detection only — the
        # report is a question for a human, never an instruction to merge.
        "entity_aliases": check_entity_aliases(entity_references),
        "core_count": core_count,
    }
