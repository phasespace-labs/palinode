"""
Compaction Executor

Applies validated structured operations to markdown memory files.

This separation ensures:
- Inputs are schema-checked before file changes
- Every change is a git commit with clear provenance
- Operations are auditable and reversible
"""
from __future__ import annotations

import os
import re
import logging
from datetime import UTC, datetime


from palinode.core import git_tools
from palinode.core.parser import split_frontmatter
from palinode.consolidation.op_parse import op_kind

logger = logging.getLogger("palinode.consolidation.executor")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _is_replace_policy(content: str) -> bool:
    """Return True if the file's frontmatter declares ``update_policy: replace``.

    ADR-015 §2.2: a ``replace`` doc is a living/current-state document that
    consolidation must never SUPERSEDE/ARCHIVE-into-history. Parses the
    frontmatter via the shared markdown parser; any parse failure falls open to
    ``False`` (no protection) so a malformed file never blocks consolidation.

    A WARNING is emitted on the fail-open path when the raw text contains
    ``update_policy: replace`` but the parsed metadata does not — this
    indicates frontmatter corruption that silently removed the protection.
    The fail-open behaviour is intentional and preserved.
    """
    try:
        from palinode.core.parser import parse_markdown

        metadata, _ = parse_markdown(content)
    except Exception as exc:  # noqa: BLE001 — defensive: never let the guard raise
        # parse_markdown itself swallows exceptions internally, so this branch
        # is a last-resort safety net. Log with the hint regardless.
        logger.warning(
            "replace-guard: unexpected error parsing frontmatter — "
            "falling open to no-protection (doc may be unprotected): %s",
            exc,
        )
        return False

    protected = metadata.get("update_policy") == "replace"

    # Cheap post-parse corruption check: the parser returns {} on
    # garbled frontmatter — if the raw text contains the policy declaration
    # but the parsed metadata does not, the frontmatter silently failed and
    # the protection is lost. Warn so operators can detect this without
    # blocking consolidation.
    if not protected and "update_policy: replace" in content:
        logger.warning(
            "replace-guard: raw text contains 'update_policy: replace' but "
            "parsed metadata does not — frontmatter may be corrupt; "
            "falling open to no-protection (doc may be unprotected)",
        )

    return protected


def _superseded_only_signal(file_path: str, content: str) -> str | None:
    """Name the rule making this document superseded-only, or ``None``.

    ADR-020: identity/profile documents (``people/``, a project's profile doc,
    a living ``update_policy: replace`` doc, a ``core: true`` doc, or any doc
    declaring ``retirement_policy: superseded-only``) retire by supersession or
    retraction, never by age. The classification lives in
    :mod:`palinode.consolidation.retirement` so the executor and the TTL sweep
    cannot drift on what an identity document is.

    Parses defensively and falls open to "no protection" on a garbled file, for
    the same reason the ``update_policy: replace`` guard does: a malformed
    frontmatter must never block consolidation entirely.
    """
    try:
        from palinode.core.parser import parse_markdown

        metadata, _ = parse_markdown(content)
    except Exception as exc:  # noqa: BLE001 — defensive: never let the guard raise
        logger.warning(
            "retirement-guard: unexpected error parsing frontmatter — "
            "falling open to no-protection (doc may be unprotected): %s",
            exc,
        )
        metadata = {}
    from palinode.consolidation.retirement import SUPERSEDED_ONLY, classify

    policy, signal = classify(file_path, metadata)
    return signal if policy == SUPERSEDED_ONLY else None


def _normalize_fact_text(text: str) -> str:
    """Normalize LLM-proposed fact text to list-item content only."""
    normalized = text.strip()
    normalized = re.sub(r"^[-*]\s+", "", normalized)
    return normalized


def _extract_fact_date(content: str, fact_id: str) -> str | None:
    """Extract the date prefix from a fact's text, if present.

    Looks for a ``[YYYY-MM-DD]`` date tag at the start of the fact text.
    Returns the ``YYYY-MM-DD`` string, or ``None`` if no date is found.
    """
    pattern = re.compile(
        r'^[\s]*[-*]\s+\[(\d{4}-\d{2}-\d{2})\].*?<!-- fact:' + re.escape(fact_id) + r' -->',
        re.MULTILINE,
    )
    m = pattern.search(content)
    return m.group(1) if m else None


