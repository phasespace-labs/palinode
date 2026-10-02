"""
Consolidation Runner

Orchestrates weekly memory consolidation: daily → curated.
Uses a configurable LLM for distillation (any OpenAI-compatible endpoint).
"""
from __future__ import annotations

import os
import re
import json
import glob
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, NamedTuple

# The injectable model call has the shape
# (system_prompt, user_prompt) -> (response_text, model_used).
# Making this callable injectable lets the consolidation path be driven with
# canned op-JSON — no live LLM, no wholesale mock of _consolidate_project. The
# default is the live fallback-chain caller; tests pass a fake that returns
# deterministic op-JSON. Kept here (not a separate module) so the client-factory
# patch seam test_fallback relies on — `runner.get_ollama_client` — stays put.
LlmFn = Callable[[str, str], tuple[str, str]]

import yaml

from palinode.core.config import config
from palinode.core import store, embedder, git_tools
from palinode.core.lifecycle import eligibility, is_retired_fact_text, parse_moment
from palinode.core.ollama_client import OllamaError, OllamaRole, get_ollama_client
from palinode.core.parser import split_frontmatter
from palinode.consolidation import retirement, status_doc, watermark
from palinode.consolidation.fact_ids import FACT_LINE_RE, count_body_facts
from palinode.consolidation.log_lines import LogLine, older_than
from palinode.consolidation.op_parse import op_kind, op_reason, parse_result
from palinode.consolidation.proposal_guard import (
    GUARD_STATS,
    PromptContext,
    footer_fact_ids,
    guard_operations,
)
from palinode.prompts import PromptUnavailable, prompt_body, resolve_prompt

logger = logging.getLogger("palinode.consolidation")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)

def _git_commit(message: str, files: list[str] | None = None) -> None:
    """Commit consolidation mutations through the git_tools choke point.

    One-mutation-one-commit: ``files`` is the explicit list of memory files this
    pass mutated; each gets its own per-file commit so a consolidation touching
    N files produces N commits, never a repo-wide ``git add *.md`` sweep that
    would conflate unrelated working-tree edits under one message. The
    per-file ``message`` is suffixed with the file basename for blameability.

    ``files=None`` is retained only for callers with nothing concrete to stage;
    it is a no-op (we never sweep the repo). All real consolidation/ttl callers
    pass an explicit list.
    """
    if not config.git.auto_commit:
        return
    if not files:
        return
    # De-duplicate while preserving order — a project and its history sibling
    # may be listed more than once across a multi-project pass.
    seen: set[str] = set()
    for file_path in files:
        if file_path in seen or not os.path.exists(file_path):
            continue
        seen.add(file_path)
        base = os.path.basename(file_path)
        git_tools.commit_memory_file(file_path, f"{message} [{base}]")


def _touched_files(target: str) -> list[str]:
    """Files a single project compaction may have mutated.

    The op target itself plus its ``-history.md`` sibling, which the executor
    appends to on SUPERSEDE/ARCHIVE/RETRACT. Mirrors the path derivation in
    ``executor.append_to_history`` so a history append is committed alongside
    its parent mutation rather than swept up later.
    """
    base = re.sub(r"-status\.md$", "", target)
    base = re.sub(r"\.md$", "", base)
    history_path = f"{base}-history.md"
    touched = [target]
    if os.path.exists(history_path):
        touched.append(history_path)
    return touched

def _get_decisions_for_project(project_id: str) -> list[dict]:
    """Fetch the decisions still governing a specific project.

    Governing means usable under :func:`palinode.core.lifecycle.eligibility`
    — the same classifier the session-start digest selects through. A
    decision archived in place (``status: archived`` under ``decisions/``),
    superseded, deprecated, retracted, or past its ``expires_at`` is withheld;
    a legacy decision with no ``status`` at all is unmarked and stays in, as
    it always has.
    """
    decisions_dir = os.path.join(config.memory_dir, "decisions")
    if not os.path.exists(decisions_dir):
        return []

    active_decisions = []
    for filepath in glob.glob(os.path.join(decisions_dir, "*.md")):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            parts = content.split("---")
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    if not isinstance(meta, dict):
                        continue
                    entities = meta.get("entities", [])
                    if f"project/{project_id}" not in entities:
                        continue
                    rel = os.path.relpath(filepath, config.memory_dir)
                    elig = eligibility(meta, path=rel)
                    if elig.retired:
                        logger.debug(
                            "palinode.consolidation: %s is retired (%s) — not a "
                            "governing decision for %r", rel, elig.reason, project_id,
                        )
                        continue
                    active_decisions.append({
                        "id": meta.get("id"),
                        "name": meta.get("name"),
                        # The memory's on-disk identity, `category/slug` —
                        # the only form a typed link accepts. The
                        # frontmatter `id` is `category-slug`, which is not
                        # a ref, so a model told to cite one had nothing
                        # citable in the prompt to copy.
                        "ref": "decisions/" + os.path.splitext(
                            os.path.basename(filepath)
                        )[0],
                        "content": parts[2].strip()
                    })
                except Exception as _parse_exc:
                    # Silent skip was hiding corrupt frontmatter — log so
                    # operators can find and fix bad files.
                    # Recovery: run `palinode lint` to surface all parse errors.
                    logger.warning(
                        "palinode.consolidation: YAML parse failed in %r — "
                        "skipping for project decision lookup (run `palinode lint` "
                        "to find all bad files): %s",
                        filepath, _parse_exc,
                    )
                    continue
    return active_decisions

#: Prompt budget for the decision context. Decisions are supplied in full-ish
#: because a truncated constraint is worse than none — the model would see half
#: a rule and treat it as the whole rule — but a project with a long decision
#: record still must not crowd out the notes it is meant to compact.
MAX_DECISIONS_CHARS = 2000
_MAX_DECISION_CHARS = 500


def _format_active_decisions(project_id: str) -> str:
    """Render this project's active decisions for the compaction prompt.

    Returns an empty string when there are none, so the caller can omit the
    section entirely rather than send an empty heading.

    The lookup itself (``_get_decisions_for_project``) already excludes
    retired decisions — archived in place, superseded, deprecated, retracted
    or expired — which are exactly the things the compactor must NOT treat as
    binding.
    """
    return _render_active_decisions(project_id)[0]


def _render_active_decisions(project_id: str) -> tuple[str, list[str]]:
    """``(rendered text, refs of the decisions actually rendered)``.

    The refs come from the same loop that writes the text, budget cut-off
    included, so the propose-side guard's notion of "in the prompt" cannot
    drift from what the model was shown.
    """
    try:
        decisions = _get_decisions_for_project(project_id)
    except Exception as exc:  # pragma: no cover — defensive
        # Context is an improvement to the proposal, never a precondition for
        # it. A malformed decisions/ directory must not stop a compaction pass.
        logger.warning(
            "palinode.consolidation: could not load decisions for %r "
            "(compacting without decision context): %s",
            project_id, exc,
        )
        return "", []

    parts: list[str] = []
    refs: list[str] = []
    total = 0
    for d in decisions:
        title = d.get("name") or d.get("id") or "decision"
        body = (d.get("content") or "").strip()
        if len(body) > _MAX_DECISION_CHARS:
            body = body[:_MAX_DECISION_CHARS].rstrip() + " …[truncated]"
        # The ref is rendered alongside the title because PROPOSE_CONTRADICTS
        # takes `category/slug` refs: a conflict the model can see but cannot
        # name is a conflict it cannot record.
        ref = d.get("ref")
        head = f"- **{title}** (ref: {ref})" if ref else f"- **{title}**"
        entry = f"{head}: {body}" if body else head
        if total + len(entry) > MAX_DECISIONS_CHARS:
            parts.append(
                f"- …and {len(decisions) - len(parts)} more decision(s) not shown"
            )
            break
        parts.append(entry)
        if ref:
            refs.append(ref)
        total += len(entry)

    return "\n".join(parts), refs


def _note_ref(filepath: str | None) -> str | None:
    """A note's ``category/slug`` memory ref, or ``None`` if it has none.

    Derived from its path under ``memory_dir`` — the on-disk identity a typed
    link accepts, the same derivation ``_get_decisions_for_project`` uses for
    a decision's ref. A note outside the store (or with a path no link would
    accept, or none at all) yields ``None`` rather than a ref nothing could
    resolve.
    """
    if not filepath:
        return None
    rel = os.path.relpath(filepath, config.memory_dir)
    if rel.startswith(".."):
        return None
    ref = os.path.splitext(rel)[0].replace(os.sep, "/")
    try:
        from palinode.core.typed_links import TypedLinkError, normalize_link_refs

        return normalize_link_refs([ref], "ref")[0]
    except TypedLinkError:
        return None


_CONSOLIDATION_SKIP_DIRS = {"daily", "archive", "inbox", "logs", "prompts", "specs"}


#: Default corpus for the weekly consolidation pass.
#:
#: ``daily/`` is the ephemeral capture stream consolidation was built for.
#: ``insights/`` is included because a store built the documented way puts its
#: durable findings there, and leaving them out meant the executor never saw
#: the memories most worth consolidating — the whole defect this default is
#: correcting. The weekly lookback (7 days) bounds each pass to recently
#: touched files rather than the whole corpus.
#:
#: The nightly pass deliberately does NOT use this — see ``run_nightly``.
DEFAULT_CONSOLIDATION_SOURCES: tuple[str, ...] = ("daily", "insights")

#: What the nightly pass reads. Nightly is the lightweight "what happened
#: today" sweep; insights are not a daily activity stream, so widening the
#: weekly default must not silently widen this one too.
NIGHTLY_CONSOLIDATION_SOURCES: tuple[str, ...] = ("daily",)

#: Filenames shaped ``YYYY-MM-DD.md``. Daily notes carry their date in the
#: filename; every other memory carries it in frontmatter.
_DATED_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _note_date(filepath: str, meta: dict) -> str:
    """The date a note is filed under, as ``YYYY-MM-DD``.

    Daily notes are named for their date, and that is authoritative — reading
    frontmatter for them would change long-standing behaviour. Typed memories
    (Insight, Decision, ProjectSnapshot…) are not date-named, so their date
    comes from frontmatter, falling back to mtime.

    Without this, a typed memory's filename fails the cutoff's string
    comparison in whichever direction its first characters happen to sort —
    silently including or excluding it rather than honouring the lookback.
    """
    stem = os.path.basename(filepath)
    dated = _DATED_FILENAME.match(stem)
    if dated:
        return dated.group(1)

    for key in ("last_updated", "created_at", "date"):
        value = meta.get(key)
        if value:
            text = str(value)
            if _DATED_FILENAME.match(text):
                return text[:10]

    return datetime.fromtimestamp(os.path.getmtime(filepath), UTC).strftime("%Y-%m-%d")


def _modified_at(filepath: str) -> datetime:
    """When the file was last written, as a timezone-aware UTC timestamp.

    The nightly's watermark compares against this rather than against the
    note's date: a ``YYYY-MM-DD`` string is coarser than the timestamps it
    filters and its boundary is UTC midnight, which is the middle of the
    working day in most of the world. A file appended to after its own date
    has passed — a session that ran past midnight UTC, a backdated capture —
    is invisible to the date comparison and obvious to this one.
    """
    return datetime.fromtimestamp(os.path.getmtime(filepath), UTC)


