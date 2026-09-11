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
from typing import Any, Callable

# The propose→apply seam. The nondeterministic half of consolidation is a
# single call shaped (system_prompt, user_prompt) -> (response_text, model_used);
# the deterministic half (parse → executor.apply_operations) runs on its output.
# Making this callable injectable lets the runner→executor path be driven with
# canned op-JSON — no live LLM, no wholesale mock of _consolidate_project. The
# default is the live fallback-chain caller; tests pass a fake that returns
# deterministic op-JSON. Kept here (not a separate module) so the client-factory
# patch seam test_fallback relies on — `runner.get_ollama_client` — stays put.
LlmFn = Callable[[str, str], tuple[str, str]]

import yaml

from palinode.core.config import config
from palinode.core import store, embedder, git_tools
from palinode.core.ollama_client import OllamaError, OllamaRole, get_ollama_client
from palinode.core.parser import split_frontmatter
from palinode.consolidation import status_doc
from palinode.consolidation.fact_ids import FACT_LINE_RE, count_body_facts
from palinode.consolidation.op_parse import op_kind, op_reason, parse_result
from palinode.prompts import PromptUnavailable, resolve_prompt

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
    """Fetch active decisions related to a specific project."""
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
                    entities = meta.get("entities", [])
                    if f"project/{project_id}" in entities and meta.get("status") != "superseded":
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
    superseded decisions — a superseded decision is exactly the thing the
    compactor must NOT treat as binding.
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
        return ""

    parts: list[str] = []
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
        total += len(entry)

    return "\n".join(parts)


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