def _nightly_merge_allowed(content: str, ids: list[str]) -> bool:
    """Return True iff a nightly MERGE is permitted for the given fact IDs.

    Nightly policy: all facts in the merge must share the same calendar date
    (``[YYYY-MM-DD]`` prefix in their text).  Cross-date merges are deferred to
    the weekly pass.  Facts without a recognisable date prefix are rejected to
    avoid silent data loss.
    """
    if not ids:
        return False
    dates = [_extract_fact_date(content, fid) for fid in ids]
    # Any fact without a parseable date → reject
    if any(d is None for d in dates):
        return False
    # All dates must be the same calendar day
    return len(set(dates)) == 1


def _atomic_write_text(file_path: str, content: str) -> None:
    """Write text atomically via the git_tools mutation choke point.

    All memory-file writes route through :func:`git_tools.write_memory_file`
    (the single atomic write primitive) so a future signer can observe content
    at mutation time in one place. Retained as a thin local alias because the
    executor calls it from two sites (op write-back + history append).
    """
    git_tools.write_memory_file(file_path, content)


def apply_operations(file_path: str, operations: list[dict], *,
                     nightly_policy: bool = False,
                     applied_merges: list[int] | None = None) -> dict:
    """Apply a list of operations to a memory file.

    Args:
        file_path: Path to the target markdown file.
        operations: List of operation dicts with 'op' key.
        nightly_policy: When True, MERGE ops are subject to the same-day guard:
            only facts sharing the same ``[YYYY-MM-DD]`` date prefix may be
            merged.  Cross-date or undated MERGE proposals are rejected with a
            log warning and counted as ``merge_rejected``.
        applied_merges: Optional out-parameter. When given, the index into
            ``operations`` of every MERGE the executor actually applied is
            appended to it. A caller that records an audit line per operation
            (the runner's Consolidation Log) can then record the outcome
            rather than the proposal — a MERGE that retired nothing must not
            leave a line saying a merge happened.

    Returns:
        Stats dict: {kept, updated, merged, superseded, archived, retracted,
                     merge_rejected, protected_rejected, contradicts_proposed,
                     unmatched}. ``unmatched`` counts UPDATE/MERGE/SUPERSEDE/
                     ARCHIVE/RETRACT ops that were dropped without effect —
                     either a required field (e.g. ``new_text``) was
                     missing/empty, or the op's fact id(s) were not found in
                     the file. Both cases are logged as a warning; previously
                     both were silent. This is distinct from
                     ``merge_rejected``, which counts a MERGE deliberately
                     refused by the nightly same-day policy — a rejection
                     with a reason, not a drop. ``protected_rejected`` counts
                     ops the target document's own declared regime refused:
                     the ADR-015 ``update_policy: replace`` guard, and the
                     ADR-020 guard that forbids retiring an identity/profile
                     document by age (an ARCHIVE with no ``superseded_by``).
    """
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    # Every fact operation below matches markdown list syntax with a whole-file
    # MULTILINE regex. YAML frontmatter uses the same `- item` syntax, so an op
    # could (and did) rewrite an `entities:` entry with LLM-supplied prose —
    # producing frontmatter that no longer strict-parses. Split once and mutate
    # only the body; the frontmatter block is preserved byte-for-byte
    # (PROPOSE_CONTRADICTS below is the sole intentional frontmatter writer, and
    # it goes through the typed-links merger).
    frontmatter_block, body = split_frontmatter(content)

    # ADR-015 §2.2 §3: a memory declaring `update_policy: replace` is a
    # living/current-state document. Consolidation may UPDATE it in place but
    # must NEVER SUPERSEDE it (strikethrough + spawn a "supersedes-" sibling)
    # or ARCHIVE-into-history it — either would fork the single current fact
    # into a stale historical snapshot, the exact failure mode the axis exists
    # to prevent. Read the file's own declared regime once and guard the
    # history-forking ops. Parse defensively: an unreadable/garbled frontmatter
    # falls open to today's behaviour (no protection) rather than blocking
    # consolidation entirely.
    is_replace_doc = _is_replace_policy(content)

    # ADR-020: retirement policy is document-relative. An identity/profile
    # document — a person, a project's profile doc, a living doc, a `core: true`
    # doc, or one declaring `retirement_policy: superseded-only` — may still be
    # retired, but only for a stated reason other than age. ARCHIVE is the one
    # op that removes a fact from recall with no retrievable trace in the main
    # file, and the compaction prompt's staleness rule is what proposes it, so
    # ARCHIVE here is allowed only when the op names a successor
    # (`superseded_by`). SUPERSEDE and RETRACT are untouched: both state a
    # reason that is not age.
    superseded_only_signal = _superseded_only_signal(file_path, content)

    stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0, "retracted": 0, "merge_rejected": 0, "protected_rejected": 0, "contradicts_proposed": 0, "unmatched": 0, "review_flagged": 0}

    # Every op that retires a fact's current text — SUPERSEDE, ARCHIVE, RETRACT,
    # MERGE — is recorded here as (kind, fact ids, reason) so that, once the
    # file is written, the memories whose `backed_by` cites it can be flagged
    # for review. Collected across the loop and propagated once per call: one
    # scan of the store, one commit, whatever the number of ops.
    retirements: list[tuple[str, list[str], str]] = []

    for op_index, op in enumerate(operations):
        if not isinstance(op, dict):
            logger.warning(f"Malformed operation (expected dict, got {type(op).__name__}): {op}")
            continue

        op_type = op_kind(op) or "KEEP"

        # ADR-015 §2.2: refuse history-forking ops on a living (replace) doc.
        # UPDATE/MERGE/KEEP keep the one current fact current. SUPERSEDE and
        # ARCHIVE move content into history. RETRACT is ALSO history-forking on
        # a living doc (H3): _retract_fact strikethrough-tombstones the current
        # fact in place AND appends a `-history.md` sibling — exactly the stale-
        # snapshot fork this axis forbids. A provably-wrong value in a living
        # document must be corrected with UPDATE, not tombstoned; guard RETRACT.
        if is_replace_doc and op_type in ("SUPERSEDE", "ARCHIVE", "RETRACT"):
            logger.warning(
                "%s rejected by update_policy=replace guard (living document): "
                "%s on %s",
                op_type,
                op.get("id"),
                file_path,
            )
            stats["protected_rejected"] += 1
            continue

        # ADR-020 age-retirement guard. An ARCHIVE carrying `superseded_by` is
        # a stated supersession and is applied; one without it is retirement
        # argued from age/staleness alone, which this document's regime
        # forbids. Counted with the replace-guard's rejections — both are the
        # document refusing an op, with the reason in the log line.
        if (
            superseded_only_signal is not None
            and op_type == "ARCHIVE"
            and not str(op.get("superseded_by") or "").strip()
        ):
            logger.warning(
                "ARCHIVE rejected by retirement_policy=superseded-only guard "
                "(%s): age/staleness is not a retirement reason for this "
                "document — use SUPERSEDE, RETRACT, or an ARCHIVE naming "
                "superseded_by: %s on %s (rationale: %r)",
                superseded_only_signal,
                op.get("id"),
                file_path,
                op.get("rationale", op.get("reason", "")),
            )
            stats["protected_rejected"] += 1
            continue

        if op_type == "KEEP":
            stats["kept"] += 1
            continue

        elif op_type == "UPDATE":
            fact_id = op.get("id")
            new_text = op.get("new_text", "")
            if fact_id and new_text:
                updated_body = _update_fact(body, fact_id, new_text)
                if updated_body != body:
                    body = updated_body
                    stats["updated"] += 1
                else:
                    logger.warning(
                        "UPDATE unmatched: fact id=%r not found in %s",
                        fact_id, file_path,
                    )
                    stats["unmatched"] += 1
            else:
                logger.warning(
                    "UPDATE dropped: missing required field(s) (id=%r, "
                    "new_text present=%s) in %s",
                    fact_id, bool(new_text), file_path,
                )
                stats["unmatched"] += 1

        elif op_type == "MERGE":
            ids = op.get("ids", [])
            new_text = op.get("new_text", "")
            reason = op.get("rationale", op.get("reason", ""))
            if ids and new_text:
                if nightly_policy and not _nightly_merge_allowed(body, ids):
                    id_list = ", ".join(ids)
                    logger.warning(
                        f"MERGE rejected by nightly policy: cross-date or undated facts "
                        f"({id_list}) in {file_path}"
                    )
                    stats["merge_rejected"] += 1
                    continue
                merged_body = _merge_facts(body, ids, new_text, reason, file_path)
                if merged_body != body:
                    body = merged_body
                    stats["merged"] += 1
                    retirements.append(("merge", list(ids), reason))
                    if applied_merges is not None:
                        applied_merges.append(op_index)
                else:
                    logger.warning(
                        "MERGE unmatched: fact id(s)=%r not found, or nothing "
                        "left to retire, in %s",
                        ids, file_path,
                    )
                    stats["unmatched"] += 1
            else:
                logger.warning(
                    "MERGE dropped: missing required field(s) (ids=%r, "
                    "new_text present=%s) in %s",
                    ids, bool(new_text), file_path,
                )
                stats["unmatched"] += 1

        elif op_type == "SUPERSEDE":
            fact_id = op.get("id")
            new_text = op.get("new_text", "")
            reason = op.get("reason", "")
            if fact_id and new_text:
                superseded_body = _supersede_fact(body, fact_id, new_text, reason, file_path)
                if superseded_body != body:
                    body = superseded_body
                    stats["superseded"] += 1
                    retirements.append(("supersede", [fact_id], reason))
                else:
                    logger.warning(
                        "SUPERSEDE unmatched: fact id=%r not found in %s",
                        fact_id, file_path,
                    )
                    stats["unmatched"] += 1
            else:
                logger.warning(
                    "SUPERSEDE dropped: missing required field(s) (id=%r, "
                    "new_text present=%s) in %s",
                    fact_id, bool(new_text), file_path,
                )
                stats["unmatched"] += 1

        elif op_type == "ARCHIVE":
            fact_id = op.get("id")
            reason = op.get("rationale", op.get("reason", ""))
            if fact_id:
                archived_body = _archive_fact(body, fact_id, reason, file_path)
                if archived_body != body:
                    body = archived_body
                    stats["archived"] += 1
                    retirements.append(("archive", [fact_id], reason))
                else:
                    logger.warning(
                        "ARCHIVE unmatched: fact id=%r not found in %s",
                        fact_id, file_path,
                    )
                    stats["unmatched"] += 1
            else:
                logger.warning(
                    "ARCHIVE dropped: missing required field id in %s",
                    file_path,
                )
                stats["unmatched"] += 1

        elif op_type == "RETRACT":
            fact_id = op.get("id")
            reason = op.get("reason", op.get("rationale", ""))
            if fact_id:
                retracted_body = _retract_fact(body, fact_id, reason, file_path)
                if retracted_body != body:
                    body = retracted_body
                    stats["retracted"] += 1
                    retirements.append(("retract", [fact_id], reason))
                else:
                    logger.warning(
                        "RETRACT unmatched: fact id=%r not found in %s",
                        fact_id, file_path,
                    )
                    stats["unmatched"] += 1
            else:
                logger.warning(
                    "RETRACT dropped: missing required field id in %s",
                    file_path,
                )
                stats["unmatched"] += 1

        elif op_type == "PROPOSE_CONTRADICTS":
            # (G4): the executor may PROPOSE a typed contradiction link but
            # must NEVER auto-resolve a conflict. SUPERSEDE stays the only
            # winner-picking op. This op is non-destructive: it records the
            # `contradicts` link in frontmatter (idempotently) and picks no
            # winner. It is intentionally NOT subject to the replace-guard above
            # — recording a conflict forks nothing into history.
            refs = op.get("contradicts", op.get("refs", op.get("ids")))
            try:
                from palinode.core.typed_links import (
                    TypedLinkError,
                    merge_link_refs_into_content,
                    normalize_link_refs,
                )
                norm = normalize_link_refs(refs, "contradicts")
            except TypedLinkError as exc:
                logger.warning(
                    "PROPOSE_CONTRADICTS rejected (malformed refs) on %s: %s",
                    file_path, exc,
                )
                norm = []
            if norm:
                current = frontmatter_block + body
                proposed_content = merge_link_refs_into_content(
                    current, "contradicts", norm
                )
                if proposed_content != current:
                    # The merger re-dumps the whole document; re-split so the
                    # remaining ops keep operating on the body alone.
                    frontmatter_block, body = split_frontmatter(proposed_content)
                    stats["contradicts_proposed"] += 1

    # Write back
    _atomic_write_text(file_path, frontmatter_block + body)

    # `backed_by` propagation (one hop, flag-only): now that the retirements
    # are on disk, every live memory citing this file as a source gets a
    # `stale_backing` entry. Deterministic, idempotent per source, its own
    # commit. The dependents are not rewritten — that is the next
    # consolidation pass's job, with the flag as its input.
    if retirements:
        from palinode.consolidation.propagate import flag_dependents

        reasons: list[str] = []
        for _, _, r in retirements:
            if r and r not in reasons:
                reasons.append(r)
        flagged = flag_dependents(
            file_path,
            ops=[kind for kind, _, _ in retirements],
            facts=[fid for _, ids, _ in retirements for fid in ids],
            reason="; ".join(reasons),
        )
        stats["review_flagged"] = len(flagged)

    return stats