def _collect_daily_notes(
    lookback_days: int | None = None,
    sources: Sequence[str] | None = None,
    since: datetime | None = None,
) -> tuple[list[dict], int]:
    """Collect recent notes from the selected corpora.

    ``sources`` names directories under ``memory_dir`` to scan. This function
    previously scanned ``daily/`` unconditionally, which made the deterministic
    executor — the architecture's headline differentiator — unreachable for
    memories saved the documented way: a store full of typed Insights
    consolidated to ``{"status": "no notes found"}`` because none of them live
    in ``daily/``.

    The default is now ``DEFAULT_CONSOLIDATION_SOURCES``, which includes
    ``insights/``; pass ``sources`` explicitly to narrow or widen it.

    Two selectors, one per pass. ``lookback_days`` is the weekly's calendar
    window and is unchanged. ``since`` is the nightly's watermark: a file is
    collected when it was *written* after that moment, with no date arithmetic
    anywhere in the comparison. Passing ``since`` ignores ``lookback_days``.

    Every note carries ``modified_at`` regardless, so the caller can filter
    further — the nightly does, per project, since one collection serves
    several marks.

    Returns:
        Tuple of (notes list, skipped_count) where skipped_count is the
        number of files whose YAML frontmatter failed to parse.
        Callers surface skipped_count in the consolidation run summary so
        operators know to run ``palinode lint``.
    """
    selected = tuple(sources) if sources else DEFAULT_CONSOLIDATION_SOURCES

    cutoff_date = (
        ""
        if since is not None
        else (_utc_now() - timedelta(days=lookback_days or 0)).strftime("%Y-%m-%d")
    )
    notes = []
    skipped = 0
    candidates: list[str] = []

    for source in selected:
        source_dir = os.path.join(config.memory_dir, source)
        if not os.path.exists(source_dir):
            continue
        candidates.extend(glob.glob(os.path.join(source_dir, "*.md")))

    if not candidates:
        return [], 0

    for filepath in candidates:
        meta: dict = {}
        modified_at = _modified_at(filepath)

        if since is not None:
            # The watermark path: one stat, no date parsing, and the file is
            # not opened unless it was written after the mark.
            if modified_at <= since:
                continue
        else:
            # Fast path, and the reason weekly behaviour is unchanged: a
            # date-named file older than the cutoff is rejected without being
            # opened, exactly as before. Only files whose date must come from
            # frontmatter get read in order to be filtered.
            named = _DATED_FILENAME.match(os.path.basename(filepath))
            if named and named.group(1) < cutoff_date:
                continue

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    if not isinstance(meta, dict):
                        meta = {}
                    content = parts[2].strip()
                except Exception as _parse_exc:
                    # Silent pass was hiding corrupt frontmatter — log so
                    # operators can find and fix bad files.
                    # Recovery: run `palinode lint` to surface all parse errors.
                    logger.warning(
                        "palinode.consolidation: YAML parse failed in %r — "
                        "frontmatter ignored, body text still collected "
                        "(run `palinode lint` to find all bad files): %s",
                        filepath, _parse_exc,
                    )
                    skipped += 1
                    # body text is kept — better to collect partial content
                    # than silently drop the note.
                    content = parts[2].strip() if len(parts) >= 3 else content

        date_str = _note_date(filepath, meta)
        if since is None and date_str < cutoff_date:
            continue

        # Frontmatter `entities:` is the reliable signal for a typed memory —
        # it is what `palinode_save` records, whereas the regex below only sees
        # refs a human happened to write into the body. Daily notes rarely carry
        # it, so the two are unioned rather than one replacing the other.
        declared = meta.get("entities") or []
        if isinstance(declared, str):
            declared = [declared]
        mentions = {
            str(e)
            for e in declared
            if isinstance(e, (str, int)) and str(e).startswith(("project/", "person/"))
        }
        mentions.update(re.findall(r"(project/[\w-]+|person/[\w-]+)", content))
        mentions = list(mentions)

        # Fallback: detect projects by keyword if no entity refs found
        if not any(m.startswith("project/") for m in mentions):
            keyword_map = config.consolidation.keyword_map or {
                "project/palinode": ["Palinode", "palinode", "memory system", "SQLite-vec", "BGE-M3", "palinode_search"],
            }
            content_lower = content.lower()
            for project_ref, keywords in keyword_map.items():
                if any(kw.lower() in content_lower for kw in keywords):
                    mentions.append(project_ref)

        notes.append({
            "filepath": filepath,
            "date": date_str,
            "modified_at": modified_at,
            "content": content,
            "mentions": mentions
        })

    return sorted(notes, key=lambda x: x["date"]), skipped

def _group_by_project(daily_notes: list[dict]) -> dict[str, list[dict]]:
    """Group daily notes by the projects they mention."""
    groups = {}
    for note in daily_notes:
        for m in note["mentions"]:
            if m.startswith("project/"):
                pid = m.split("project/")[1]
                if pid not in groups:
                    groups[pid] = []
                groups[pid].append(note)
    return groups


def _notes_written_between(
    earlier: datetime, later: datetime, sources: Sequence[str]
) -> int:
    """How many note files in ``sources`` were written in ``(earlier, later]``.

    Only called when the catch-up floor clamps a mark, to put a number on what
    the clamp excluded. A file count rather than a per-project count: the
    excluded files were never read, so nothing here knows which projects they
    mention, and claiming otherwise would be a guess dressed as a report.
    """
    total = 0
    for source in sources:
        source_dir = os.path.join(config.memory_dir, source)
        if not os.path.exists(source_dir):
            continue
        for filepath in glob.glob(os.path.join(source_dir, "*.md")):
            if earlier < _modified_at(filepath) <= later:
                total += 1
    return total


def _select_by_watermark(
    grouped: dict[str, list[dict]],
    *,
    marks: dict[str, datetime],
    cold_start_at: datetime,
    floor: datetime,
    catchup_days: int,
    sources: Sequence[str],
    resumes: dict[str, watermark.Resume] | None = None,
) -> tuple[dict[str, list[dict]], list[str], dict[str, watermark.Resume]]:
    """Narrow each group to the notes written since *that project's* mark.

    The collection step is bounded by the catch-up floor, which is the oldest
    any mark may be; this is where the per-project part happens. A group left
    with no notes is dropped — the project has nothing new, and an empty
    prompt is a wasted inference.

    A project whose last pass stopped part-way (``resumes``) selects from its
    resume position instead: the unfinished note and everything after it in
    ``(modified_at, path)`` order, ties included.

    Returns the surviving groups, the ids whose mark the floor clamped, and
    the resume positions that still apply (a clamped one does not).
    A clamp is a WARNING with the gap it skipped in it: one abandoned project
    must not hand the model months of notes, and the bound that prevents that
    must not do it silently.
    """
    kept: dict[str, list[dict]] = {}
    clamped: list[str] = []
    applied: dict[str, watermark.Resume] = {}
    resumes = resumes or {}
    for project_id, notes in grouped.items():
        start = watermark.since_for(
            project_id, marks=marks, cold_start_at=cold_start_at, floor=floor
        )
        resume = resumes.get(project_id)
        if start.clamped_from is not None:
            clamped.append(project_id)
            # A resume position's own note sits *at* the mark, and the gap
            # count below is exclusive of its lower end.
            gap_from = (
                start.clamped_from - timedelta(microseconds=1)
                if resume is not None
                else start.clamped_from
            )
            logger.warning(
                "palinode.consolidation: %s — its watermark (%s) is older than the "
                "%d-day catch-up bound, so this pass starts at %s instead and %d "
                "note file(s) written in that gap are not selected. Widen the bound "
                "(consolidation.nightly.lookback_days, or --days N on the cron line) "
                "and re-run to cover them.",
                project_id,
                watermark.stamp(start.clamped_from),
                catchup_days,
                watermark.stamp(floor),
                _notes_written_between(gap_from, floor, sources),
            )
            resume = None
        if resume is not None and resume.modified_at == start.since:
            position = (resume.modified_at, resume.path)
            fresh = [note for note in notes if _progress_key(note) >= position]
            applied[project_id] = resume
        else:
            fresh = [note for note in notes if note["modified_at"] > start.since]
        if fresh:
            kept[project_id] = fresh
        else:
            logger.debug(
                "palinode.consolidation: %s — nothing written since its watermark "
                "(%s, %s); not sent to the model",
                project_id,
                watermark.stamp(start.since),
                start.source,
            )
    return kept, sorted(clamped), applied


def _target_file_for(project_id: str) -> str | None:
    """The file this project's compaction writes into, or ``None`` if it has none.

    Prefers the status layer, which is the fast-changing one, and falls back to
    the project file. ``None`` means neither exists.

    A group whose subject has no project document is a **skip, not an error**.
    An entity ref like ``project/searxng`` appearing in one insight does not
    imply the store wants a ``projects/searxng.md``, and consolidation is not
    in the business of creating documents — it compacts into existing ones.
    """
    projects_dir = os.path.join(config.memory_dir, "projects")
    status_file = os.path.join(projects_dir, f"{project_id}-status.md")
    if os.path.exists(status_file):
        return status_file
    project_file = os.path.join(projects_dir, f"{project_id}.md")
    if os.path.exists(project_file):
        return project_file
    return None


def _partition_by_target(
    grouped: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], list[str]]:
    """Split groups into those with a target document and those without.

    Filtering here rather than letting the target read raise means a subject
    with no project document is a *reported skip* rather than a caught
    exception buried in the log. The run summary previously said
    ``status: success`` while every such group produced nothing, so the count
    below is the part that makes the result honest.
    """
    keep: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for project_id, notes in grouped.items():
        if _target_file_for(project_id) is None:
            skipped.append(project_id)
        else:
            keep[project_id] = notes
    return keep, sorted(skipped)


#: What an operator has to run to make an untagged target consolidatable.
UNTAGGED_REMEDIATION = (
    "run `palinode bootstrap-ids --file projects/<project>-status.md` (or "
    "`palinode bootstrap-ids` for the whole store) to mint ids for the bullets "
    "already there"
)


def _tagged_fact_count(target_file: str) -> int:
    """How many body bullets in *target_file* the runner can actually address.

    Counted with the same regex ``_consolidate_project`` harvests with, so this
    cannot answer "there are facts" for a file that harvests to nothing.
    """
    return count_body_facts(target_file)[1]


def _partition_by_tagged_facts(
    grouped: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], list[str]]:
    """Split groups whose target carries no ``<!-- fact:… -->`` marker out of the pass.

    Sibling of :func:`_partition_by_target`, and it exists for the same reason
    one layer in: a target document with no markers harvests to zero facts, so
    the pass proposes nothing for that project no matter how much activity its
    daily notes hold — and that outcome was reported as ``projects_compacted: 0``
    under ``status: success``, indistinguishable from a quiet week. Measured on
    a real store: 79 consecutive nightly runs skipped a 449-bullet status
    document appended entirely by session-end, every one of them "successful".

    Partitioning *here* rather than inside ``_consolidate_project`` also means
    the skip costs no prompt render and no inference, and puts the count where
    the run summary can report it.
    """
    keep: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for project_id, notes in grouped.items():
        target = _target_file_for(project_id)
        if target is not None and _tagged_fact_count(target) == 0:
            skipped.append(project_id)
        else:
            keep[project_id] = notes
    return keep, sorted(skipped)


def _log_untagged_skips(skipped: list[str]) -> None:
    """WARNING, not INFO: an inert consolidation target is a defect in the store.

    The old line was INFO from inside ``_consolidate_project`` and read as
    routine — which is how six months of it went unread in the cron log. The
    message names the files (what to fix) and the command (how).
    """
    if not skipped:
        return
    targets = ", ".join(
        f"{project_id} ({_target_file_for(project_id) or 'no target'})"
        for project_id in skipped
    )
    logger.warning(
        "palinode.consolidation: %d group(s) skipped — the target document "
        "carries no <!-- fact:... --> markers, so consolidation has nothing to "
        "address and proposes nothing: %s. To fix, %s.",
        len(skipped),
        targets,
        UNTAGGED_REMEDIATION,
    )


def _record_skips(
    result: dict[str, Any],
    *,
    no_target: list[str],
    untagged: list[str],
) -> None:
    """Annotate a run summary with both skip classes.

    Present-or-absent rather than zero, matching how ``yaml_parse_errors``
    behaves. The two classes stay separate keys because their remediations
    differ: a no-target group wants a project document created (or the ref
    dropped), an untagged one wants ids minted in a document that already
    exists.
    """
    if no_target:
        result["groups_skipped_no_target"] = len(no_target)
        result["skipped_no_target_projects"] = no_target
    if untagged:
        result["groups_skipped_untagged"] = len(untagged)
        result["skipped_untagged_projects"] = untagged
        result["untagged_remediation"] = UNTAGGED_REMEDIATION


def _build_model_chain() -> list[dict[str, str]]:
    """Build ordered chain from config: primary + fallbacks.

    Returns list of {"model": ..., "url": ...} dicts.
    Primary is always first.
    """
    chain = [{"model": config.consolidation.llm_model, "url": config.consolidation.llm_url}]
    for fb in getattr(config.consolidation, "llm_fallbacks", []):
        chain.append({"model": fb["model"], "url": fb["url"]})
    return chain