def _collect_daily_notes(
    lookback_days: int, sources: Sequence[str] | None = None
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

    Returns:
        Tuple of (notes list, skipped_count) where skipped_count is the
        number of files whose YAML frontmatter failed to parse.
        Callers surface skipped_count in the consolidation run summary so
        operators know to run ``palinode lint``.
    """
    selected = tuple(sources) if sources else DEFAULT_CONSOLIDATION_SOURCES

    cutoff_date = (_utc_now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
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

        # Fast path, and the reason daily behaviour is unchanged: a date-named
        # file older than the cutoff is rejected without being opened, exactly
        # as before. Only files whose date must come from frontmatter get read
        # in order to be filtered.
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
        if date_str < cutoff_date:
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


def _partition_notes_for_archive(
    notes: list[dict], unresolved: set[str]
) -> tuple[list[dict], list[dict]]:
    """Split notes into (retire, leave in place).

    A note is retired only if none of the project groups it belongs to is in
    ``unresolved`` — the failed and no-target groups. Per-note rather than
    per-run: a note that mentions two projects, one compacted and one whose
    LLM call failed, has not been consolidated, and archiving it would hide
    the failed half from every future run.
    """
    retire: list[dict] = []
    left: list[dict] = []
    for note in notes:
        projects = {
            m.split("project/", 1)[1] for m in note["mentions"] if m.startswith("project/")
        }
        (left if projects & unresolved else retire).append(note)
    return retire, left


def _read_prompt_body(prompt_path: str) -> str:
    """The prompt text a model should see — frontmatter stripped.

    Prompt files under ``specs/prompts/`` are memory files: they carry a
    ``version:``/``active:`` frontmatter block that the prompt-versioning API
    and `palinode doctor` read. That block is metadata *about* the prompt, not
    an instruction to the model, and sending it prepends a YAML document to the
    system prompt for no benefit. Files without frontmatter are returned whole,
    which is what every prompt did before any of them had one.
    """
    with open(prompt_path, encoding="utf-8") as f:
        _, body = split_frontmatter(f.read())
    return body.lstrip("\n")


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
    """
    # Load compaction prompt: store copy, then the copy inside the wheel.
    # PromptUnavailable propagates — the caller records the project as failed,
    # which is the honest report for "this pass could not run".
    system_prompt = _system_prompt(
        "nightly-consolidation.md" if is_nightly else "compaction.md"
    )

    # Load project file and extract facts. Callers filter no-target groups out
    # before reaching here; this stays defensive for direct callers, and returns
    # the same "nothing to do" shape as a file with no tagged facts rather than
    # raising for a condition that is a skip.
    target_file = _target_file_for(project_id)
    if target_file is None:
        logger.info(
            "No project document for %r — nothing to compact into, skipping",
            project_id,
        )
        return [], "primary"

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
        return [], "primary"
    
    # Format for LLM
    facts_text = "\n".join(f"[{f['id']}] {f['text']}" for f in facts)
    
    MAX_NOTES_CHARS = 6000
    notes_parts = []
    total = 0
    for n in reversed(notes):
        entry = f"### {n['date']}\n{n['content'][:1500]}"
        if total + len(entry) > MAX_NOTES_CHARS:
            break
        notes_parts.append(entry)
        total += len(entry)
    notes_parts.reverse()
    notes_text = "\n\n".join(notes_parts)

    # Active decisions for this project, as *constraints* on what may be
    # proposed — not as material to compact. Supplied without a lookback:
    # a decision does not stop governing because nobody touched its file this
    # week, which is the opposite of how a note ages.
    decisions_text = _format_active_decisions(project_id)
    decisions_section = (
        f"\n## ACTIVE_DECISIONS (governing this project)\n\n{decisions_text}\n"
        if decisions_text
        else ""
    )

    user_prompt = f"""## EXISTING_FACTS ({len(facts)} facts from {os.path.basename(target_file)})

{facts_text}
{decisions_section}
## RECENT_NOTES (last {config.consolidation.lookback_days} days)

{notes_text}

Return the operations JSON array."""

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


def _loggable_operations(
    operations: list[dict], applied_merges: list[int],
) -> list[dict]:
    """Drop the MERGE proposals the executor did not apply.

    The Consolidation Log records what consolidation *did*, and every other op
    kind's line already survives a no-op honestly (a KEEP with no rationale
    emits nothing; an unresolvable id logs as unresolved). MERGE was the
    exception: a proposal that retired nothing — no fact moved, no history
    entry written — still left a ``[MERGE] …`` line claiming a merge happened.
    Gate that one kind on the executor's own report of which merges landed.

    Args:
        operations: The ops passed to ``apply_operations``, in order.
        applied_merges: Indices into ``operations`` of the MERGEs it applied.
    """
    applied = set(applied_merges)
    return [
        op for index, op in enumerate(operations)
        if (op_kind(op) or "KEEP") != "MERGE" or index in applied
    ]


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


def _proposed_changes(target: str, operations: list[dict]) -> list[dict[str, str]]:
    return [
        {
            "type": op_kind(op),
            "file": target,
            "rationale": op_reason(op),
        }
        for op in operations
        if isinstance(op, dict)
    ]


def run_consolidation(
    lookback_days: int | None = None,
    dry_run: bool = False,
    llm_fn: LlmFn | None = None,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run weekly consolidation under the memory store's shared run lock.

    Records the pass against the activity gate's clock on the way out. A pass
    that raises records nothing, so the next tick retries it immediately rather
    than waiting out a fresh interval; a dry run records nothing either, since
    it changed no memory and consolidated no notes.
    """
    from palinode.consolidation import activity_gate
    from palinode.consolidation.run_lock import consolidation_run_lock

    with consolidation_run_lock():
        result = _run_consolidation_unlocked(
            lookback_days=lookback_days,
            dry_run=dry_run,
            llm_fn=llm_fn,
            sources=sources,
        )
        if not dry_run:
            activity_gate.record_run("weekly")
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

    total_stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    projects_processed = 0
    failed_projects: list[str] = []
    proposed_changes: list[dict[str, str]] = []
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
            operations, model_used_current = _consolidate_project(project_id, pnotes, llm_fn=llm_fn)
            if model_used_current == LLM_FAILED:
                failed_projects.append(project_id)
                continue
            if not operations:
                continue

            model_used = model_used_current

            # Enforce allowed-ops restriction (mirrors run_nightly's filter,
            # applied against the weekly-pass config key so the two passes
            # each have exactly one knob to restrict them). A missing kind
            # defaults to KEEP — same default the executor applies — so an
            # op with no "op"/"operation" field is filtered on what it will
            # actually become, not dropped as if it were some other kind.
            allowed_ops = set(config.consolidation.allowed_ops)
            operations = [op for op in operations if (op_kind(op) or "KEEP") in allowed_ops]
            if not operations:
                continue

            # Groups without a target were filtered before this loop, so the
            # helper cannot return None here.
            target = _target_file_for(project_id)

            if dry_run:
                proposed_changes.extend(_proposed_changes(target, operations))
                projects_processed += 1
                logger.info(f"Previewed compaction for {project_id}: {len(operations)} operation(s)")
                continue

            pre_apply_ids = _fact_ids_before_apply(target)
            applied_merges: list[int] = []
            stats = apply_operations(target, operations, applied_merges=applied_merges)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

            # Iteratively append operations to the status file, preserving history
            _update_status_summary(
                target,
                _loggable_operations(operations, applied_merges),
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

    if dry_run:
        result = {
            "status": _run_status(failed_projects),
            "processed_notes": len(notes),
            "projects_compacted": projects_processed,
            "projects_failed": len(failed_projects),
            "projects_skipped": len(skipped_no_target) + len(skipped_untagged),
            "dry_run": True,
            "proposed_changes": proposed_changes,
        }
        if yaml_skipped:
            result["yaml_parse_errors"] = yaml_skipped
        if failed_projects:
            result["failed_projects"] = failed_projects
        # Same shape as yaml_parse_errors: a count of what silently
        # did not happen belongs in the result, not only the log.
        _record_skips(result, no_target=skipped_no_target, untagged=skipped_untagged)
        return result

    # Archive only what was actually consolidated. A note that belongs to a
    # failed, untargetable or untagged group stays in place so the next run sees
    # it again — moving it to archive/ would retire it unconsolidated. The
    # untagged class was previously archived: the group reached the loop, was
    # dropped for having no addressable facts, and its notes retired anyway.
    retire, left_in_place = _partition_notes_for_archive(
        notes,
        unresolved=set(failed_projects) | set(skipped_no_target) | set(skipped_untagged),
    )
    if projects_processed > 0:
        _archive_daily_notes(retire)
    else:
        logger.warning("No projects compacted successfully — skipping daily note archival")
        left_in_place = notes
        retire = []
    if left_in_place and projects_processed > 0:
        logger.warning(
            "palinode.consolidation: %d note(s) left in place — their project "
            "group(s) failed, had no target, or had no tagged facts: %s",
            len(left_in_place),
            ", ".join(os.path.basename(n["filepath"]) for n in left_in_place),
        )

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
        **total_stats,
    }
    if yaml_skipped:
        result["yaml_parse_errors"] = yaml_skipped
    if failed_projects:
        result["failed_projects"] = failed_projects
    _record_skips(result, no_target=skipped_no_target, untagged=skipped_untagged)
    return result


def run_nightly(lookback_days: int | None = None, dry_run: bool = False, llm_fn: LlmFn | None = None) -> dict[str, Any]:
    """Run nightly consolidation under the memory store's shared run lock.

    Records against the gate's ``nightly`` clock on the same terms as
    ``run_consolidation`` records against ``weekly``; the two modes are tracked
    separately so whichever ran last cannot starve the other.
    """
    from palinode.consolidation import activity_gate
    from palinode.consolidation.run_lock import consolidation_run_lock

    with consolidation_run_lock():
        result = _run_nightly_unlocked(
            lookback_days=lookback_days,
            dry_run=dry_run,
            llm_fn=llm_fn,
        )
        if not dry_run:
            activity_gate.record_run("nightly")
        return result


def _run_nightly_unlocked(lookback_days: int | None = None, dry_run: bool = False, llm_fn: LlmFn | None = None) -> dict[str, Any]:
    """Lightweight nightly consolidation — process today's daily notes only.

    Restricted to UPDATE and SUPERSEDE ops. No ARCHIVE or MERGE (those
    are weekly concerns). Smaller LLM context = better JSON output.

    Pinned to ``NIGHTLY_CONSOLIDATION_SOURCES`` rather than inheriting the
    weekly default: "today's daily notes only" is this function's contract, and
    an unpinned call would have widened it silently the moment the weekly
    default grew.
    """
    from palinode.consolidation.executor import apply_operations

    lookback = lookback_days or config.consolidation.nightly.lookback_days
    notes, yaml_skipped = _collect_daily_notes(
        lookback, sources=NIGHTLY_CONSOLIDATION_SOURCES
    )
    if not notes:
        if dry_run:
            return {
                "status": "no_new_notes",
                "processed_notes": 0,
                "projects_compacted": 0,
                "dry_run": True,
                "proposed_changes": [],
            }
        return {"status": "no_new_notes", "processed_notes": 0, "projects_compacted": 0}

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
    
    total_stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    projects_processed = 0
    failed_projects: list[str] = []
    model_used = "primary"
    proposed_changes: list[dict[str, str]] = []
    mutated_files: list[str] = []

    for project_id, pnotes in grouped.items():
        try:
            operations, model_used_current = _consolidate_project(project_id, pnotes, is_nightly=True, llm_fn=llm_fn)
            if model_used_current == LLM_FAILED:
                failed_projects.append(project_id)
                continue
            if not operations:
                continue

            model_used = model_used_current

            # Enforce allowed-ops restriction. Resolves a missing op-kind the
            # same way the weekly filter does (op_kind(op) or "KEEP", matching
            # the executor's own default) rather than comparing an empty
            # string against allowed_ops — which can never match regardless
            # of configuration, silently dropping the op no matter what an
            # operator sets allowed_ops to.
            allowed_ops = set(config.consolidation.nightly.allowed_ops)
            operations = [op for op in operations if (op_kind(op) or "KEEP") in allowed_ops]
            if not operations:
                continue

            # Groups without a target were filtered before this loop, so the
            # helper cannot return None here.
            target = _target_file_for(project_id)

            if dry_run:
                proposed_changes.extend(_proposed_changes(target, operations))
                projects_processed += 1
                logger.info(f"Previewed nightly compaction for {project_id}: {len(operations)} operation(s)")
                continue

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

            projects_processed += 1
            logger.info(f"Nightly compacted {project_id}: {stats}")

        except Exception as e:
            failed_projects.append(project_id)
            logger.error(f"Nightly compaction failed for {project_id}: {e}")

    # Nightly does NOT archive daily notes (left for weekly)

    if dry_run:
        nightly_result = {
            "status": _run_status(failed_projects),
            "processed_notes": len(notes),
            "projects_compacted": projects_processed,
            "projects_failed": len(failed_projects),
            "projects_skipped": len(skipped_no_target) + len(skipped_untagged),
            "dry_run": True,
            "proposed_changes": proposed_changes,
        }
        if yaml_skipped:
            nightly_result["yaml_parse_errors"] = yaml_skipped
        if failed_projects:
            nightly_result["failed_projects"] = failed_projects
        _record_skips(
            nightly_result, no_target=skipped_no_target, untagged=skipped_untagged
        )
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
        "processed_notes": len(notes),
        "projects_compacted": projects_processed,
        "projects_failed": len(failed_projects),
        "projects_skipped": len(skipped_no_target) + len(skipped_untagged),
        **total_stats,
    }
    if yaml_skipped:
        nightly_result["yaml_parse_errors"] = yaml_skipped
    if failed_projects:
        nightly_result["failed_projects"] = failed_projects
    _record_skips(nightly_result, no_target=skipped_no_target, untagged=skipped_untagged)
    return nightly_result


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