def _update_fact(content: str, fact_id: str, new_text: str) -> str:
    """Replace a fact's text while preserving its ID."""
    pattern = re.compile(
        r'^([\s]*[-*]\s+).*?(<!-- fact:' + re.escape(fact_id) + r' -->)',
        re.MULTILINE
    )
    replacement = rf'\1{_normalize_fact_text(new_text)} <!-- fact:{fact_id} -->'
    return pattern.sub(replacement, content, count=1)


def _merge_facts(content: str, ids: list[str], new_text: str,
                 reason: str, file_path: str) -> str:
    """Remove all source facts and insert merged fact at first occurrence.

    Every source fact's original text — ``ids[0]``, whose text is replaced,
    and ``ids[1:]``, whose lines are removed — is appended verbatim to the
    ``-history.md`` sibling before the body is mutated. MERGE was the
    one op whose sources leave the main file entirely, so without this the
    only copy of the working behind a merged conclusion was in ``git log``,
    which recall cannot address.

    When ``new_text`` is the surviving fact's own text, the proposal is
    "``ids[1:]`` are already said by ``ids[0]``" — a legitimate merge whose
    conclusion happens to need no rewriting, not a failed one. That case keeps
    ``ids[0]`` byte-identical (nothing is rewritten, so nothing is renamed) and
    retires ``ids[1:]`` to history exactly as the rewriting path does. It used
    to abort on the unchanged-content check, leaving the duplicates in the file
    with no history entry.
    """
    first_id = ids[0]
    merged_id = f"merged-{ids[0]}"
    now = _utc_now().strftime("%Y-%m-%d")

    def fact_pattern(fid: str) -> re.Pattern[str]:
        return re.compile(
            r'^([\s]*[-*]\s+)(.*?)(<!-- fact:' + re.escape(fid) + r' -->)\n?',
            re.MULTILINE,
        )

    def record(fid: str, old_text: str, target_id: str) -> None:
        append_to_history(
            file_path, fid,
            f"Merged into {target_id} ({now}): {old_text} (reason: {reason})",
        )

    first_match = fact_pattern(first_id).search(content)
    if first_match is None:
        return content

    unchanged = first_match.group(2).strip() == _normalize_fact_text(new_text)
    if unchanged:
        # The survivor keeps its text and its id, so the remaining sources are
        # merged *into* ``first_id``. Removing by id would take the survivor's
        # own line with it (no `merged-` rename happened), so a source that
        # repeats ``ids[0]`` is left alone rather than deleted.
        surviving_id = first_id
        remaining = [fid for fid in ids[1:] if fid != first_id]
    else:
        # Replace first with merged text
        updated_content = _update_fact(content, first_id, new_text)
        if updated_content == content:
            return content
        record(first_id, first_match.group(2).strip(), merged_id)
        content = updated_content
        # Update the fact ID to the merged ID
        content = re.sub(
            r"<!-- fact:" + re.escape(first_id) + r" -->",
            f"<!-- fact:{merged_id} -->",
            content,
            count=1,
        )
        surviving_id = merged_id
        remaining = list(ids[1:])

    # Remove remaining source facts. Every removed line is recorded, not just
    # the first match — a duplicate id is still a fact leaving the file.
    for fid in remaining:
        pattern = fact_pattern(fid)
        for m in pattern.finditer(content):
            record(fid, m.group(2).strip(), surviving_id)
        content = pattern.sub('', content)

    return content