def _call_llm_with_fallback(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    """Call the consolidation LLM with fallback chain.

    Tries primary model first. On timeout or HTTP error, tries each
    fallback in order. Returns (response_text, model_used).

    ``response_text`` is a
    :class:`~palinode.core.ollama_client.ChatCompletionText` — a ``str`` that
    also knows whether the model was cut off at ``llm_max_tokens``
    (``.truncated`` / ``.finish_reason``). The chain deliberately does *not*
    walk to the next model on truncation: that is not a host failure, and the
    next host would hit the same cap on the same prompt. The caller reports it.

    Raises:
        RuntimeError: All models in chain failed.
    """
    chain = _build_model_chain()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    client = get_ollama_client()

    last_error = None
    for i, endpoint in enumerate(chain):
        try:
            # Phase 4: route through the centralized client (CONSOLIDATION
            # role). retries=0 — the fallback chain itself is the retry strategy,
            # so the client shouldn't re-hammer each (slow, 600 s) host.
            result = client.chat_completions(
                messages,
                model=endpoint["model"],
                base_url=endpoint["url"],
                temperature=config.consolidation.llm_temperature,
                max_tokens=config.consolidation.llm_max_tokens,
                timeout=600.0,
                retries=0,
                role=OllamaRole.CONSOLIDATION,
            )
            if i > 0:
                logger.info(f"Fallback model succeeded: {endpoint['model']} @ {endpoint['url']} (primary failed)")
            return result, endpoint["model"]

        except OllamaError as e:
            last_error = e
            logger.warning(f"Model {endpoint['model']} @ {endpoint['url']} failed: {e}")
            continue

    raise RuntimeError(f"All {len(chain)} models failed. Last error: {last_error}")

#: ``model_used`` sentinel returned by ``_consolidate_project`` when the propose
#: step produced nothing usable — the call raised, the response was cut off at
#: the token cap, or what came back could not be parsed as an operations array.
#: The runners read it to count the group as failed rather than as a quiet
#: "nothing to do" — the two are indistinguishable by the (empty) operations
#: list alone. The *reason* is logged at WARNING where it is detected, next to
#: the project name, rather than threaded through this one-word return.
LLM_FAILED = "failed"


def _run_status(failed_projects: list[str]) -> str:
    """``success`` only when no project group failed; ``partial`` otherwise."""
    return "partial" if failed_projects else "success"


def _record_gate_run(
    mode: str, result: dict[str, Any], started_at: datetime, *, dry_run: bool
) -> None:
    """Stamp the activity gate's ``mode`` clock after a real, fully successful pass.

    The stamp is the pass's *start*, so the state file carries the tick that
    fired it: a completion stamp lands a minute after the tick, and the next
    day's tick then arrives a minute short of ``min_hours_elapsed`` and defers
    — the nightly silently became every other night that way. A partial pass
    (``projects_failed > 0`` without raising) leaves the clock alone so the next
    tick retries the failed group, the same contract a pass that raised gets by
    never reaching this point. A dry run changed nothing and records nothing.
    """
    from palinode.consolidation import activity_gate

    if dry_run:
        return
    if result.get("projects_failed"):
        # Named in the cron log so "why did it run again this morning" is
        # answerable from the log alone.
        logger.info(
            "palinode.consolidation: %s pass was partial — %d project group(s) "
            "failed (%s); leaving the activity gate's clock alone so the next "
            "tick retries them",
            mode,
            result["projects_failed"],
            ", ".join(result.get("failed_projects") or []) or "unnamed",
        )
        return
    if result.get("notes_pending"):
        # Pending input is work the pass knows it has not done. Stamping the
        # clock would let a quiet week defer it until the catch-up bound
        # clamps it away; leaving it lets the next tick continue.
        logger.info(
            "palinode.consolidation: %s pass left %d note(s) pending — more than "
            "its prompts held; leaving the activity gate's clock alone so the "
            "next tick continues",
            mode,
            result["notes_pending"],
        )
        return
    activity_gate.record_run(mode, now=started_at)


def _advance_watermarks(
    result: dict[str, Any], started_at: datetime, *, dry_run: bool
) -> None:
    """Move the mark of every project this nightly pass actually resolved.

    Resolved means the model saw the project's notes and the pass reached a
    decision about them: compacted, proposed nothing, or had every proposal
    removed by ``allowed_ops``. Everything else is deliberately absent, and
    each absence is the self-heal working — a group whose call failed, was cut
    off at the token cap, or raised inside the executor keeps its old mark and
    is re-selected next run. So does a group with no target document or no
    fact markers: nothing ever read those notes.

    "Saw" is literal. A group whose prompt could not carry all of its notes
    resolved only the contiguous prefix it was shown, and its mark records a
    resume position at the end of that prefix (``watermark_resume``) instead
    of the pass's start — an empty proposal must not acknowledge text the
    model never received. When a pass sends a project several prompts, the
    position is the end of the last one that resolved.

    Stamped with the pass's *start* for the same reason ``_record_gate_run``
    is: a note written during the pass was not in its selection.

    A dry run advances nothing. Neither does a deferred run (the gate returns
    before the pass) or one refused by the run lock (it raises before the pass
    starts), which is why neither needs a check here.
    """
    if dry_run:
        return
    resolved = [str(project) for project in result.get("projects_resolved") or []]
    # A project whose first prompts resolved and a later one failed is in
    # both lists; its mark moves to the end of the last resolved prompt.
    held = sorted(
        (
            set(result.get("failed_projects") or [])
            | set(result.get("skipped_no_target_projects") or [])
            | set(result.get("skipped_untagged_projects") or [])
        )
        - set(resolved)
    )
    if held:
        logger.info(
            "palinode.consolidation: nightly watermark held for %d project(s) — "
            "their notes are selected again next run: %s",
            len(held),
            ", ".join(held),
        )
    if not resolved:
        return
    # A resolved project whose prompt could not carry its whole selection
    # resolved only the prefix it was shown, so its mark moves to where that
    # prefix ends rather than to the pass's start.
    resume: dict[str, watermark.Resume] = {}
    for project_id, raw in (result.get("watermark_resume") or {}).items():
        position = watermark.Resume.from_state(raw)
        if position is not None and project_id in resolved:
            resume[project_id] = position
    advanced = watermark.advance(resolved, started_at, resume=resume)
    if advanced:
        result["watermark_advanced"] = advanced
        result["watermark_at"] = watermark.stamp(started_at)
        logger.info(
            "palinode.consolidation: nightly watermark advanced to %s for %d "
            "project(s): %s",
            result["watermark_at"],
            len(advanced),
            ", ".join(advanced),
        )
        for project_id in sorted(set(advanced) & set(resume)):
            position = resume[project_id]
            logger.info(
                "palinode.consolidation: %s — mark records a resume position "
                "instead (%s, %s, character %d): the notes after it were not "
                "presented in full",
                project_id,
                watermark.stamp(position.modified_at),
                position.path,
                position.offset,
            )


def _record_run_outcome(
    mode: str,
    result: dict[str, Any] | None,
    started_at: datetime,
    *,
    dry_run: bool,
    lookback_days: int,
    error: BaseException | None = None,
) -> None:
    """Append the pass's outcome to the gate's state file, success or not.

    The companion to ``_record_gate_run``, which stamps the clock only on a
    full success and therefore cannot say that a pass *failed*. This records
    every real pass — ``success``, ``partial``, the idle statuses, and
    ``error`` when the pass raised — so ``palinode doctor`` can report the
    last outcome and count a failure streak instead of leaving that to whoever
    reads the cron log. A dry run consolidated nothing and records nothing:
    a dry-run "success" written here would reset a real streak.
    """
    from palinode.consolidation import activity_gate

    if dry_run:
        return
    if error is not None:
        status = "error"
        failed: list[str] = []
    else:
        status = str((result or {}).get("status") or "unknown")
        failed = [str(project) for project in (result or {}).get("failed_projects") or []]
    activity_gate.record_outcome(
        mode,
        started_at=started_at,
        finished_at=activity_gate._utc_now(),
        status=status,
        failed_projects=failed,
        lookback_days=lookback_days,
        error=f"{type(error).__name__}: {error}" if error is not None else None,
    )


class ArchivePartition(NamedTuple):
    """Where each collected note goes once the pass has run.

    One bucket retires; the other three stay in place, each for a reason the
    run summary and the left-in-place warning report separately — see
    :func:`_partition_notes_for_archive`.
    """

    retire: list[dict]
    unresolved: list[dict]
    no_project: list[dict]
    today: list[dict]

    @property
    def left(self) -> list[dict]:
        return self.unresolved + self.no_project + self.today


def _is_todays_daily_note(filepath: str, today: str) -> bool:
    """``daily/<today>.md`` — the file ``POST /session-end`` is still appending to."""
    return (
        os.path.basename(filepath) == f"{today}.md"
        and os.path.basename(os.path.dirname(filepath)) == "daily"
    )


def _partition_notes_for_archive(
    notes: list[dict], unresolved: set[str], today: str | None = None,
) -> ArchivePartition:
    """Decide, per note, whether the pass may retire it to ``archive/``.

    A note is retired only when every project group it belongs to reached a
    decision: it was compacted, or the model saw the notes and proposed
    nothing. Three classes stay in place, each reported on its own so the run
    summary can say why:

    - ``unresolved``: one of its groups failed, had no target document, or had
      no tagged facts. Per-note rather than per-run: a note that mentions two
      projects, one compacted and one whose LLM call failed, has not been
      consolidated, and archiving it would hide the failed half from every
      future run.
    - ``no_project``: it carries no ``project/`` reference even after the
      keyword fallback in ``_collect_daily_notes``, so it formed no group and
      no model ever saw it. The old rule retired it because an empty set of
      projects intersects nothing — which is how one weekly pass moved 49
      never-read insights to ``archive/`` as though consolidated.
    - ``today``: it is ``daily/<today>.md``, which ``POST /session-end`` keeps
      appending to until midnight UTC. Moving it mid-day splits the day across
      two files, and the activity gate — which counts sessions in ``daily/*.md``
      only — loses every session ended so far that day. Decided here rather
      than in the mover so the count and the warning come from one place.

    ``today`` is the UTC calendar day, the same clock the session-end writer
    names the file with; injectable for tests.
    """
    today = today or _utc_now().strftime("%Y-%m-%d")
    partition = ArchivePartition([], [], [], [])
    for note in notes:
        projects = {
            m.split("project/", 1)[1] for m in note["mentions"] if m.startswith("project/")
        }
        if _is_todays_daily_note(note["filepath"], today):
            partition.today.append(note)
        elif not projects:
            partition.no_project.append(note)
        elif projects & unresolved:
            partition.unresolved.append(note)
        else:
            partition.retire.append(note)
    return partition


def _log_notes_left_in_place(partition: ArchivePartition) -> None:
    """Name every note the pass did not retire, grouped by why.

    Today's daily note is the expected outcome of every mid-day run, so it is
    reported at INFO on its own line rather than swelling the WARNING — which
    stays reserved for the classes that mean something did not happen.
    """
    for note in partition.today:
        logger.info(
            "palinode.consolidation: kept today's daily note in place: daily/%s, "
            "still being written",
            os.path.basename(note["filepath"]),
        )
    unexpected = partition.unresolved + partition.no_project
    if not unexpected:
        return
    reasons = []
    for label, bucket in (
        ("project group failed, had no target, or had no tagged facts",
         partition.unresolved),
        ("no project/ reference, so no group ever saw them", partition.no_project),
    ):
        if bucket:
            names = ", ".join(os.path.basename(n["filepath"]) for n in bucket)
            reasons.append(f"{label}: {names}")
    logger.warning(
        "palinode.consolidation: %d note(s) left in place — %s",
        len(unexpected),
        "; ".join(reasons),
    )


def _log_no_op_groups(no_ops: list[str], all_ops_filtered: list[str]) -> None:
    """One INFO line per run naming the groups the model saw and left alone.

    Under implicit KEEP an empty proposal is the normal quiet-week outcome, not
    a fault — but a group that reached the model and came back with nothing
    used to vanish from both the log and the summary, which is how a run could
    read ``projects_compacted: 1, notes_archived: 65`` with no account of the
    other 64. The ``allowed_ops`` case is named separately: there the model did
    propose, and the operator's filter removed every op.
    """
    if not no_ops and not all_ops_filtered:
        return
    no_ops, all_ops_filtered = sorted(no_ops), sorted(all_ops_filtered)
    parts = []
    if no_ops:
        parts.append(
            f"{len(no_ops)} project group(s) proposed no operations: {', '.join(no_ops)}"
        )
    if all_ops_filtered:
        parts.append(
            f"{len(all_ops_filtered)} project group(s) had every proposed operation "
            f"removed by allowed_ops: {', '.join(all_ops_filtered)}"
        )
    logger.info("palinode.consolidation: %s", "; ".join(parts))


def _record_no_ops(
    result: dict[str, Any],
    *,
    no_ops: list[str],
    all_ops_filtered: list[str],
) -> None:
    """Annotate a run summary with the groups that decided to change nothing.

    Always present, unlike the skip keys: an empty list is itself the claim
    that every group proposed something, which the summary could not make
    before. Sorted, like the skip lists, so the report is stable across runs.
    """
    result["groups_no_ops"] = len(no_ops)
    result["projects_no_ops"] = sorted(no_ops)
    result["groups_all_ops_filtered"] = len(all_ops_filtered)
    result["projects_all_ops_filtered"] = sorted(all_ops_filtered)


def _read_prompt_body(prompt_path: str) -> str:
    """The prompt text a model should see — frontmatter stripped.

    Prompt files under ``specs/prompts/`` are memory files: they carry a
    ``version:``/``active:`` frontmatter block that the prompt-versioning API
    and `palinode doctor` read. That block is metadata *about* the prompt, not
    an instruction to the model, and sending it prepends a YAML document to the
    system prompt for no benefit. Files without frontmatter are returned whole,
    which is what every prompt did before any of them had one.

    The split itself is :func:`palinode.prompts.prompt_body`, shared with
    ``palinode prompt sync``, which hashes exactly these bytes to decide whether
    a store copy is a release's or the operator's.
    """
    with open(prompt_path, encoding="utf-8") as f:
        return prompt_body(f.read())


def _system_prompt(prompt_file: str) -> str:
    """The system prompt for a consolidation pass — store copy first.

    Resolution order, and why each rung is there:

    1. ``<memory_dir>/specs/prompts/<prompt_file>`` — the operator's copy.
       Editing prompts is a documented workflow, so a store copy always wins.
    2. ``<memory_dir>/specs/prompts/compaction.md`` — pre-existing behaviour
       for a store that predates the nightly prompt. Kept above the packaged
       rung deliberately: an operator who edited ``compaction.md`` and never
       had a nightly file was getting their own text, and a packaging change
       should not quietly take that away.
    3. the copy inside the installed package — the rung that did not exist,
       and whose absence meant a ``pip install`` could not consolidate at all.
    4. nothing: raise. Never a quiet "no operations", which the run summary
       cannot tell apart from a week with nothing to compact.

    Every rung goes through :func:`_read_prompt_body`, so frontmatter never
    reaches the model regardless of which copy was used.
    """
    store_dir = os.path.join(config.memory_dir, "specs", "prompts")
    for candidate in (prompt_file, "compaction.md"):
        store_path = os.path.join(store_dir, candidate)
        if os.path.exists(store_path):
            return _read_prompt_body(store_path)

    packaged_path, _ = resolve_prompt(prompt_file, config.memory_dir)
    logger.info(
        "palinode.consolidation: %s is not in the store (%s) — using the copy "
        "packaged with palinode (%s). `palinode init` provisions the store's "
        "prompts so you can edit them.",
        prompt_file, store_dir, packaged_path,
    )
    return _read_prompt_body(str(packaged_path))


#: Characters of note entries (heading + text) one compaction prompt carries.
MAX_NOTES_CHARS = 6000
#: The weekly pass's per-note clip. The nightly plans spans instead.
MAX_NOTE_CHARS = 1500


def _progress_path(filepath: str) -> str:
    """A note's path as the watermark records it: relative, ``/``-separated."""
    return os.path.relpath(filepath, config.memory_dir).replace(os.sep, "/")


def _progress_key(note: dict) -> tuple[datetime, str]:
    """The nightly's total order over notes: written-at, then path for ties."""
    return note["modified_at"], _progress_path(note["filepath"])


def _note_heading(note: dict) -> str:
    """A note entry's heading: its date, its ref, and — for a part — which part."""
    ref = _note_ref(note.get("filepath"))
    head = f"### {note['date']} (ref: {ref})" if ref else f"### {note['date']}"
    span = note.get("span")
    if span is not None:
        start, end, length = span
        if (start, end) != (0, length):
            head += f" [excerpt: characters {start}-{end} of {length}]"
    return head


class NightlyInput(NamedTuple):
    """What one project's nightly prompt carries, and what that leaves pending.

    ``notes`` are copies whose ``content`` is exactly the span presented, in
    ``(modified_at, path)`` order. ``resume`` is where the next pass must
    start if this one resolves — ``None`` when the plain mark (the pass's
    start) already covers everything this pass did not finish.
    """

    notes: list[dict]
    #: Every selected note's path, in presentation order.
    selection: tuple[str, ...]
    #: How many of ``notes`` are presented to their end.
    complete: int
    resume: watermark.Resume | None

    @property
    def selected(self) -> int:
        return len(self.selection)

    @property
    def presented(self) -> int:
        return len(self.notes)

    @property
    def pending(self) -> int:
        return self.selected - self.complete


def _plan_nightly_input(
    notes: list[dict], *, resume: watermark.Resume | None, until: datetime
) -> NightlyInput:
    """Fit a project's selection into one prompt as a contiguous prefix.

    Oldest first, in ``(modified_at, path)`` order, each note from where the
    last pass stopped inside it (``resume``) and for as much of the remaining
    budget as it needs. The plan stops at the first note it cannot finish, so
    what the model is shown is always a prefix of the selection and a
    resolution can acknowledge exactly that prefix. Every pass
    presents at least part of the first unfinished note, so a backlog of any
    size — including one note longer than the whole budget — is consumed.

    ``until`` is the pass's start. A stopping point written after it is not
    recorded: a note written during the pass may have been missed by the
    collection, and the plain mark at ``until`` already covers every note
    before the stopping point.
    """
    ordered = sorted(notes, key=_progress_key)
    planned: list[dict] = []
    total = 0
    complete = 0
    stop: tuple[tuple[datetime, str], int] | None = None
    for note in ordered:
        key = _progress_key(note)
        text = note["content"]
        length = len(text)
        start = 0
        if resume is not None and key == (resume.modified_at, resume.path):
            start = resume.offset if resume.offset <= length else 0
        whole = _note_heading({**note, "span": (start, length, length)})
        if total + len(whole) + 1 + (length - start) <= MAX_NOTES_CHARS:
            end = length
        else:
            # Sized with an excerpt marker at least as wide as the real one.
            widest = _note_heading({**note, "span": (start, length, length + 1)})
            room = MAX_NOTES_CHARS - total - len(widest) - 1
            if room <= 0:
                stop = (key, start)
                break
            end = start + room
        piece = {**note, "content": text[start:end], "span": (start, end, length)}
        planned.append(piece)
        total += len(_note_heading(piece)) + 1 + (end - start)
        if end < length:
            stop = (key, end)
            break
        complete += 1

    next_resume = None
    if stop is not None:
        (modified_at, path), offset = stop
        if modified_at <= until:
            next_resume = watermark.Resume(modified_at, path, offset)
    return NightlyInput(
        planned, tuple(note["filepath"] for note in ordered), complete, next_resume
    )


class AssembledPrompt(NamedTuple):
    """One group's user prompt and, as identifiers, what it contains."""

    user_prompt: str
    context: PromptContext


def _assemble_prompt(project_id: str, notes: list[dict]) -> AssembledPrompt | None:
    """Build the compaction user prompt for one project group.

    Returns ``None`` — after logging why — when there is nothing to build it
    from: no project document, or a document with no tagged facts. Callers
    filter such groups out before the passes reach here; this stays defensive
    for direct callers, and "nothing to do" is a skip, not an error.

    The :class:`PromptContext` is filled from the same loops that write the
    text, budget cut-offs included, so the propose-side guard's notion of "in
    the prompt" is exactly what the model was shown.
    """
    target_file = _target_file_for(project_id)
    if target_file is None:
        logger.info(
            "No project document for %r — nothing to compact into, skipping",
            project_id,
        )
        return None

    with open(target_file, encoding="utf-8") as f:
        file_content = f.read()

    # Extract facts with IDs — body only. A YAML frontmatter list entry uses the
    # same `- item` syntax, and harvesting one as a "fact" is what invited the
    # LLM to propose operations against `entities:`.
    _, file_body = split_frontmatter(file_content)
    facts = []
    for match in FACT_LINE_RE.finditer(file_body):
        facts.append({"id": match.group(2), "text": match.group(1).strip()})

    if not facts:
        logger.info(f"No tagged facts in {target_file}, skipping compaction")
        return None

    # A fact the executor already retired in place (a `~~struck~~ [superseded
    # …]` / `[RETRACTED …]` tombstone) is history, not current state. It stays
    # in the prompt — same id, same text, same marker, so the model can see
    # what was replaced and the guard still counts it as in context — but in
    # its own block, so EXISTING_FACTS is exactly the set of current claims.
    # The executor's renderings and the recognizer are pinned together in
    # tests; the executor itself is untouched.
    current_facts = [f for f in facts if not is_retired_fact_text(f["text"])]
    retired_facts = [f for f in facts if is_retired_fact_text(f["text"])]

    # Format for LLM
    facts_text = "\n".join(f"[{f['id']}] {f['text']}" for f in current_facts)
    retired_section = ""
    if retired_facts:
        retired_text = "\n".join(f"[{f['id']}] {f['text']}" for f in retired_facts)
        retired_section = (
            f"\n## RETIRED_FACTS ({len(retired_facts)} superseded or retracted — "
            f"history, not current state)\n\n{retired_text}\n"
        )

    # Each note is headed by its ref so the model has a `category/slug` it can
    # copy into `falsified_by` / `contradicts` — the same reason a decision's
    # ref is rendered beside its title. The refs of the notes that fit the
    # budget are what the guard later accepts as "in context".
    #
    # A note carrying ``span`` was sized by :func:`_plan_nightly_input`, which
    # owns the budget for it; it is rendered exactly as planned, because the
    # plan is what the nightly's watermark records as seen.
    notes_parts = []
    note_refs: list[str] = []
    total = 0
    for n in reversed(notes):
        ref = _note_ref(n.get("filepath"))
        if "span" in n:
            notes_parts.append(f"{_note_heading(n)}\n{n['content']}")
            if ref:
                note_refs.append(ref)
            continue
        entry = f"{_note_heading(n)}\n{n['content'][:MAX_NOTE_CHARS]}"
        if total + len(entry) > MAX_NOTES_CHARS:
            break
        notes_parts.append(entry)
        if ref:
            note_refs.append(ref)
        total += len(entry)
    notes_parts.reverse()
    note_refs.reverse()
    notes_text = "\n\n".join(notes_parts)

    # Active decisions for this project, as *constraints* on what may be
    # proposed — not as material to compact. Supplied without a lookback:
    # a decision does not stop governing because nobody touched its file this
    # week, which is the opposite of how a note ages.
    decisions_text, decision_refs = _render_active_decisions(project_id)
    decisions_section = (
        f"\n## ACTIVE_DECISIONS (governing this project)\n\n{decisions_text}\n"
        if decisions_text
        else ""
    )

    user_prompt = f"""## EXISTING_FACTS ({len(current_facts)} facts from {os.path.basename(target_file)})

{facts_text}
{retired_section}{decisions_section}
## RECENT_NOTES (last {config.consolidation.lookback_days} days)

{notes_text}

Return the operations JSON array."""

    context = PromptContext(
        fact_ids=frozenset(f["id"] for f in facts),
        footer_fact_ids=footer_fact_ids(file_body),
        decision_refs=frozenset(decision_refs),
        note_refs=tuple(note_refs),
    )
    return AssembledPrompt(user_prompt, context)


def _prompt_context_for(project_id: str, notes: list[dict]) -> PromptContext | None:
    """What the prompt for this group contained, for the propose-side guard.

    Rebuilt through :func:`_assemble_prompt` rather than threaded out of
    :func:`_consolidate_project`, whose ``(operations, model_used)`` contract
    its direct callers and their test doubles rely on. The assembly is
    deterministic on the same store state, and one extra read of the target
    document is nothing next to the model call it follows.
    """
    assembled = _assemble_prompt(project_id, notes)
    return assembled.context if assembled else None


def _consolidate_project(
    project_id: str,
    notes: list[dict],
    is_nightly: bool = False,
    llm_fn: LlmFn | None = None,
) -> tuple[list[dict], str]:
    """Consolidate a project by generating compaction operations.

    Reads the compaction prompt, extracts facts from the project file,
    sends both to the LLM, returns structured operations.

    Args:
        project_id: Project slug.
        notes: Recent daily notes mentioning this project.
        is_nightly: Use the lightweight nightly prompt.
        llm_fn: The propose seam. ``(system_prompt, user_prompt) ->
            (response_text, model_used)``. Defaults to the live fallback-chain
            caller; tests inject a fake returning deterministic op-JSON so the
            real fact-extraction + parse + executor path runs without an LLM.

    Returns:
        Tuple of (List of operation dicts, model_used). ``model_used`` is
        :data:`LLM_FAILED` when the propose step produced nothing usable — the
        call raised, the response was truncated, or it carried no readable
        operations array — so the caller counts the group as failed. An empty
        list with a real model name is the other thing entirely: the model
        looked and proposed nothing.

    The ops come back as proposed. The propose-side guard runs in the passes,
    against :func:`_prompt_context_for` — same assembly, no LLM.
    """
    # Load compaction prompt: store copy, then the copy inside the wheel.
    # PromptUnavailable propagates — the caller records the project as failed,
    # which is the honest report for "this pass could not run".
    system_prompt = _system_prompt(
        "nightly-consolidation.md" if is_nightly else "compaction.md"
    )

    assembled = _assemble_prompt(project_id, notes)
    if assembled is None:
        return [], "primary"
    user_prompt = assembled.user_prompt

    # Call LLM (via the injectable propose seam; default = live fallback chain).
    try:
        result_text, model_used = (llm_fn or _call_llm_with_fallback)(system_prompt, user_prompt)
    except Exception as e:
        logger.error(f"Failed to call LLM for {project_id}: {e}")
        return [], LLM_FAILED

    # A response cut off at the token cap is a failed proposal, not an empty
    # one. `getattr` because the propose seam is injectable: a test fake (or a
    # server that reports no finish_reason) hands back a plain str, which reads
    # as "not known to be truncated" — the parse check below is the backstop.
    if getattr(result_text, "truncated", False):
        logger.warning(
            "palinode.consolidation: %s — the model's response was cut off at the "
            "token cap (finish_reason=%s, consolidation.llm_max_tokens=%d, %d chars "
            "returned); proposing nothing for this project. Raise llm_max_tokens or "
            "reduce what the prompt asks the model to enumerate.",
            project_id, getattr(result_text, "finish_reason", None),
            config.consolidation.llm_max_tokens, len(result_text),
        )
        return [], LLM_FAILED

    # Parse the operations JSON array — extraction + json_repair recovery +
    # nested-list/dict filtering all live in op_parse now. An empty *list* is a
    # model that proposed nothing, which is a legitimate no-op; a response with
    # no readable array is a failure, and the two used to be the same `[]`.
    parsed = parse_result(result_text)
    if not parsed.ok:
        logger.warning(
            "palinode.consolidation: %s — could not read operations from the %s "
            "response: %s. Counting this project as failed; its notes stay in place.",
            project_id, model_used, parsed.reason,
        )
        return [], LLM_FAILED
    return parsed.operations, model_used


def _guarded(
    operations: list[dict],
    context: PromptContext | None,
    target: str,
    total_stats: dict[str, int],
) -> list[dict]:
    """Run the propose-side guard on a group's ops, folding its counts in.

    Runs before the pass's ``allowed_ops`` filter, so a RETRACT downgraded to
    PROPOSE_CONTRADICTS is then subject to that filter like any other op. A
    ``None`` context means no prompt could be built for the group, so there
    is nothing to check the ops against; they pass through as they are.
    """
    if context is None:
        return operations
    operations, guard_stats = guard_operations(operations, context, target=target)
    for key, value in guard_stats.items():
        total_stats[key] = total_stats.get(key, 0) + value
    return operations


def _empty_stats() -> dict[str, int]:
    """The run-summary counters every pass starts from."""
    stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    stats.update({key: 0 for key in GUARD_STATS})
    return stats

def _check_contradictions(
    new_items: list[dict], project_id: str, llm_fn: LlmFn | None = None
) -> list[dict]:
    """Check new items for contradictions against existing knowledge base.

    ``llm_fn`` is the same propose seam — defaults to the live caller;
    tests inject a fake returning a canned contradiction op so the embed/search
    + parse + translate path runs deterministically.
    """
    try:
        update_prompt_path, from_store = resolve_prompt("update.md", config.memory_dir)
    except PromptUnavailable as exc:
        # Not the vacuous-success shape: every candidate still becomes an ADD,
        # so the caller's work happens — only the contradiction check is lost,
        # and now it says so instead of degrading silently.
        logger.warning(
            "palinode.consolidation: no update.md prompt (%s) — adding %d item(s) "
            "for project %s without the contradiction check.",
            exc, len(new_items), project_id,
        )
        return [{"operation": "ADD", "item": item} for item in new_items]
    if not from_store:
        logger.info(
            "palinode.consolidation: update.md is not in the store — using the "
            "copy packaged with palinode (%s).",
            update_prompt_path,
        )
    system_prompt = _read_prompt_body(str(update_prompt_path))

    operations = []
    for item in new_items:
        try:
            emb = embedder.embed(item.get("content", ""))
        except embedder.EmbeddingUnavailable as e:
            # Batch/background path: a nightly consolidation run should not
            # abort a whole project over one backend hiccup. Degrade to the
            # same ADD-without-dedup outcome the old falsy `[]` produced — the
            # embedder already logged a WARNING with the real cause; this
            # DEBUG line adds the project/item context it can't see.
            logger.debug(
                "dedup check skipped for project=%s: embedder unavailable (%s)",
                project_id, e,
            )
            operations.append({"operation": "ADD", "item": item})
            continue
        if not emb:
            operations.append({"operation": "ADD", "item": item})
            continue

        # H1: consolidation dedup is an internal candidate lookup, not human
        # recall — use search_internal so recall_count / importance are never
        # bumped regardless of future refactors (ADR-015 H1).
        existing = store.search_internal(
            emb, category=item.get("category"), top_k=5, threshold=0.7,
        )

        if not existing:
            operations.append({"operation": "ADD", "item": item})
            continue

        user_prompt = f"""## Candidate
{json.dumps(item, indent=2)}

## Existing Similar Memories
{json.dumps(existing, indent=2)}

Return the operation as JSON."""

        try:
            result_text, model_used = (llm_fn or _call_llm_with_fallback)(system_prompt, user_prompt)

            json_match = re.search(r"```json\s*([\s\S]*?)```", result_text)
            if json_match:
                result_text = json_match.group(1)

            try:
                operation = json.loads(result_text)
                operation["item"] = item
                # The candidate rows the proposal was generated against. A
                # `target_id` can only name a fact the model saw here (or one
                # in the item itself), so the applier uses this set to find
                # the file that owns the id and the section hash it was
                # proposed against — the write-time equivalent of the
                # PromptContext the compaction passes hand to the guard.
                operation["candidates"] = existing
                operations.append(operation)
            except json.JSONDecodeError:
                operations.append({"operation": "ADD", "item": item, "reason": "LLM parse failed"})
        except Exception as e:
            logger.error(f"Contradiction check failed: {e}")
            operations.append({"operation": "ADD", "item": item, "reason": f"API error: {e}"})

    return operations

def _archive_daily_notes(notes: list[dict]) -> None:
    """Move processed daily notes to the archive directory.

    Through git_tools.move_memory_file (the choke point's move primitive)
    rather than a raw shutil.move, and committed here per-note: the old path
    stages as a deletion and the new path as an addition, in one commit —
    _git_commit (used for the compaction pass itself) filters out paths that
    no longer exist on disk, which would silently drop the deletion half of
    a move, so the archived pair cannot ride along with mutated_files there.
    """
    archive_dir = os.path.join(config.memory_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)

    for note in notes:
        try:
            date_prefix = note["date"][:4] # YYYY
            year_dir = os.path.join(archive_dir, date_prefix)
            os.makedirs(year_dir, exist_ok=True)
            old_path = note["filepath"]
            new_path = os.path.join(year_dir, os.path.basename(old_path))
            git_tools.move_memory_file(old_path, new_path)
            if config.git.auto_commit:
                git_tools.commit_memory_files(
                    [old_path, new_path],
                    f"{config.git.commit_prefix} archive daily note [{os.path.basename(old_path)}]",
                )
        except Exception as e:
            logger.error(f"Failed to archive note {note['filepath']}: {e}")

def _fact_ids_before_apply(file_path: str) -> set[str]:
    """Fact ids present in ``file_path`` before the executor runs.

    Captured pre-apply because ARCHIVE removes a fact's marker from the file —
    validating an ARCHIVE's ``fact_id`` against post-apply content alone would
    mark every legitimate archive unresolved.
    """
    if not os.path.exists(file_path):
        return set()
    with open(file_path, encoding="utf-8") as f:
        return status_doc.fact_ids(f.read())


#: Names the proposer of an age-retirement op wherever provenance is written —
#: the commit subject, the history entry's reason, the status document's log
#: line. A deterministic actor like ``lint``, not the model: nothing here is
#: proposed, judged or worded by an LLM.
AGE_RETENTION_SOURCE = "age-retention"


def _age_retention_days() -> int:
    """The configured status-log window in days; ``0`` disables the sweep."""
    try:
        return int(getattr(config.consolidation, "status_log_retention_days", 0) or 0)
    except (TypeError, ValueError):
        logger.warning(
            "palinode.consolidation: consolidation.status_log_retention_days is "
            "not an integer (%r) — age retirement disabled for this pass",
            getattr(config.consolidation, "status_log_retention_days", None),
        )
        return 0


def _aged_log_lines(
    target: str, *, now: datetime | None = None
) -> tuple[list[LogLine], int]:
    """``(lines this pass may retire on age alone, the window in days)``.

    Empty whenever the sweep must not run: the window is disabled, the file
    cannot be read, or — the rule that matters — the document is not
    *age-eligible* under :func:`palinode.consolidation.retirement.classify`.
    An identity or profile document retires by supersession or retraction and
    never by age (ADR-020), so the ops are never built for one. That is also
    why the executor's superseded-only guard cannot fire on this path: the
    only way to reach it is to have already asked the same classifier.
    """
    days = _age_retention_days()
    if days <= 0:
        return [], days
    try:
        with open(target, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning(
            "palinode.consolidation: could not read %s for age retirement: %s",
            target, exc,
        )
        return [], days

    from palinode.core.parser import parse_markdown

    try:
        metadata, _ = parse_markdown(content)
    except Exception:  # noqa: BLE001 — a garbled file is never swept by age
        metadata = {}
    policy, signal = retirement.classify(target, metadata)
    if policy != retirement.AGE_ELIGIBLE:
        logger.debug(
            "palinode.consolidation: %s is %s (%s) — age retirement does not "
            "apply to it", target, policy, signal,
        )
        return [], days

    _, body = split_frontmatter(content)
    cutoff = (now or _utc_now()) - timedelta(days=days)
    return older_than(body, cutoff), days


def _first_per_fact_id(lines: list[LogLine]) -> list[LogLine]:
    """*lines*, in document order, keeping the first line of each fact id.

    Duplicate ids are legacy on a store minted before the writers deduplicated
    them — nothing new mints one — so this de-duplicates the *ops*, not the
    document: the lines it drops are removed anyway, by the op that keeps the
    id's first occurrence.
    """
    seen: set[str] = set()
    first: list[LogLine] = []
    for line in lines:
        if line.fact_id in seen:
            continue
        seen.add(line.fact_id)
        first.append(line)
    return first


def _retire_aged_log_lines(
    target: str, *, dry_run: bool = False, now: datetime | None = None
) -> int:
    """Archive *target*'s stale dated log lines before the model is asked anything.

    A status document fed by session-end grows one ``- [YYYY-MM-DD] …`` line
    per session. Six months of them is a backlog the weekly pass cannot
    digest: on the dogfood store the model was shown 449 facts, nearly all of
    them stale log lines, and the honest proposal — one ARCHIVE with a
    rationale per line — ran past every workable token cap, so the pass failed
    and *nothing* was retired. Retiring a line because it is older than a
    configured window is arithmetic, not judgement, so it happens here, with
    no model involved, and the prompt is then built from what is left.

    Each line becomes an ordinary ``ARCHIVE`` op and goes through the same
    :func:`apply_operations` every other proposal does — the executor keeps
    the verbatim line in the ``-history.md`` sibling, so nothing is lost. The
    step commits on its own and writes **one** Consolidation Log line naming
    the range it retired, rather than one line per fact: a log that needed
    eliding to stay readable would be reporting the sweep as noise.

    Returns the number of lines retired (previewed, under ``dry_run``).
    """
    from palinode.consolidation.executor import apply_operations

    lines, days = _aged_log_lines(target, now=now)
    if not lines:
        return 0

    cutoff_date = ((now or _utc_now()) - timedelta(days=days)).strftime("%Y-%m-%d")
    if dry_run:
        logger.info(
            "palinode.consolidation: would retire %d status log line(s) older "
            "than %d days (dated before %s) from %s",
            len(lines), days, cutoff_date, target,
        )
        return len(lines)

    # One op per distinct id, in document order. A fact id is derived from the
    # line's text, so two byte-identical log lines carry the same id — and an
    # ARCHIVE retires every line its id names. Emitting one op per
    # *line* therefore sent a second op after the lines were already gone, and
    # the executor logged it, correctly, as `ARCHIVE unmatched`: six warnings
    # for five ids on the first live sweep. The lines are still counted — the
    # executor's `archived` counts lines, not ops.
    operations = [
        {
            "op": "ARCHIVE",
            "id": line.fact_id,
            "rationale": (
                f"{AGE_RETENTION_SOURCE}: status log line dated {line.date} is "
                f"older than {days} days; retired by policy "
                f"(consolidation.status_log_retention_days)"
            ),
        }
        for line in _first_per_fact_id(lines)
    ]
    pre_apply_ids = _fact_ids_before_apply(target)
    stats = apply_operations(target, operations)
    retired = stats.get("archived", 0)
    if not retired:
        return 0

    _update_status_summary(
        target,
        [{
            "op": "ARCHIVE_BEFORE",
            "before": cutoff_date,
            "rationale": (
                f"{AGE_RETENTION_SOURCE}: retired {retired} status log line(s) "
                f"older than {days} days; full text in the -history.md sibling"
            ),
        }],
        known_fact_ids=pre_apply_ids,
    )
    _git_commit(
        f"{config.git.commit_prefix} {AGE_RETENTION_SOURCE}: {retired} status "
        f"log line(s) older than {days}d",
        files=_touched_files(target),
    )
    logger.info(
        "palinode.consolidation: age retirement retired %d status log line(s) "
        "older than %d days (dated before %s) from %s",
        retired, days, cutoff_date, target,
    )
    return retired


def _loggable_operations(
    operations: list[dict], applied_merges: list[int],
    applied_ranges: list[int] | None = None,
) -> list[dict]:
    """Drop the MERGE proposals the executor did not apply.

    The Consolidation Log records what consolidation *did*, and every other op
    kind's line already survives a no-op honestly (a KEEP with no rationale
    emits nothing; an unresolvable id logs as unresolved). MERGE was the
    exception: a proposal that retired nothing — no fact moved, no history
    entry written — still left a ``[MERGE] …`` line claiming a merge happened.
    Gate that one kind on the executor's own report of which merges landed.

    ``ARCHIVE_BEFORE`` is gated the same way and for the same reason: it names
    a date rather than an id, so a range that matched nothing cannot be read
    as unresolved after the fact — the log line would be its only trace.

    Args:
        operations: The ops passed to ``apply_operations``, in order.
        applied_merges: Indices into ``operations`` of the MERGEs it applied.
        applied_ranges: Indices of the ARCHIVE_BEFOREs it applied. ``None``
            means the caller did not ask, so nothing is gated on it.
    """
    gated: dict[str, set[int]] = {"MERGE": set(applied_merges)}
    if applied_ranges is not None:
        gated["ARCHIVE_BEFORE"] = set(applied_ranges)

    def _landed(index: int, op: dict) -> bool:
        indices = gated.get(op_kind(op) or "KEEP")
        return indices is None or index in indices

    return [op for index, op in enumerate(operations) if _landed(index, op)]


def _update_status_summary(
    file_path: str,
    new_activity: list[dict],
    known_fact_ids: set[str] | None = None,
) -> None:
    """
    Update a -status.md file by merging new activity into existing sections
    rather than rewriting from scratch. Preserves longitudinal history.
    Inspired by NousResearch/hermes-agent trajectory compressor (MIT).

    The audit contract lives in :mod:`palinode.consolidation.status_doc`
    and is shared verbatim with ``palinode repair-status``: op fields are read
    through ``op_kind``/``op_reason`` (the dry-run preview's accessors, so the
    write path can no longer disagree with what ``--dry-run`` showed), a missing
    kind defaults to ``KEEP`` like the executor, unresolvable ``fact_id``s never
    reach the file, the log is bounded, and the frontmatter counts are
    reconciled with the body on every write.

    Args:
        file_path: Absolute path to the status markdown file.
        new_activity: List of operation dicts (``op``/``operation``,
            ``id``/``fact_id``/``ids``, ``reason``/``rationale``).
        known_fact_ids: Fact ids that existed before ``apply_operations`` ran.
            Unioned with the ids currently in the file, so both an ARCHIVE'd
            fact and a freshly minted ``supersedes-*`` id validate.
    """
    if not os.path.exists(file_path):
        return  # No existing status file to update

    if not new_activity:
        return  # Nothing to update

    with open(file_path, encoding="utf-8") as f:
        existing = f.read()

    valid_ids = set(known_fact_ids or set()) | status_doc.fact_ids(existing)
    lines = status_doc.render_log_lines(new_activity, valid_ids)
    if not lines:
        logger.info(
            "No auditable operations to log for %s (%d no-op KEEP(s) suppressed)",
            file_path, len(new_activity),
        )
        return

    frontmatter_block, body = split_frontmatter(existing)
    today = _utc_now().strftime("%Y-%m-%d")
    max_blocks = getattr(
        config.consolidation, "status_log_max_blocks",
        status_doc.DEFAULT_MAX_LOG_BLOCKS,
    )
    body = status_doc.merge_log_entry(body, today, lines, max_blocks=max_blocks)

    if not frontmatter_block:
        # No frontmatter to reconcile — write the merged body as-is rather than
        # refusing, which is what this path has always done.
        updated = body
    else:
        meta = status_doc.desired_frontmatter(frontmatter_block + body)
        if meta is None:
            logger.warning(
                "status frontmatter for %s does not parse — skipping the write "
                "so the log entry is not written into a broken document "
                "(run `palinode repair-status`)", file_path,
            )
            return
        # Reached only when new activity was merged above, so this is a real
        # content change and the receipt is earned.
        meta["last_updated"] = _utc_now().isoformat()
        updated = status_doc.render(meta, body)

    git_tools.write_memory_file(file_path, updated)

    logger.info(f"Updated status summary: {file_path} (+{len(lines)} entries)")


def _range_preview(target: str, op: dict) -> dict[str, Any]:
    """What an ``ARCHIVE_BEFORE`` would retire, for the dry-run preview.

    A range op names a date, so ``--dry-run`` showing only the date would
    describe the proposal and not the change: "before 2026-06-01" reads the
    same whether it retires two lines or four hundred. The ids are resolved
    with the executor's own recognizer against the document as it stands, so
    the preview is the same set the apply would remove.
    """
    before = str(op.get("before") or "").strip()
    cutoff = parse_moment(before) if before else None
    if cutoff is None:
        return {"ids": [], "count": 0, "before": before}
    try:
        with open(target, encoding="utf-8") as f:
            _, body = split_frontmatter(f.read())
    except OSError:  # pragma: no cover — defensive
        return {"ids": [], "count": 0, "before": before}
    ids = [line.fact_id for line in older_than(body, cutoff)]
    return {"ids": ids, "count": len(ids), "before": before}


def _proposed_changes(target: str, operations: list[dict]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for op in operations:
        if not isinstance(op, dict):
            continue
        change: dict[str, Any] = {
            "type": op_kind(op),
            "file": target,
            "rationale": op_reason(op),
        }
        if op_kind(op) == "ARCHIVE_BEFORE":
            change.update(_range_preview(target, op))
        changes.append(change)
    return changes


def run_consolidation(
    lookback_days: int | None = None,
    dry_run: bool = False,
    llm_fn: LlmFn | None = None,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run weekly consolidation under the memory store's shared run lock.

    Records the pass's start time against the activity gate's clock on the way
    out. A pass that raises or returns ``partial`` records nothing, so the next
    tick retries it immediately rather than waiting out a fresh interval; a dry
    run records nothing either, since it changed no memory and consolidated no
    notes. See ``_record_gate_run``. The pass's *outcome* — including a
    partial or a raise — is appended to the same state file regardless, so
    doctor can report it (``_record_run_outcome``).
    """
    from palinode.consolidation import activity_gate
    from palinode.consolidation.run_lock import consolidation_run_lock

    with consolidation_run_lock():
        started_at = activity_gate._utc_now()
        lookback = lookback_days or config.consolidation.lookback_days
        try:
            result = _run_consolidation_unlocked(
                lookback_days=lookback_days,
                dry_run=dry_run,
                llm_fn=llm_fn,
                sources=sources,
            )
        except Exception as error:
            _record_run_outcome(
                "weekly", None, started_at, dry_run=dry_run, lookback_days=lookback, error=error
            )
            raise
        _record_gate_run("weekly", result, started_at, dry_run=dry_run)
        _record_run_outcome("weekly", result, started_at, dry_run=dry_run, lookback_days=lookback)
        return result


def _run_consolidation_unlocked(
    lookback_days: int | None = None,
    dry_run: bool = False,
    llm_fn: LlmFn | None = None,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Orchestrator for the entire memory consolidation process.

    ``sources`` selects which directories under ``memory_dir`` to consolidate,
    defaulting to ``daily/``. Grouping and targeting are unchanged: notes
    are grouped by the ``project/`` refs they carry and compacted into that
    project's status file, so a source whose memories name no project
    contributes nothing — which the run summary reports as
    ``projects_compacted: 0`` rather than silently.

    Proposed operations are filtered against ``config.consolidation.allowed_ops``
    before being applied — the weekly-pass counterpart to ``run_nightly``'s
    ``config.consolidation.nightly.allowed_ops`` filter below.

    Each group's target is swept for stale dated log lines *before* its prompt
    is built (:func:`_retire_aged_log_lines`, reported as ``age_retired``), so
    what the model is shown is the recent window rather than the whole
    backlog. The sweep is deterministic and runs on age-eligible documents
    only; the nightly pass does not do it at all.
    """
    from palinode.consolidation.executor import apply_operations

    lookback = lookback_days or config.consolidation.lookback_days
    notes, yaml_skipped = _collect_daily_notes(lookback, sources=sources)
    if not notes:
        if dry_run:
            return {
                "status": "no notes found",
                "processed": 0,
                "processed_notes": 0,
                "projects_compacted": 0,
                "dry_run": True,
                "proposed_changes": [],
            }
        return {"status": "no notes found", "processed": 0}

    if yaml_skipped:
        logger.warning(
            "palinode.consolidation: %d daily note(s) had unparseable YAML frontmatter "
            "— run `palinode lint` to inspect. Proceeding with body text only.",
            yaml_skipped,
        )

    grouped = _group_by_project(notes)
    grouped, skipped_no_target = _partition_by_target(grouped)
    if skipped_no_target:
        logger.info(
            "palinode.consolidation: %d group(s) skipped — no project document to "
            "compact into: %s",
            len(skipped_no_target),
            ", ".join(skipped_no_target),
        )
    grouped, skipped_untagged = _partition_by_tagged_facts(grouped)
    _log_untagged_skips(skipped_untagged)

    total_stats = _empty_stats()
    # Weekly-only: the deterministic age sweep below is a weekly concern, the
    # same way ARCHIVE is. Counted here rather than in `_empty_stats` so the
    # nightly summary does not grow a key for something the nightly pass never
    # does.
    total_stats["age_retired"] = 0
    projects_processed = 0
    failed_projects: list[str] = []
    projects_no_ops: list[str] = []
    projects_all_ops_filtered: list[str] = []
    proposed_changes: list[dict[str, Any]] = []
    mutated_files: list[str] = []
    # Pre-existing crash, found by the no-target tests: `model_used` was assigned
    # only *after* the `if not operations: continue` inside the loop, but the
    # commit message below always reads it. A real pass where no project yields
    # operations — every fact a KEEP, which is the common quiet week — raised
    # UnboundLocalError at the commit step. `run_nightly` already initialised it;
    # this is the same guard, and the asymmetry is what marks it an oversight.
    model_used = "primary"

    for project_id, pnotes in grouped.items():
        try:
            model_used_current = "primary"

            # Groups without a target were filtered before this loop, so the
            # helper cannot return None here.
            target = _target_file_for(project_id)

            # Deterministic first, model second. Stale dated log lines are
            # retired on age alone — no LLM, its own commit — so the prompt
            # below is assembled from what is left rather than from six months
            # of session lines the model would have to enumerate one by one.
            total_stats["age_retired"] += _retire_aged_log_lines(
                target, dry_run=dry_run
            )

            operations, model_used_current = _consolidate_project(project_id, pnotes, llm_fn=llm_fn)
            if model_used_current == LLM_FAILED:
                failed_projects.append(project_id)
                continue
            if not operations:
                projects_no_ops.append(project_id)
                continue

            model_used = model_used_current

            # Propose-side guard: an uncited RETRACT becomes a
            # PROPOSE_CONTRADICTS, a retiring op aimed at the auto-footer is
            # rejected. Before the allowed_ops filter, so the downgraded op is
            # filtered like any other.
            operations = _guarded(
                operations, _prompt_context_for(project_id, pnotes), target, total_stats
            )

            # Enforce allowed-ops restriction (mirrors run_nightly's filter,
            # applied against the weekly-pass config key so the two passes
            # each have exactly one knob to restrict them). A missing kind
            # defaults to KEEP — same default the executor applies — so an
            # op with no "op"/"operation" field is filtered on what it will
            # actually become, not dropped as if it were some other kind.
            allowed_ops = set(config.consolidation.allowed_ops)
            operations = [op for op in operations if (op_kind(op) or "KEEP") in allowed_ops]
            if not operations:
                projects_all_ops_filtered.append(project_id)
                continue

            if dry_run:
                proposed_changes.extend(_proposed_changes(target, operations))
                projects_processed += 1
                logger.info(f"Previewed compaction for {project_id}: {len(operations)} operation(s)")
                continue

            pre_apply_ids = _fact_ids_before_apply(target)
            applied_merges: list[int] = []
            applied_ranges: list[int] = []
            stats = apply_operations(
                target, operations,
                applied_merges=applied_merges, applied_ranges=applied_ranges,
            )
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

            # Iteratively append operations to the status file, preserving history
            _update_status_summary(
                target,
                _loggable_operations(operations, applied_merges, applied_ranges),
                known_fact_ids=pre_apply_ids,
            )

            # Track exactly the files this project's compaction touched so the
            # commit stages only them (one-mutation-one-commit).
            mutated_files.extend(_touched_files(target))

            projects_processed += 1
            logger.info(f"Compacted {project_id}: {stats}")

        except Exception as e:
            failed_projects.append(project_id)
            logger.error(f"Compaction failed for {project_id}: {e}")

    _log_no_op_groups(projects_no_ops, projects_all_ops_filtered)

    # Archive only what a group actually saw and decided on. A note that
    # belongs to a failed, untargetable or untagged group stays in place so the
    # next run sees it again; so does one that names no project (never grouped,
    # so never seen) and today's live daily note. A group that proposed nothing
    # — or whose every op the allowed_ops filter removed — did see its notes
    # and chose to change nothing, so those notes retire: otherwise a quiet
    # store never archives and daily/ grows without bound.
    partition = _partition_notes_for_archive(
        notes,
        unresolved=set(failed_projects) | set(skipped_no_target) | set(skipped_untagged),
    )
    decided = projects_processed + len(projects_no_ops) + len(projects_all_ops_filtered)

    if dry_run:
        result = {
            "status": _run_status(failed_projects),
            "processed_notes": len(notes),
            "projects_compacted": projects_processed,
            "projects_failed": len(failed_projects),
            "projects_skipped": len(skipped_no_target) + len(skipped_untagged),
            "notes_no_project": len(partition.no_project),
            "notes_today_kept": len(partition.today),
            "dry_run": True,
            "proposed_changes": proposed_changes,
            # Previewed, not applied: the sweep counted its lines and left them
            # in the file, like every other op a dry run reports.
            "age_retired": total_stats["age_retired"],
            **{key: total_stats[key] for key in GUARD_STATS},
        }
        if yaml_skipped:
            result["yaml_parse_errors"] = yaml_skipped
        if failed_projects:
            result["failed_projects"] = failed_projects
        # Same shape as yaml_parse_errors: a count of what silently
        # did not happen belongs in the result, not only the log.
        _record_skips(result, no_target=skipped_no_target, untagged=skipped_untagged)
        _record_no_ops(
            result, no_ops=projects_no_ops, all_ops_filtered=projects_all_ops_filtered
        )
        return result

    if decided > 0:
        retire, left_in_place = partition.retire, partition.left
        _archive_daily_notes(retire)
        _log_notes_left_in_place(partition)
    else:
        logger.warning(
            "No project group reached a decision (all failed or skipped) — "
            "skipping daily note archival"
        )
        left_in_place = notes
        retire = []

    # A pass whose only outcome was a contradiction link used to commit
    # "0u 0m 0s 0a" — a message that reads as "nothing happened" over a real
    # mutation. Appended only when non-zero so the common message is unchanged.
    contradicts_note = (
        f" {total_stats.get('contradicts_proposed', 0)}c"
        if total_stats.get("contradicts_proposed") else ""
    )
    _git_commit(
        f"palinode: compaction {_utc_now().strftime('%Y-%m-%d')} — "
        f"{total_stats['updated']}u {total_stats['merged']}m "
        f"{total_stats['superseded']}s {total_stats['archived']}a"
        f"{contradicts_note}"
        f" (model: {model_used})",
        files=mutated_files,
    )

    result: dict[str, Any] = {
        "status": _run_status(failed_projects),
        "processed_notes": len(notes),
        "projects_compacted": projects_processed,
        "projects_failed": len(failed_projects),
        "projects_skipped": len(skipped_no_target) + len(skipped_untagged),
        "notes_archived": len(retire),
        "notes_left_in_place": len(left_in_place),
        "notes_no_project": len(partition.no_project),
        "notes_today_kept": len(partition.today),
        **total_stats,
    }
    if yaml_skipped:
        result["yaml_parse_errors"] = yaml_skipped
    if failed_projects:
        result["failed_projects"] = failed_projects
    _record_skips(result, no_target=skipped_no_target, untagged=skipped_untagged)
    _record_no_ops(result, no_ops=projects_no_ops, all_ops_filtered=projects_all_ops_filtered)
    return result


def run_nightly(
    lookback_days: int | None = None,
    dry_run: bool = False,
    llm_fn: LlmFn | None = None,
) -> dict[str, Any]:
    """Run nightly consolidation under the memory store's shared run lock.

    Records against the gate's ``nightly`` clock on the same terms as
    ``run_consolidation`` records against ``weekly``; the two modes are tracked
    separately so whichever ran last cannot starve the other.

    Also advances the per-project watermarks, on the same success-only terms
    and from the same ``started_at`` — one clock decides both what the pass
    selected and what it records having consolidated.

    ``lookback_days`` (the cron's ``--days N``) is no longer a window: it is
    the catch-up bound, the furthest back a mark may reach. See
    :func:`_run_nightly_unlocked`.
    """
    from palinode.consolidation import activity_gate
    from palinode.consolidation.run_lock import consolidation_run_lock

    with consolidation_run_lock():
        started_at = activity_gate._utc_now()
        lookback = lookback_days or config.consolidation.nightly.lookback_days
        try:
            result = _run_nightly_unlocked(
                lookback_days=lookback_days,
                dry_run=dry_run,
                llm_fn=llm_fn,
                started_at=started_at,
            )
        except Exception as error:
            _record_run_outcome(
                "nightly", None, started_at, dry_run=dry_run, lookback_days=lookback, error=error
            )
            raise
        _advance_watermarks(result, started_at, dry_run=dry_run)
        _record_gate_run("nightly", result, started_at, dry_run=dry_run)
        _record_run_outcome("nightly", result, started_at, dry_run=dry_run, lookback_days=lookback)
        return result


def _run_nightly_unlocked(
    lookback_days: int | None = None,
    dry_run: bool = False,
    llm_fn: LlmFn | None = None,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    """Lightweight nightly consolidation — everything not yet consolidated.

    Restricted to UPDATE and SUPERSEDE ops. No ARCHIVE or MERGE (those
    are weekly concerns). Smaller LLM context = better JSON output.

    Selection is a **per-project watermark**, not a calendar window: each
    project's notes are those written since the last pass that resolved *that*
    project (:mod:`palinode.consolidation.watermark`). A note is therefore sent
    exactly once when passes succeed, and a failed pass re-sends only what it
    failed on. ``lookback_days`` — the cron's ``--days N`` — survives as the
    catch-up bound: how far back a cold or long-failed mark may reach, which
    is the only thing the number still governs.

    Pinned to ``NIGHTLY_CONSOLIDATION_SOURCES`` rather than inheriting the
    weekly default: "the daily capture stream" is this function's contract, and
    an unpinned call would have widened it silently the moment the weekly
    default grew.
    """
    from palinode.consolidation.executor import apply_operations

    started_at = started_at or _utc_now()
    catchup_days = max(int(lookback_days or config.consolidation.nightly.lookback_days), 1)
    floor = watermark.floor_at(started_at, catchup_days)
    marks = watermark.load()
    resumes = watermark.load_resume()
    cold_start_at, cold_start_reason = watermark.cold_start(floor)
    logger.info(
        "palinode.consolidation: nightly selects per-project watermarks — %d "
        "recorded mark(s); a project with none starts at %s (%s); catch-up "
        "bounded to %d day(s), i.e. nothing older than %s (a bound, not a "
        "window: notes are selected once and re-selected only when a pass fails)",
        len(marks),
        watermark.stamp(cold_start_at),
        cold_start_reason,
        catchup_days,
        watermark.stamp(floor),
    )

    # One microsecond under the floor: a resume position sitting exactly on it
    # selects its own note inclusively. The per-project filter below decides.
    notes, yaml_skipped = _collect_daily_notes(
        sources=NIGHTLY_CONSOLIDATION_SOURCES, since=floor - timedelta(microseconds=1)
    )
    if yaml_skipped:
        logger.warning(
            "palinode.consolidation: %d daily note(s) had unparseable YAML frontmatter "
            "— run `palinode lint` to inspect. Proceeding with body text only.",
            yaml_skipped,
        )

    grouped = _group_by_project(notes)
    grouped, watermark_clamped, resumes = _select_by_watermark(
        grouped,
        marks=marks,
        cold_start_at=cold_start_at,
        floor=floor,
        catchup_days=catchup_days,
        sources=NIGHTLY_CONSOLIDATION_SOURCES,
        resumes=resumes,
    )
    # Counted after the per-project filter: the honest answer to "what did this
    # pass process" is the notes at least one group actually sent, not every
    # file inside the catch-up bound.
    selected = {
        note["filepath"] for project_notes in grouped.values() for note in project_notes
    }
    if not grouped:
        if dry_run:
            return {
                "status": "no_new_notes",
                "processed_notes": 0,
                "projects_compacted": 0,
                "dry_run": True,
                "proposed_changes": [],
            }
        return {"status": "no_new_notes", "processed_notes": 0, "projects_compacted": 0}

    grouped, skipped_no_target = _partition_by_target(grouped)
    if skipped_no_target:
        logger.info(
            "palinode.consolidation: %d group(s) skipped — no project document to "
            "compact into: %s",
            len(skipped_no_target),
            ", ".join(skipped_no_target),
        )
    grouped, skipped_untagged = _partition_by_tagged_facts(grouped)
    _log_untagged_skips(skipped_untagged)
    
    total_stats = _empty_stats()
    projects_processed = 0
    failed_projects: list[str] = []
    projects_no_ops: list[str] = []
    projects_all_ops_filtered: list[str] = []
    # The ids behind ``projects_processed``, kept because the watermark moves
    # per project and a count cannot say which.
    projects_compacted: list[str] = []
    model_used = "primary"
    proposed_changes: list[dict[str, str]] = []
    mutated_files: list[str] = []
    # Input coverage: what each group selected, what each of its prompts
    # actually carried, and which prompt a resolution of it may move the mark
    # to the end of.
    plans: dict[str, list[NightlyInput]] = {}
    last_resolved: dict[str, NightlyInput] = {}
    max_prompts = max(int(config.consolidation.nightly.max_prompts_per_project), 1)

    def _send(project_id: str, pnotes: list[dict]) -> str:
        """One prompt for one project: propose, guard, filter, apply.

        Returns ``"failed"``, ``"no_ops"``, ``"all_filtered"`` or
        ``"compacted"``. Everything but a failure resolved the prompt.
        """
        nonlocal model_used
        try:
            operations, model_used_current = _consolidate_project(project_id, pnotes, is_nightly=True, llm_fn=llm_fn)
            if model_used_current == LLM_FAILED:
                return "failed"
            if not operations:
                return "no_ops"

            model_used = model_used_current

            # Groups without a target were filtered before this loop, so the
            # helper cannot return None here.
            target = _target_file_for(project_id)

            # Same propose-side guard as the weekly pass, and for the same
            # reason it runs before the filter: nightly's allowed_ops excludes
            # RETRACT, so the guard is what turns an uncited one into the
            # PROPOSE_CONTRADICTS the filter admits.
            operations = _guarded(
                operations, _prompt_context_for(project_id, pnotes), target, total_stats
            )

            # Enforce allowed-ops restriction. Resolves a missing op-kind the
            # same way the weekly filter does (op_kind(op) or "KEEP", matching
            # the executor's own default) rather than comparing an empty
            # string against allowed_ops — which can never match regardless
            # of configuration, silently dropping the op no matter what an
            # operator sets allowed_ops to.
            allowed_ops = set(config.consolidation.nightly.allowed_ops)
            operations = [op for op in operations if (op_kind(op) or "KEEP") in allowed_ops]
            if not operations:
                return "all_filtered"

            if dry_run:
                proposed_changes.extend(_proposed_changes(target, operations))
                logger.info(f"Previewed nightly compaction for {project_id}: {len(operations)} operation(s)")
                return "compacted"

            pre_apply_ids = _fact_ids_before_apply(target)
            applied_merges: list[int] = []
            stats = apply_operations(
                target, operations, nightly_policy=True, applied_merges=applied_merges,
            )
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

            _update_status_summary(
                target,
                _loggable_operations(operations, applied_merges),
                known_fact_ids=pre_apply_ids,
            )

            mutated_files.extend(_touched_files(target))
            logger.info(f"Nightly compacted {project_id}: {stats}")
            return "compacted"

        except Exception as e:
            logger.error(f"Nightly compaction failed for {project_id}: {e}")
            return "failed"

    for project_id, selection in grouped.items():
        # Up to ``max_prompts`` prompts, each planned exactly as a single
        # pass plans one and resumed where the previous one stopped. The next
        # prompt is sent only after this one resolved and left a resume
        # position; a failed, truncated or unreadable reply ends the project's
        # pass there, and its mark stays at the end of the last resolved one.
        resume = resumes.get(project_id)
        remaining = selection
        sent: list[NightlyInput] = []
        outcomes: set[str] = set()
        while True:
            plan = _plan_nightly_input(remaining, resume=resume, until=started_at)
            sent.append(plan)
            outcome = _send(project_id, plan.notes)
            if outcome == "failed":
                failed_projects.append(project_id)
                break
            outcomes.add(outcome)
            last_resolved[project_id] = plan
            if plan.resume is None or len(sent) >= max_prompts:
                break
            resume = plan.resume
            position = (resume.modified_at, resume.path)
            remaining = [note for note in remaining if _progress_key(note) >= position]
        plans[project_id] = sent

        # One entry per project in the lists below, however many prompts it
        # took: compacted if any prompt applied something.
        if "compacted" in outcomes:
            projects_processed += 1
            projects_compacted.append(project_id)
        elif "all_filtered" in outcomes:
            projects_all_ops_filtered.append(project_id)
        elif "no_ops" in outcomes:
            projects_no_ops.append(project_id)

        chosen, shown, finished = _project_files(sent)
        if chosen - finished:
            logger.warning(
                "palinode.consolidation: %s — %d of %d selected note(s) do not fit "
                "this pass's %d prompt(s) in full (%d presented, %d of them partly); "
                "the rest stay pending and are selected again next pass",
                project_id,
                len(chosen - finished),
                len(chosen),
                len(sent),
                len(shown),
                len(shown - finished),
            )

    _log_no_op_groups(projects_no_ops, projects_all_ops_filtered)

    # Nightly does NOT archive daily notes (left for weekly)

    # Every group the pass reached a decision about — compacted, proposed
    # nothing, or had every op filtered. All three saw their notes, which is
    # the condition for advancing a mark; a failed or skipped group is absent
    # by construction and keeps the mark it had.
    projects_resolved = sorted(
        set(projects_compacted) | set(projects_no_ops) | set(projects_all_ops_filtered)
    )

    if dry_run:
        nightly_result = {
            "status": _run_status(failed_projects),
            "processed_notes": len(selected),
            "projects_compacted": projects_processed,
            "projects_failed": len(failed_projects),
            "projects_skipped": len(skipped_no_target) + len(skipped_untagged),
            "projects_resolved": projects_resolved,
            "dry_run": True,
            "proposed_changes": proposed_changes,
            **{key: total_stats[key] for key in GUARD_STATS},
        }
        if yaml_skipped:
            nightly_result["yaml_parse_errors"] = yaml_skipped
        if failed_projects:
            nightly_result["failed_projects"] = failed_projects
        if watermark_clamped:
            nightly_result["watermark_clamped"] = watermark_clamped
        _record_skips(
            nightly_result, no_target=skipped_no_target, untagged=skipped_untagged
        )
        _record_no_ops(
            nightly_result, no_ops=projects_no_ops, all_ops_filtered=projects_all_ops_filtered
        )
        _record_coverage(nightly_result, plans, last_resolved)
        return nightly_result

    if projects_processed > 0:
        _git_commit(
            f"palinode: nightly {_utc_now().strftime('%Y-%m-%d')} — "
            f"{total_stats['updated']}u {total_stats['superseded']}s"
            f" (model: {model_used})",
            files=mutated_files,
        )
    
    nightly_result: dict[str, Any] = {
        "status": _run_status(failed_projects),
        "processed_notes": len(selected),
        "projects_compacted": projects_processed,
        "projects_failed": len(failed_projects),
        "projects_skipped": len(skipped_no_target) + len(skipped_untagged),
        "projects_resolved": projects_resolved,
        **total_stats,
    }
    if yaml_skipped:
        nightly_result["yaml_parse_errors"] = yaml_skipped
    if failed_projects:
        nightly_result["failed_projects"] = failed_projects
    if watermark_clamped:
        nightly_result["watermark_clamped"] = watermark_clamped
    _record_skips(nightly_result, no_target=skipped_no_target, untagged=skipped_untagged)
    _record_no_ops(
        nightly_result, no_ops=projects_no_ops, all_ops_filtered=projects_all_ops_filtered
    )
    _record_coverage(nightly_result, plans, last_resolved)
    return nightly_result


def _project_files(sent: list[NightlyInput]) -> tuple[set[str], set[str], set[str]]:
    """``(selected, presented, finished)`` note files across one project's prompts.

    The pass's selection is the first prompt's; a later prompt selects a
    suffix of it. A note split across prompts is presented once and finished
    once, so nothing is counted twice.
    """
    selected = set(sent[0].selection) if sent else set()
    presented: set[str] = set()
    finished: set[str] = set()
    for plan in sent:
        for note in plan.notes:
            presented.add(note["filepath"])
            if note["span"][1] == note["span"][2]:
                finished.add(note["filepath"])
    return selected, presented, finished


def _project_coverage(sent: list[NightlyInput]) -> dict[str, int]:
    """One project's ``{selected, presented, pending}`` over the prompts it was sent."""
    selected, presented, finished = _project_files(sent)
    return {
        "selected": len(selected),
        "presented": len(presented),
        "pending": len(selected - finished),
    }


def _record_coverage(
    result: dict[str, Any],
    plans: dict[str, list[NightlyInput]],
    last_resolved: dict[str, NightlyInput],
) -> None:
    """Report what the model was shown against what the pass selected.

    Counts are distinct note files across every prompt sent to the model:
    ``notes_selected`` were chosen, ``notes_presented`` appeared in a prompt at
    least in part, ``notes_pending`` were not presented in full to at least one
    group and are selected again next pass. ``coverage`` breaks the same three
    down per project, and ``prompts_sent`` says how many prompts each project
    took. ``watermark_resume`` names, for each project whose last *resolved*
    prompt stopped part-way, the position its mark records instead of the
    pass's start — the one thing :func:`_advance_watermarks` writes
    differently.
    """
    selected: set[str] = set()
    presented: set[str] = set()
    pending: set[str] = set()
    coverage: dict[str, dict[str, int]] = {}
    for project_id, sent in plans.items():
        chosen, shown, finished = _project_files(sent)
        selected |= chosen
        presented |= shown
        pending |= chosen - finished
        coverage[project_id] = _project_coverage(sent)
    result["notes_selected"] = len(selected)
    result["notes_presented"] = len(presented)
    result["notes_pending"] = len(pending)
    result["coverage"] = coverage
    result["prompts_sent"] = {project_id: len(sent) for project_id, sent in plans.items()}
    resumes = {
        project_id: plan.resume.to_state()
        for project_id, plan in last_resolved.items()
        if plan.resume is not None
    }
    if resumes:
        result["watermark_resume"] = resumes


def apply_proposed_operations(
    target: str,
    operations: list[dict],
    *,
    source: str,
) -> dict[str, Any]:
    """Apply already-built operations to one memory file, with provenance.

    The entry point for proposers that are *not* the LLM — today the
    deterministic lint→op mapping in
    :mod:`palinode.consolidation.propose_from_lint`. It deliberately reuses the
    weekly pass's tail rather than reimplementing it: the same
    ``apply_operations`` call, the same pre-apply fact-id capture so a retiring
    op's id still resolves in the audit log, the same status-document log entry,
    and the same one-mutation-one-commit staging of the target plus its
    ``-history.md`` sibling.

    The only thing it adds is the actor. ``source`` names the proposer and lands
    in the commit subject, so ``git log`` distinguishes an op the executor
    applied on lint's proposal from one it applied on the model's. Everything
    upstream of this call — deciding *which* ops, and whether they are allowed —
    belongs to the proposer.

    Args:
        target: absolute path to the memory file the operations address.
        operations: executor operation dicts, already validated by the proposer.
        source: the proposing actor, e.g. ``"lint"``.

    Returns the executor's stats dict.
    """
    from palinode.consolidation.executor import apply_operations

    pre_apply_ids = _fact_ids_before_apply(target)
    stats = apply_operations(target, operations)

    # A status document is the one place with a Consolidation Log to append to;
    # writing that log into an ordinary memory would invent a section the
    # document never had.
    if target.endswith("-status.md"):
        _update_status_summary(target, operations, known_fact_ids=pre_apply_ids)

    kinds = ", ".join(sorted({
        op_kind(op) or "KEEP" for op in operations if isinstance(op, dict)
    }))
    _git_commit(
        f"{config.git.commit_prefix} {source}-proposed ops: {kinds}",
        files=_touched_files(target),
    )
    return stats