def _supersede_fact(content: str, fact_id: str, new_text: str,
                    reason: str, file_path: str) -> str:
    """Mark a fact as superseded and add the new version."""
    now = _utc_now().strftime("%Y-%m-%d")
    new_id = f"supersedes-{fact_id}"
    
    # Strikethrough the old fact and add superseded marker
    pattern = re.compile(
        r'^([\s]*[-*]\s+)(.*?)(<!-- fact:' + re.escape(fact_id) + r' -->)',
        re.MULTILINE
    )
    
    def replacer(m):
        old_text = m.group(2).strip()
        return (f"{m.group(1)}~~{old_text}~~ [superseded {now}] {m.group(3)}\n"
                f"{m.group(1)}{_normalize_fact_text(new_text)} <!-- fact:{new_id} -->")
    
    updated_content, substitutions = pattern.subn(replacer, content, count=1)
    if substitutions == 0:
        return content
    
    # Also append to history file
    append_to_history(file_path, fact_id, f"Superseded ({now}): {reason}")
    
    return updated_content


def _archive_fact(content: str, fact_id: str, reason: str, file_path: str) -> str:
    """Remove a fact from the file and append it to the history file."""
    # Extract the fact text before removing
    pattern = re.compile(
        r'^([\s]*[-*]\s+)(.*?)(<!-- fact:' + re.escape(fact_id) + r' -->)\n?',
        re.MULTILINE
    )
    match = pattern.search(content)
    if match:
        archived_text = match.group(2).strip()
        append_to_history(file_path, fact_id,
                          f"Archived: {archived_text} (reason: {reason})")

    # Remove from main file
    content = pattern.sub('', content)
    return content


def _retract_fact(content: str, fact_id: str, reason: str, file_path: str) -> str:
    """Mark a fact as retracted — explicitly wrong, not just stale.

    Unlike ARCHIVE (removes silently), RETRACT leaves a visible tombstone
    with strikethrough and reason so readers know the fact was wrong and why.
    Aligns with IETF Knowledge Unit lifecycle (retract = known-incorrect).
    """
    now = _utc_now().strftime("%Y-%m-%d")
    reason_text = f" — {reason}" if reason else ""

    pattern = re.compile(
        r'^([\s]*[-*]\s+)(.*?)(<!-- fact:' + re.escape(fact_id) + r' -->)',
        re.MULTILINE
    )

    def replacer(m):
        old_text = m.group(2).strip()
        return f"{m.group(1)}~~{old_text}~~ [RETRACTED {now}{reason_text}] {m.group(3)}"

    updated_content, substitutions = pattern.subn(replacer, content, count=1)
    if substitutions == 0:
        return content

    append_to_history(file_path, fact_id, f"Retracted ({now}): {reason}")

    return updated_content


def _ensure_archived_frontmatter(content: str) -> str:
    """Ensure a history file's frontmatter carries ``status: archived``.

    History files hold ARCHIVE'd / SUPERSEDE'd facts. They must be excluded
    from default recall (``config.search.exclude_status = ["archived"]``) while
    staying indexed and retrievable on demand. The status lives in file-level
    frontmatter, which the indexer propagates to every chunk's metadata.

    Legacy history files created before this fix lack the field; inject it on
    the next append rather than leaving them leaking into recall.
    """
    fm_match = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
    if not fm_match:
        # No frontmatter at all — prepend a complete archived block.
        return "---\ncategory: history\ncore: false\nstatus: archived\n---\n\n" + content
    fm_body = fm_match.group(1)
    if re.search(r'^status:', fm_body, re.MULTILINE):
        return content  # already carries an explicit status; respect it
    new_fm_body = fm_body + "\nstatus: archived"
    return content[:fm_match.start(1)] + new_fm_body + content[fm_match.end(1):]


def append_to_history(file_path: str, fact_id: str, text: str) -> str:
    """Append an entry to the corresponding history file; return its path.

    The history file carries ``status: archived`` frontmatter so its content
    (archived + superseded facts) is suppressed from default recall while
    remaining indexed and retrievable on demand — preserving the audit trail
    PROGRAM.md's "never hard-delete" contract requires.

    Returns the history-file path so callers that must stage it in the same commit (the
    on-demand archive op, the no on-demand archive/supersede for a specific work) don't
    re-derive the naming rule. """
    base = re.sub(r'-status\.md$', '', file_path)
    base = re.sub(r'\.md$', '', base)
    history_path = f"{base}-history.md"

    now = _utc_now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{now}] {text} <!-- fact:{fact_id} -->\n"

    if os.path.exists(history_path):
        with open(history_path, encoding="utf-8") as f:
            history_content = f.read()
        history_content = _ensure_archived_frontmatter(history_content)
    else:
        history_content = "---\ncategory: history\ncore: false\nstatus: archived\n---\n\n# History\n\n"

    _atomic_write_text(history_path, history_content + entry)
    return history_path
