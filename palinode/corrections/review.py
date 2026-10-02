"""The two-phase correction contract: PREVIEW reads, APPLY writes.

Mining a correction candidate (:mod:`palinode.corrections.scan`) stops at a
proposal. This module is the review flow that turns one — or a person's own
statement, with no candidate behind it at all — into a memory operation, and it
is the **only** sanctioned path from "this memory is wrong" to a write.

Three properties are why it is one module rather than a verb per surface:

* **Two phases, and the first one writes nothing.** :func:`preview_correction`
  reads. It returns the old text, the proposed new text, the target's exact
  source revision, the rationale, where the correction came from, the scope,
  the replacement relation that *would* be recorded, every other record that
  quotes or derives from the target, and the exact recovery command. Nothing
  about the store changes. :func:`apply_correction` is the only writer and it
  refuses to run without the revision the preview showed.

* **It invents no write semantics.** A file-level supersession is
  :func:`palinode.core.save.save_memory` for the replacement plus
  :func:`palinode.consolidation.archive.archive_memory` for the original — the
  same two calls the Quickstart walks a person through by hand. A claim-level
  one is the executor's own ``SUPERSEDE`` / ``ARCHIVE`` op
  (:func:`palinode.consolidation.executor.apply_operations`) followed by the
  commit the runner makes. Nothing here writes a file; everything routes
  through the existing validated write path, so the git provenance, the
  ``-history.md`` audit sibling and the index propagation are whatever those
  contracts already produce.

* **It refuses rather than guesses.** A stale revision (the file moved since
  the preview) is :class:`StaleRevisionError`. A ref that names more than one
  memory, or a claim id that names more than one line, is
  :class:`AmbiguousTargetError` carrying the candidates so the caller can
  choose. A candidate whose span never named what it replaced is
  :class:`TargetRequiredError` — the reviewer picks the target, because an
  absent relation is the honest record of "the user did not name one" and
  inferring it is a proposal to change the wrong memory.

* **A half-finished write is reported, not raised.** A document-level
  supersession is two writes — save the replacement, retire the original — and
  there is no transaction over both. When the second one fails the store is in
  a real, nameable state, and the caller gets it as ``applied: "partial"``
  (:data:`APPLIED_PARTIAL`) naming what was written, what was not, and the one
  command that finishes the job. An exception here would say only "it failed",
  which is the one thing that is not true.

  **The order is deliberate and is not reversed.** Either order leaves a
  partial state, so the choice is which one: saving first leaves *two* current
  records, one of which says it supersedes the other — nothing has left recall
  and no reference dangles. Archiving first would leave the original retired
  with ``superseded_by`` pointing at a record that does not exist, so the
  store would be missing the answer entirely until someone repaired it. A
  visible duplicate is recoverable by reading; an absence is not noticed.

**What this is not.** It is not a truth heuristic: nothing here decides whether
a statement is correct. A reviewer decides, and the record of that decision is
the actor on the commit and the history line. Ordinary later observations still
never retire anything — the only retirement path through this module is an
explicit confirmed apply.
"""
from __future__ import annotations

import glob
import hashlib
import logging
import os
import re
from typing import Any, Iterable

import frontmatter

from palinode.core import path_guard
from palinode.core.config import config
from palinode.core.parity import CORRECTION_APPLIED_PARTIAL
from palinode.core.revalidation import normalize_ref
from palinode.corrections.content import content_loss
from palinode.corrections.queue import load_candidates

logger = logging.getLogger("palinode.corrections.review")

#: The relation a confirmed correction records. ``supersede`` names a
#: replacement; ``retire`` withdraws the target with no successor.
ACTION_SUPERSEDE = "supersede"
ACTION_RETIRE = "retire"
ACTIONS: tuple[str, ...] = (ACTION_SUPERSEDE, ACTION_RETIRE)

#: Where a correction came from. Never inferred — the caller says which.
SOURCE_USER = "user_statement"
SOURCE_CANDIDATE = "candidate"

#: The revision domain this contract compares. Deliberately the whole-file
#: hash (``palinode.core.receipt.REVISION_FILE``) and not the indexer's
#: per-section ``content_hash``: a correction is a statement about a document
#: as it stands on disk, and frontmatter edits (a ``status`` flip, a typed
#: link) must invalidate a preview even though they move no section body.
REVISION_BASIS = "file_sha256"

#: The actor recorded on the commit subject and the history line. A reader of
#: ``git log`` can tell a reviewed correction from a consolidation pass and
#: from a bare operator archive without opening anything.
ACTOR = "reviewed-correction"

#: ``palinode.core.save``'s ADR-010 source attribution for the replacement.
SAVE_SOURCE = "correction-review"

#: ``applied`` when the replacement was saved and the original was not
#: retired. The canonical value lives in :mod:`palinode.core.parity` with the
#: other cross-surface enums; it is bound here so this module's callers read it
#: from the contract they already import.
APPLIED_PARTIAL = CORRECTION_APPLIED_PARTIAL

#: The step a partial apply stopped at. One name, used by every surface.
PARTIAL_STEP_ARCHIVE = "archive_memory"

#: Why the correction undo is not the way out of a partial apply. Stated in
#: the result because "there is a recovery command" and "that command applies
#: to this state" are different claims: undo restores an *archived* original,
#: and a partial apply is exactly the case where nothing was archived.
PARTIAL_UNDO_UNAVAILABLE = (
    "the correction undo cannot unwind this. It restores an archived original, "
    "and this original was never archived — `palinode corrections undo` "
    "refuses a record whose status is still active, and the replacement is a "
    "record it never archived either"
)

#: What an undo cannot do, stated in the result rather than left to be
#: inferred from the absence of a field. These three are different things and
#: conflating them is how "undo" becomes a promise the store cannot keep.
UNDO_RESTORES = (
    "restores the previous assertion: the archived record returns to "
    "status: active and to default recall"
)
UNDO_DOES_NOT_DELETE_HISTORY = (
    "does not delete history: the correction's commits, the -history.md "
    "sibling and the replacement record all remain. Deleting history is not "
    "offered here"
)
UNDO_CANNOT_REACH_EXTERNAL = (
    "cannot undo external actions: anything an agent did while acting on the "
    "corrected memory — code written, messages sent, requests made — is "
    "outside this store and is not reachable from here"
)

#: The executor's fact marker, re-used rather than re-spelled.
_FACT_LINE_RE = re.compile(r"^[\s]*[-*]\s+(.*?)<!-- fact:(\S+) -->", re.MULTILINE)

_ARCHIVED = "archived"


class CorrectionError(Exception):
    """A correction could not be previewed or applied. Never a partial write."""

    code = "correction_error"

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "detail": str(self)}


class TargetNotFoundError(CorrectionError):
    code = "target_not_found"


class AmbiguousTargetError(CorrectionError):
    """More than one memory (or claim) matched. The candidates are returned."""

    code = "ambiguous_target"

    def __init__(self, message: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.candidates = candidates

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "candidates": self.candidates}


class TargetRequiredError(CorrectionError):
    """The correction names no target and none can honestly be derived."""

    code = "target_required"

    def __init__(self, message: str, candidates: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "candidates": self.candidates}


class StaleRevisionError(CorrectionError):
    """The target changed between the preview and the apply."""

    code = "stale_revision"

    def __init__(self, message: str, expected: str, actual: str) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "expected": self.expected, "actual": self.actual}


class CorrectionRefused(CorrectionError):
    """A document's own declared regime refused the operation."""

    code = "refused"


# ── revision ────────────────────────────────────────────────────────────────
def file_revision(abs_path: str) -> str:
    """The target's exact source revision: SHA-256 over the file's bytes."""
    with open(abs_path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ── target resolution ───────────────────────────────────────────────────────
def _memory_root() -> str:
    return os.path.realpath(config.memory_dir)


def _candidate_rows(rels: Iterable[str]) -> list[dict[str, Any]]:
    """Describe matches for an ambiguity report — path plus what is in it."""
    rows: list[dict[str, Any]] = []
    for rel in sorted(rels):
        abs_path = os.path.join(config.memory_dir, rel)
        title = ""
        status = ""
        try:
            with open(abs_path, encoding="utf-8") as handle:
                post = frontmatter.load(handle)
            title = str(post.metadata.get("title") or post.metadata.get("name") or "")
            status = str(post.metadata.get("status") or "")
        except (OSError, ValueError):
            pass
        rows.append({"file": rel, "title": title, "status": status})
    return rows


def resolve_target(ref: str) -> tuple[str, str]:
    """Resolve a caller-supplied memory ref to ``(rel_path, abs_path)``.

    Every spelling goes through :func:`palinode.core.path_guard
    .resolve_memory_path` first, so ``..``, absolute paths, null bytes and
    symlinks out of the store are rejected before anything is read. A ref
    carrying no directory (``harbor-notes-storage``) is then looked up across
    the store — and a slug that names two files raises
    :class:`AmbiguousTargetError` with both, rather than picking the first.
    """
    raw = (ref or "").strip()
    if not raw:
        raise TargetRequiredError("no target memory was named")

    for candidate in (raw, raw if raw.endswith(".md") else f"{raw}.md"):
        base, resolved = path_guard.resolve_memory_path(candidate)
        if os.path.isfile(resolved):
            rel = os.path.relpath(resolved, base)
            return rel, os.path.join(config.memory_dir, rel)

    if "/" not in raw.replace(os.sep, "/"):
        stem = raw[:-3] if raw.endswith(".md") else raw
        # Validate the bare stem before it reaches a glob pattern.
        path_guard.resolve_memory_path(f"{stem}.md")
        root = _memory_root()
        matches = {
            os.path.relpath(found, root)
            for found in glob.glob(os.path.join(root, "**", f"{stem}.md"), recursive=True)
            if os.path.isfile(found)
        }
        if len(matches) == 1:
            rel = matches.pop()
            return rel, os.path.join(config.memory_dir, rel)
        if len(matches) > 1:
            raise AmbiguousTargetError(
                f"{len(matches)} memories are named {stem!r} — name the one you mean",
                _candidate_rows(matches),
            )

    raise TargetNotFoundError(f"no memory matches {raw!r}")


def claims_in(content: str) -> list[dict[str, str]]:
    """Every ``<!-- fact:id -->`` claim in a document body, in document order."""
    return [
        {"claim_id": match.group(2), "text": match.group(1).strip()}
        for match in _FACT_LINE_RE.finditer(content)
    ]


def resolve_claim(content: str, claim_id: str, rel: str) -> dict[str, str]:
    """Find one claim by id, refusing a duplicated id rather than taking the first.

    A fact id is derived from the line's text, so two byte-identical lines
    carry the same id. The executor's ops address *every* line an id names, so
    a correction aimed at "the" claim would silently rewrite both.
    """
    wanted = (claim_id or "").strip()
    if not wanted:
        raise TargetRequiredError("no claim id was named")
    matches = [claim for claim in claims_in(content) if claim["claim_id"] == wanted]
    if not matches:
        raise TargetNotFoundError(f"no claim {wanted!r} in {rel}")
    if len(matches) > 1:
        raise AmbiguousTargetError(
            f"claim id {wanted!r} names {len(matches)} lines in {rel} — "
            "the id is derived from the line text, so identical lines share it",
            [{"file": rel, "claim_id": wanted, "text": claim["text"]} for claim in matches],
        )
    return matches[0]


# ── the blast radius, reported and never changed ────────────────────────────
_LINK_FIELDS = ("supersedes", "superseded_by", "backed_by", "contradicts", "falsified_by")


def _refs_from(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        ref = value.get("ref") or value.get("source_id") or value.get("target")
        return [str(ref)] if ref else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_refs_from(item))
        return out
    return []


def referencing_records(rel: str) -> list[dict[str, str]]:
    """Records that quote or derive from the target. **Reported, never changed.**

    A correction is a statement about one memory. Whatever else cites it is a
    separate record with its own author and its own reason for citing, and
    rewriting those is how one reviewed decision becomes an unreviewed cascade.
    So they are listed — with the relation that names them — and left alone.
    (``backed_by`` dependents additionally get the existing ``stale_backing``
    flag from :mod:`palinode.consolidation.propagate` when the apply lands;
    that is a flag for review, not a rewrite.)
    """
    target_ref = normalize_ref(rel)
    wikilink = f"[[{os.path.splitext(os.path.basename(rel))[0]}]]"
    root = _memory_root()
    found: list[dict[str, str]] = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.md"), recursive=True)):
        try:
            if os.path.commonpath([root, os.path.realpath(path)]) != root:
                continue
        except ValueError:
            continue
        other = os.path.relpath(path, root)
        if other == rel:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                post = frontmatter.load(handle)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        relations: list[str] = []
        for field in _LINK_FIELDS:
            if any(normalize_ref(r) == target_ref for r in _refs_from(post.metadata.get(field))):
                relations.append(field)
        for field in ("sources", "claims"):
            if any(normalize_ref(r) == target_ref for r in _refs_from(post.metadata.get(field))):
                relations.append(f"{field} (quoted)")
        body = post.content or ""
        if wikilink in body:
            relations.append("wikilink")
        for relation in relations:
            found.append({"file": other, "relation": relation})
    return found


#: The note for an operation that retires nothing (restore, unretract): the
#: same records, reported for the same reason — this operation does not reach
#: them either.
REFERENCING_UNCHANGED_NOTE = (
    "Reported, never changed. Each record below quotes, cites or links this "
    "memory; this operation does not touch it."
)

#: How many retained copies a lifecycle result names before it says "N more".
RETAINED_COPIES_LIMIT = 10

RETAINED_COPIES_NOTE = (
    "Reported, never changed. Each record below quotes, cites or links the "
    "retired one; this operation does not reach it, so it stays in default "
    "recall exactly as it is. Retiring a record is not the same as taking its "
    "wording out of recall."
)

_RELATION_ACTIONS = {
    "sources (quoted)": "it quotes this record verbatim — archive it too "
    "(`palinode archive {file}`) or edit the quote out if the wording must "
    "leave recall",
    "claims (quoted)": "it quotes this record verbatim — archive it too "
    "(`palinode archive {file}`) or edit the quote out if the wording must "
    "leave recall",
    "backed_by": "it cites this record as support — review whether its claim "
    "still stands (`palinode read {file}`); an archive flags it "
    "`stale_backing`, it does not retire it",
    "contradicts": "it records a conflict with this record — edit its "
    "`contradicts` link if the conflict no longer applies",
    "falsified_by": "it records that this record falsified it — no action "
    "unless that relation is wrong",
    "supersedes": "it records lineage with this record — lineage is history, "
    "not support; no action needed",
    "superseded_by": "it records lineage with this record — lineage is "
    "history, not support; no action needed",
    "wikilink": "it links to this record by name — edit the link if it should "
    "stop pointing here",
}


def retained_copies(
    rels: str | Iterable[str],
    *,
    chain: Any = None,
    limit: int = RETAINED_COPIES_LIMIT,
    note: str = RETAINED_COPIES_NOTE,
) -> dict[str, Any]:
    """The copies a retirement does **not** reach, bounded and visibility-filtered.

    Built on :func:`referencing_records` — the same discovery the correction
    preview reports — so there is one definition of "a record that quotes,
    cites or derives from this one". What this adds is the lifecycle framing:
    only records still in default recall are listed (one already retired is
    not a copy that stays live), the records being retired together are not
    listed against each other, every row says what the user can do about it,
    at most ``limit`` rows are named with the remainder counted in ``more``,
    and a record ``chain`` may not see (:func:`palinode.core.visibility.is_visible`)
    is counted in ``not_visible`` and never named.
    """
    from palinode.core.lifecycle import eligibility
    from palinode.core.visibility import is_visible

    targets = [rels] if isinstance(rels, str) else list(rels)
    target_set = {normalize_ref(t) for t in targets}
    rows: dict[tuple[str, str], list[str]] = {}
    for target in targets:
        for row in referencing_records(target):
            if normalize_ref(row["file"]) in target_set:
                continue
            rows.setdefault((row["file"], target), []).append(row["relation"])

    visible: list[dict[str, Any]] = []
    not_visible: set[str] = set()
    for (other, target), relations in rows.items():
        abs_path = os.path.join(config.memory_dir, other)
        if not is_visible(chain, abs_path):
            not_visible.add(other)
            continue
        try:
            with open(abs_path, encoding="utf-8") as handle:
                meta = dict(frontmatter.load(handle).metadata)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if eligibility(meta, path=other).retired:
            continue
        actions = [
            _RELATION_ACTIONS.get(r, "review it").format(file=other)
            for r in dict.fromkeys(relations)
        ]
        visible.append({
            "file": other,
            "of": target,
            "relations": list(dict.fromkeys(relations)),
            "in_default_recall": True,
            "action": "; ".join(dict.fromkeys(actions)),
        })

    shown = visible[: max(limit, 0)]
    return {
        "records": shown,
        "total": len(visible) + len(not_visible),
        "more": len(visible) - len(shown),
        "not_visible": len(not_visible),
        "note": note,
    }


def retained_copies_reported(
    rels: str | Iterable[str], *, note: str = RETAINED_COPIES_NOTE
) -> dict[str, Any]:
    """:func:`retained_copies` for a result whose operation has already landed.

    The scan runs after the write, so it must not be able to turn a completed
    archive into a failure. A scan that raises is logged and reported in the
    block as *not checked* (``total: None`` plus ``error``) — never as an
    empty list, which would read as "no copies".
    """
    try:
        return retained_copies(rels, note=note)
    except Exception:
        logger.warning("retained-copy scan failed (non-fatal)", exc_info=True)
        return {
            "records": [],
            "total": None,
            "more": 0,
            "not_visible": 0,
            "note": note,
            "error": "the retained-copy scan failed; the operation's own "
            "result is accurate, but copies were not checked",
        }


def candidates_naming(rel: str, *, memory_dir: str | None = None) -> list[dict[str, Any]]:
    """Unresolved candidates whose own quoted span names text in this record.

    The match is evidence, not inference: a candidate is listed only when the
    text the user said was being replaced (``relation.replaced``, which the
    queue records **only** when the span itself supplied it) appears verbatim
    in this document. A candidate that named no replacement matches nothing —
    which is correct, and is why the reviewer has to choose its target.
    """
    abs_path = os.path.join(config.memory_dir, rel)
    try:
        with open(abs_path, encoding="utf-8") as handle:
            haystack = _normalize(handle.read())
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for record in load_candidates(memory_dir):
        if record.get("status") not in (None, "proposed"):
            continue
        replaced = (record.get("relation") or {}).get("replaced")
        if not replaced:
            continue
        if _normalize(str(replaced)) in haystack:
            out.append(record)
    return out


def _normalize(text: str) -> str:
    return " ".join(str(text).split()).casefold()


# ── candidate lookup ────────────────────────────────────────────────────────
def find_candidate(candidate_id: str, *, memory_dir: str | None = None) -> dict[str, Any]:
    """One queued candidate by id, with its source-availability verdict."""
    wanted = (candidate_id or "").strip()
    if not wanted:
        raise TargetRequiredError("no candidate id was named")
    for record in load_candidates(memory_dir):
        if record.get("candidate_id") == wanted:
            return record
    raise TargetNotFoundError(f"no correction candidate {wanted!r} in the queue")


def _source_block(candidate: dict[str, Any] | None) -> dict[str, Any]:
    """Describe where the correction came from, including an absent transcript.

    The queue stores the bounded span, never the transcript, so a transcript
    that has since been deleted or rotated does not weaken the evidence — the
    quoted span *is* the evidence, and was captured with no model involved.
    What changes is that the surrounding conversation can no longer be
    re-read, and the result says so rather than leaving a reader to assume it
    can be.
    """
    if candidate is None:
        return {
            "kind": SOURCE_USER,
            "detail": "the reviewer's own statement in this session",
            "available": True,
        }
    available, reason = transcript_availability(candidate)
    block: dict[str, Any] = {
        "kind": SOURCE_CANDIDATE,
        "candidate_id": candidate.get("candidate_id"),
        "harness": candidate.get("harness"),
        "session_id": candidate.get("session_id"),
        "turn_index": candidate.get("turn_index"),
        "turn_uuid": candidate.get("turn_uuid"),
        "span": candidate.get("span"),
        "span_hash": candidate.get("span_hash"),
        "occurred_at": candidate.get("occurred_at"),
        "classification": candidate.get("classification"),
        "classifier": candidate.get("classifier"),
        "project": candidate.get("project"),
        "available": available,
    }
    if not available:
        block["unavailable_reason"] = reason
    return block


def transcript_availability(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Can the candidate's originating transcript still be read?

    Deliberately conservative: the queue never recorded a transcript path (it
    records the session, turn and span), so this asks the configured harness
    roots whether a session file with that id is still there. "No" is a
    statement about the *surroundings*, not about the span.
    """
    session_id = str(candidate.get("session_id") or "")
    harness = str(candidate.get("harness") or "")
    configured = getattr(config.capture.transcripts, "harness_paths", {}) or {}
    roots = configured.get(harness) or []
    if not session_id:
        return False, "the candidate records no session id"
    if not roots:
        return False, (
            "the source is unavailable: no transcript path is configured for "
            f"harness {harness!r}. The stored span is still the evidence — it "
            "was captured verbatim from the user's own turn with no model "
            "involved — but the surrounding conversation cannot be re-read."
        )
    for root in roots:
        pattern = os.path.join(os.path.expanduser(str(root)), "**", f"{session_id}*")
        if glob.glob(pattern, recursive=True):
            return True, ""
    return False, (
        "the source is unavailable: the transcript for session "
        f"{session_id} is no longer at the configured path. The stored span is "
        "still the evidence — it was captured verbatim from the user's own "
        "turn with no model involved — but the surrounding conversation "
        "cannot be re-read."
    )


# ── preview ─────────────────────────────────────────────────────────────────
def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _target_block(rel: str, abs_path: str, post: frontmatter.Post) -> dict[str, Any]:
    from palinode.api.ui.discovery import discovery_visibility
    from palinode.consolidation import retirement

    policy, signal = retirement.classify(abs_path, post.metadata)
    update_policy = post.metadata.get("update_policy")
    return {
        "file": rel,
        "id": post.metadata.get("id") or normalize_ref(rel),
        "title": post.metadata.get("title") or post.metadata.get("name"),
        "type": post.metadata.get("type"),
        "status": str(post.metadata.get("status") or "active"),
        "core": bool(post.metadata.get("core", False)),
        "revision": file_revision(abs_path),
        "revision_basis": REVISION_BASIS,
        "update_policy": str(update_policy) if update_policy else None,
        "update_policy_source": (
            "inherited from the target's frontmatter" if update_policy
            else "the target declares none; the replacement declares none either"
        ),
        "retirement_policy": policy,
        "retirement_signal": signal,
        "visibility": discovery_visibility(rel, metadata=post.metadata),
    }


def preview_correction(
    *,
    target: str | None = None,
    claim_id: str | None = None,
    replacement: str | None = None,
    action: str | None = None,
    reason: str | None = None,
    candidate_id: str | None = None,
    project: str | None = None,
    backed_by: list[str] | None = None,
    type: str | None = None,
    slug: str | None = None,
    allow_content_loss: bool = False,
    memory_dir: str | None = None,
) -> dict[str, Any]:
    """Describe the correction that an apply would make. Writes nothing.

    ``target`` is a memory ref (``decisions/x.md``, ``decisions/x`` or a bare
    slug); ``claim_id`` narrows it to one ``<!-- fact:id -->`` line inside it.
    ``replacement`` is the text that would stand instead; omitting it (or
    passing ``action="retire"``) withdraws the target with no successor.
    ``candidate_id`` names the transcript-candidate queue row it came from —
    its span, session and turn become the recorded source, and its target is
    used only when the user's own words supplied one.

    ``backed_by`` is the replacement's **own** evidence: refs to records that
    support the new statement. It exists because the old statement must not be
    made to do that job — the archived original is lineage, and a verified
    quote of it establishes what that source said, never that the new claim is
    true. Nothing is cited automatically; an unevidenced correction is
    reported as unevidenced.
    """
    candidate = find_candidate(candidate_id, memory_dir=memory_dir) if candidate_id else None

    if candidate is not None:
        relation = candidate.get("relation") or {}
        if replacement is None and relation.get("replacement"):
            replacement = str(relation["replacement"])
        if reason is None and candidate.get("rationale"):
            reason = str(candidate["rationale"])
        if project is None and candidate.get("project"):
            project = str(candidate["project"])

    resolved_action = (action or (ACTION_SUPERSEDE if replacement else ACTION_RETIRE)).strip()
    if resolved_action not in ACTIONS:
        raise CorrectionError(f"action must be one of {', '.join(ACTIONS)}")
    if resolved_action == ACTION_SUPERSEDE and not (replacement or "").strip():
        raise CorrectionError("a supersede needs the replacement text that will stand")

    if not (target or "").strip():
        raise TargetRequiredError(
            "this correction names no target memory. "
            + (
                "The candidate's span did not say which memory it replaced, so "
                "there is nothing to infer from — choose the target explicitly."
                if candidate is not None
                else "Name the memory to correct."
            ),
            _candidates_for_hint(candidate),
        )

    rel, abs_path = resolve_target(str(target))
    with open(abs_path, encoding="utf-8") as handle:
        content = handle.read()
    post = frontmatter.loads(content)

    claim = resolve_claim(content, claim_id, rel) if claim_id else None
    old_text = claim["text"] if claim is not None else (post.content or "").strip()

    target_block = _target_block(rel, abs_path, post)
    warnings: list[str] = []
    refusal: str | None = None

    if target_block["status"] == _ARCHIVED:
        warnings.append(
            f"{rel} is already archived"
            + (f" (superseded by {post.metadata.get('superseded_by')})"
               if post.metadata.get("superseded_by") else "")
            + " — correcting it again changes nothing in default recall."
        )
    if claim is not None and target_block["update_policy"] == "replace":
        refusal = (
            f"{rel} declares update_policy: replace — a living document holding "
            "one current state. The executor refuses to fork one of its claims "
            "into history. Correct the whole document instead, "
            "or update the claim in place through a consolidation pass."
        )

    loss = content_loss(
        old_text, (replacement or "") if resolved_action == ACTION_SUPERSEDE else ""
    )
    loss["allowed"] = allow_content_loss
    if claim is not None or resolved_action == ACTION_RETIRE:
        loss["requires_confirmation"] = False
    if loss["requires_confirmation"]:
        warning = (
            "More than one original claim is absent from the replacement. "
            "These original words would stop being delivered from this record "
            "(including the claim being corrected):\n"
            + "\n".join(f"- {text}" for text in loss["removed_text"])
            + "\nKeep untouched claims in --replacement, select --claim for a "
            "fact-marked line, or explicitly pass --allow-content-loss "
            "(allow_content_loss=true) to drop the listed text."
        )
        warnings.append(warning)
        if not allow_content_loss and refusal is None:
            refusal = warning

    successor_ref = None
    if resolved_action == ACTION_SUPERSEDE:
        successor_ref = _successor_ref(rel, slug, claim is not None)

    source = _source_block(candidate)
    if project is None:
        project = _project_of(post.metadata)

    evidence = _validated_evidence(backed_by)
    if resolved_action == ACTION_SUPERSEDE and not evidence:
        warnings.append(
            "The replacement cites no supporting record (`backed_by` is empty). "
            "The archived original is lineage, not support: it records what was "
            "decided before, not a reason the new statement is right."
        )

    recovery = _recovery_block(rel, resolved_action, claim is not None)
    result: dict[str, Any] = {
        "phase": "preview",
        "applied": False,
        "action": resolved_action,
        "level": "claim" if claim is not None else "document",
        "target": target_block,
        "claim": claim,
        "old_text": old_text,
        "content_loss": loss,
        "new_text": (replacement or "").strip() or None,
        "rationale": reason,
        "source": source,
        "scope": {"project": project},
        "evidence": {
            "backed_by": evidence,
            "note": (
                "the replacement's own support, cited as typed backed_by links. "
                "The superseded original is never cited as support for its "
                "replacement."
            ),
        },
        "relation": _relation_block(rel, resolved_action, successor_ref, claim),
        "referencing_records": referencing_records(rel),
        "recovery": recovery,
        "capture_policy": _capture_policy_block(),
        "confirm": {
            "expect_revision": target_block["revision"],
            "command": _apply_command(
                rel, claim_id, replacement, resolved_action, reason,
                candidate_id, target_block["revision"], allow_content_loss,
            ),
        },
        "notes": [
            "The archived original stays on disk and in git; the supersedes / "
            "superseded_by pair records it as decision history. It is lineage, "
            "not support: a verified quote establishes what a source said, "
            "never that the claim is true."
            if resolved_action == ACTION_SUPERSEDE else
            "The record is retired with no successor: nothing replaces it, and "
            "its content stays on disk and in git.",
        ],
        "warnings": warnings,
    }
    if refusal:
        result["refused"] = refusal
        result["confirm"]["command"] = None
    if type:
        result["replacement_type"] = type
    return result


def _validated_evidence(backed_by: list[str] | None) -> list[str]:
    """Path-guard every supporting ref before it is written into frontmatter.

    A ``backed_by`` entry is a caller-supplied ref that lands in a memory file,
    so it goes through the same guard a target does. A ref that does not
    resolve inside the store is rejected outright rather than written and left
    for a reader to trip over.
    """
    refs: list[str] = []
    for raw in backed_by or []:
        ref = str(raw).strip()
        if not ref:
            continue
        path_guard.resolve_memory_path(ref if ref.endswith(".md") else f"{ref}.md")
        normalized = normalize_ref(ref)
        if normalized not in refs:
            refs.append(normalized)
    return refs


def _candidates_for_hint(candidate: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Nothing is guessed — an absent relation yields an empty candidate list."""
    if candidate is None:
        return []
    relation = candidate.get("relation") or {}
    replaced = relation.get("replaced")
    if not replaced:
        return []
    return [{"quoted_replaced_text": str(replaced)}]


def _project_of(metadata: dict[str, Any]) -> str | None:
    for entity in metadata.get("entities") or []:
        text = str(entity)
        if text.startswith("project/"):
            return text.removeprefix("project/")
    scope = metadata.get("scope")
    if isinstance(scope, str) and scope.startswith("project/"):
        return scope.removeprefix("project/")
    return None


def _successor_ref(rel: str, slug: str | None, claim_level: bool) -> str:
    if claim_level:
        return f"supersedes-<claim id> in {rel}"
    stem = slug or f"{os.path.splitext(os.path.basename(rel))[0]}-corrected"
    return f"<category>/{stem}.md (assigned by the save when the apply runs)"


def _relation_block(
    rel: str, action: str, successor_ref: str | None, claim: dict[str, str] | None
) -> dict[str, Any]:
    if action == ACTION_RETIRE:
        return {
            "kind": "retired",
            "retired": rel if claim is None else f"{rel}#{claim['claim_id']}",
            "successor": None,
            "recorded_as": (
                "status: archived in the target's frontmatter"
                if claim is None
                else "the claim's line is moved verbatim into the -history.md sibling"
            ),
        }
    return {
        "kind": "supersede",
        "supersedes": rel if claim is None else f"{rel}#{claim['claim_id']}",
        "superseded_by": successor_ref,
        "recorded_as": (
            "supersedes: on the replacement, superseded_by: + status: archived "
            "on the original"
            if claim is None
            else "the claim is struck through in place, marked [superseded], and "
                 "a supersedes-<id> line carries the new text"
        ),
    }


def _capture_policy_block() -> dict[str, Any]:
    """What the store's own capture policy says about applying this.

    An apply is an explicit, user-initiated write, and the policy's rule for
    those is already written: exclusions never block an explicit save, but a
    store-wide ``capture pause`` does — it stops explicit MCP/API calls too.
    Reported here so a reviewer sees the refusal in the preview rather than
    meeting it at the apply.
    """
    from palinode.core.capture_policy import evaluate_capture_policy

    decision = evaluate_capture_policy("capture", automatic=False)
    return {
        "apply_allowed": decision.allowed,
        "reason": decision.reason,
        "note": (
            "capture is paused for this store, so an explicit correction is "
            "refused like any other explicit save. Resume with "
            "`palinode controls resume --capture`."
            if not decision.allowed
            else "capture is active; an explicit correction may be applied."
        ),
    }


def _recovery_block(rel: str, action: str, claim_level: bool) -> dict[str, Any]:
    return {
        "history_command": f"palinode history {rel} --detail full",
        "diff_command": "palinode diff --days 1",
        "undo_preview_command": f"palinode corrections undo --target {rel}",
        "undo_command": f"palinode corrections undo --target {rel} --confirm",
        "restores": UNDO_RESTORES,
        "does_not_delete_history": UNDO_DOES_NOT_DELETE_HISTORY,
        "cannot_reach_external_actions": UNDO_CANNOT_REACH_EXTERNAL,
        "note": (
            "a claim-level correction is undone by rolling the file back "
            "(`palinode rollback " + rel + "`), not by restore: restore is the "
            "inverse of a document archive."
            if claim_level else
            "restore is the exact inverse of the archive this correction makes."
        ),
    }


def _apply_command(
    rel: str,
    claim_id: str | None,
    replacement: str | None,
    action: str,
    reason: str | None,
    candidate_id: str | None,
    revision: str,
    allow_content_loss: bool = False,
) -> str:
    parts = ["palinode corrections apply", "--target", rel]
    if claim_id:
        parts += ["--claim", claim_id]
    if action == ACTION_RETIRE:
        parts.append("--retire")
    elif replacement:
        parts += ["--replacement", _quote(replacement)]
    if reason:
        parts += ["--reason", _quote(reason)]
    if candidate_id:
        parts += ["--candidate", candidate_id]
    if allow_content_loss:
        parts.append("--allow-content-loss")
    parts += ["--expect-revision", revision, "--confirm"]
    return " ".join(parts)


# ── apply ───────────────────────────────────────────────────────────────────
def apply_correction(
    *,
    target: str,
    expect_revision: str,
    confirm: bool = False,
    claim_id: str | None = None,
    replacement: str | None = None,
    action: str | None = None,
    reason: str | None = None,
    candidate_id: str | None = None,
    project: str | None = None,
    backed_by: list[str] | None = None,
    type: str | None = None,
    slug: str | None = None,
    allow_content_loss: bool = False,
    memory_dir: str | None = None,
) -> dict[str, Any]:
    """Apply a previewed correction. Requires the preview's target revision.

    Nothing here writes a file. A document-level supersession saves the
    replacement through :func:`palinode.core.save.save_memory` and retires the
    original through :func:`palinode.consolidation.archive.archive_memory`; a
    claim-level one goes through the executor's own ops. Both leave the git
    provenance, the ``-history.md`` audit sibling and the index propagation
    those contracts already produce, with ``actor`` naming this as a reviewed
    correction and carrying the candidate id when one was the source.

    ``applied`` is ``True``, or :data:`APPLIED_PARTIAL` when the document-level
    save landed and the archive did not — see the module docstring for why that
    is a returned result rather than an exception, and why the two writes are
    in this order. The partial carries a ``partial`` block naming both halves
    and the command that completes it.

    A claim-level apply has no partial of that shape: it is one
    :func:`palinode.consolidation.executor.apply_operations` call over one
    file, whose mutation reaches disk in a single atomic write, so there is no
    second write of this contract's for a failure to fall between. What can
    still fail after it is the git commit, and that is already reported as
    ``committed: false`` rather than raised.
    """
    if not confirm:
        raise CorrectionError(
            "apply requires explicit confirmation — preview first, then confirm"
        )

    preview = preview_correction(
        target=target, claim_id=claim_id, replacement=replacement, action=action,
        reason=reason, candidate_id=candidate_id, project=project,
        backed_by=backed_by, type=type, slug=slug, memory_dir=memory_dir,
        allow_content_loss=allow_content_loss,
    )
    rel = preview["target"]["file"]
    abs_path = os.path.join(config.memory_dir, rel)
    current = file_revision(abs_path)
    if not expect_revision:
        raise StaleRevisionError(
            "apply needs the revision the preview showed", "", current
        )
    if expect_revision != current:
        raise StaleRevisionError(
            f"{rel} changed since the preview — re-run the preview and read the "
            "new text before confirming",
            expect_revision,
            current,
        )

    if preview.get("refused"):
        raise CorrectionRefused(preview["refused"])

    actor = ACTOR if not candidate_id else f"{ACTOR} candidate:{candidate_id}"
    effective_reason = preview.get("rationale") or "reviewed correction"

    if preview["level"] == "claim":
        result = _apply_claim(preview, abs_path, rel, actor, effective_reason)
    else:
        result = _apply_document(
            preview, rel, actor, effective_reason, type=type, slug=slug,
            project=preview["scope"]["project"],
            backed_by=preview["evidence"]["backed_by"],
        )

    partial = result.get("applied") == APPLIED_PARTIAL

    if candidate_id:
        if partial:
            # The queue row stays proposed: marking it `applied` would be the
            # store's only durable statement that this correction landed, and
            # it did not.
            result["partial"]["candidate"] = (
                f"candidate {candidate_id} was left unresolved in the queue — "
                "the correction it proposed is not finished"
            )
        else:
            from palinode.corrections.queue import resolve_candidate

            result["candidate"] = resolve_candidate(
                candidate_id,
                status="applied",
                reason=effective_reason,
                target=rel,
                memory_dir=memory_dir,
            )

    result.update(
        {
            "phase": "apply",
            "applied": APPLIED_PARTIAL if partial else True,
            "action": preview["action"],
            "level": preview["level"],
            "target": preview["target"],
            "old_text": preview["old_text"],
            "content_loss": preview["content_loss"],
            "new_text": preview["new_text"],
            "rationale": effective_reason,
            "source": preview["source"],
            "scope": preview["scope"],
            "referencing_records": preview["referencing_records"],
            "recovery": preview["recovery"],
            "actor": actor,
        }
    )
    return result


def _apply_document(
    preview: dict[str, Any],
    rel: str,
    actor: str,
    reason: str,
    *,
    type: str | None,
    slug: str | None,
    project: str | None,
    backed_by: list[str],
) -> dict[str, Any]:
    from palinode.consolidation.archive import archive_memory

    successor: str | None = None
    saved: dict[str, Any] | None = None

    if preview["action"] == ACTION_SUPERSEDE:
        from palinode.core.save import save_memory

        inherited_policy = preview["target"]["update_policy"]
        saved = save_memory(
            content=preview["new_text"],
            type=type or preview["target"]["type"] or "Decision",
            slug=slug or f"{os.path.splitext(os.path.basename(rel))[0]}-corrected",
            project=project,
            source=SAVE_SOURCE,
            # Inherited, never overridden: a living document's replacement is
            # a living document, an appending one keeps appending.
            update_policy=inherited_policy,
            # The replacement's own support. The old statement is deliberately
            # NOT among these — a verified quote establishes what a source
            # said, not that the claim is true.
            backed_by=backed_by or None,
            # Lineage, recorded as lineage.
            metadata={"supersedes": normalize_ref(rel)},
        )
        successor = saved.get("rel_path") or os.path.relpath(
            saved["file_path"], config.memory_dir
        )

    try:
        archived = archive_memory(
            rel, reason=reason, superseded_by=successor, actor=actor
        )
    except Exception as error:  # noqa: BLE001 — reported, never swallowed
        if saved is None:
            # A retirement writes nothing before this call, so a failure here
            # is a failure of the whole operation and stays an exception.
            raise
        logger.exception(
            "correction apply: %s was saved but %s was not retired", successor, rel
        )
        return _partial_document(preview, rel, saved, str(successor), reason, error)

    return {
        "archived": archived,
        "replacement": saved,
        "relation": {
            **preview["relation"],
            "superseded_by": successor,
        },
        "committed": bool(archived.get("committed")),
    }


def _partial_document(
    preview: dict[str, Any],
    rel: str,
    saved: dict[str, Any],
    successor: str,
    reason: str,
    error: Exception,
) -> dict[str, Any]:
    """The state the store is actually in when the archive step failed.

    Both halves are stated, because only one of them is visible from the other:
    a caller who is told the replacement exists can read it, and would have no
    way to learn from that record alone that the original it claims to
    supersede is still being served.
    """
    return {
        "applied": APPLIED_PARTIAL,
        "archived": None,
        "replacement": saved,
        "relation": {
            **preview["relation"],
            "superseded_by": successor,
            # Half of the relation is on disk. Saying so here keeps the
            # preview's sentence from being read as a record of what happened.
            "recorded_as": (
                "supersedes: on the replacement only — the original carries "
                "neither superseded_by nor status: archived"
            ),
        },
        "committed": bool(saved.get("git_committed")),
        "partial": {
            "failed_step": PARTIAL_STEP_ARCHIVE,
            "error": f"{type(error).__name__}: {error}",
            "written": (
                f"{successor} is saved, active, and carries "
                f"supersedes: {normalize_ref(rel)}"
            ),
            "not_written": (
                f"{rel} is still current — no status: archived, no "
                "superseded_by, still returned by default recall. The store "
                "now serves both statements"
            ),
            "successor": successor,
            "original": rel,
            "complete_command": (
                f"palinode archive {rel} --superseded-by {successor} "
                f"--reason {_quote(reason)}"
            ),
            "unwind_command": (
                f"palinode archive {successor} --reason "
                + _quote("unwinding a partially applied correction")
            ),
            "unwind_note": (
                "unwinding is retiring the replacement, which is a second "
                "decision with its own record: it leaves default recall and "
                "stays on disk and in git like anything else archived"
            ),
            "undo": PARTIAL_UNDO_UNAVAILABLE,
        },
    }


def _apply_claim(
    preview: dict[str, Any], abs_path: str, rel: str, actor: str, reason: str
) -> dict[str, Any]:
    from palinode.consolidation.executor import apply_operations
    from palinode.core import git_tools

    claim_id = preview["claim"]["claim_id"]
    if preview["action"] == ACTION_SUPERSEDE:
        op = {
            "op": "SUPERSEDE",
            "id": claim_id,
            "new_text": preview["new_text"],
            "rationale": f"{reason} [actor: {actor}]",
        }
    else:
        op = {
            "op": "ARCHIVE",
            "id": claim_id,
            "superseded_by": None,
            "rationale": f"{reason} [actor: {actor}]",
        }
    stats = apply_operations(abs_path, [op])
    if stats.get("protected_rejected"):
        raise CorrectionRefused(
            f"{rel} refused the operation on its own declared regime — see the "
            "server log for the guard that fired. Nothing was written."
        )
    if stats.get("unmatched"):
        raise TargetNotFoundError(
            f"claim {claim_id!r} was not matched in {rel}; nothing was written"
        )

    history = os.path.join(
        os.path.dirname(abs_path),
        f"{os.path.splitext(os.path.basename(abs_path))[0]}-history.md",
    )
    files = [abs_path] + ([history] if os.path.isfile(history) else [])
    verb = "supersede" if preview["action"] == ACTION_SUPERSEDE else "archive"
    committed = git_tools.commit_memory_files(
        files,
        f"{config.git.commit_prefix} correction {verb}: {rel}#{claim_id} "
        f"(actor: {actor})",
    )
    return {"stats": stats, "committed": committed, "relation": preview["relation"]}


# ── dismissal ───────────────────────────────────────────────────────────────
def dismiss_candidate(
    candidate_id: str, *, reason: str, memory_dir: str | None = None
) -> dict[str, Any]:
    """Record that a reviewer looked at a candidate and declined it.

    The row is marked, never removed: the dedupe key is ``(session id, span
    hash)`` and it is read from every row, so a dismissed candidate is exactly
    what stops the next scan from proposing the same sentence again.
    """
    if not (reason or "").strip():
        raise CorrectionError("a dismissal needs a reason — it is the record")
    from palinode.corrections.queue import resolve_candidate

    find_candidate(candidate_id, memory_dir=memory_dir)
    return {
        "phase": "dismiss",
        "applied": False,
        "candidate": resolve_candidate(
            candidate_id, status="dismissed", reason=reason, memory_dir=memory_dir
        ),
        "note": (
            "the candidate stays in the queue as a dismissed row. That is what "
            "keeps a re-scan from proposing the same span again: the dedupe key "
            "is (session id, span hash) and it is read from every row."
        ),
    }


# ── recovery ────────────────────────────────────────────────────────────────
def preview_undo(*, target: str, memory_dir: str | None = None) -> dict[str, Any]:
    """Describe the undo that :func:`apply_undo` would make. Writes nothing."""
    rel, abs_path = resolve_target(target)
    with open(abs_path, encoding="utf-8") as handle:
        post = frontmatter.load(handle)

    status = str(post.metadata.get("status") or "active")
    successor = post.metadata.get("superseded_by")
    revision = file_revision(abs_path)

    from palinode.core import git_tools

    try:
        history = git_tools.history(rel, 20, detail="summary") or []
    except Exception:  # noqa: BLE001 — git failure must not 500 a read
        history = []

    refused: str | None = None
    if status != _ARCHIVED:
        refused = (
            f"{rel} is not archived (status: {status}) — there is nothing for an "
            "undo to restore. `palinode history` shows what did change."
        )
    else:
        blocked = _separately_retired(post, rel)
        if blocked:
            refused = blocked

    result: dict[str, Any] = {
        "phase": "preview",
        "applied": False,
        "action": "undo",
        "target": {
            "file": rel,
            "status": status,
            "superseded_by": str(successor) if successor else None,
            "revision": revision,
            "revision_basis": REVISION_BASIS,
        },
        "restores": UNDO_RESTORES,
        "does_not_delete_history": UNDO_DOES_NOT_DELETE_HISTORY,
        "cannot_reach_external_actions": UNDO_CANNOT_REACH_EXTERNAL,
        "history": history,
        "also_true": [
            "the replacement record is left exactly as it is. Retiring it is a "
            "second decision with its own preview, not part of this undo."
        ],
        "capture_policy": _capture_policy_block(),
        "confirm": {
            "expect_revision": revision,
            "command": (
                f"palinode corrections undo --target {rel} "
                f"--expect-revision {revision} --confirm"
            ),
        },
    }
    if refused:
        result["refused"] = refused
        result["confirm"]["command"] = None
    return result


def _separately_retired(post: frontmatter.Post, rel: str) -> str | None:
    """Refuse an undo that would resurrect something retired for another reason.

    A record withdrawn by ``forget`` or carrying a mention-level retraction was
    not archived by this correction flow, and restoring it would put content
    back into recall that a separate, deliberate act removed. Those have their
    own surfaces (``palinode forget-withdraw``, ``palinode unretract``) and an
    undo must not stand in for either.
    """
    if post.metadata.get("retracted_prefs"):
        return (
            f"{rel} carries a mention-level retraction (`retracted_prefs`). An "
            "undo would put retracted content back into recall. Withdraw the "
            "retraction deliberately with `palinode unretract` first."
        )
    forget = post.metadata.get("forgotten_at") or post.metadata.get("forget_request")
    if forget:
        return (
            f"{rel} was withdrawn by a forget request, not by a correction. Use "
            "`palinode forget-withdraw` — an undo must not quietly reverse a "
            "deletion request."
        )
    return None


def apply_undo(
    *,
    target: str,
    expect_revision: str,
    confirm: bool = False,
    reason: str | None = None,
    memory_dir: str | None = None,
) -> dict[str, Any]:
    """Restore the previous assertion. Requires the undo preview's revision."""
    if not confirm:
        raise CorrectionError(
            "undo requires explicit confirmation — preview first, then confirm"
        )
    preview = preview_undo(target=target, memory_dir=memory_dir)
    if preview.get("refused"):
        raise CorrectionRefused(preview["refused"])

    rel = preview["target"]["file"]
    abs_path = os.path.join(config.memory_dir, rel)
    current = file_revision(abs_path)
    if expect_revision != current:
        raise StaleRevisionError(
            f"{rel} changed since the undo preview — re-run it before confirming",
            expect_revision,
            current,
        )

    from palinode.consolidation.archive import restore_memory

    restored = restore_memory(
        rel, reason=reason or f"undo of a {ACTOR}"
    )
    return {
        "phase": "apply",
        "applied": True,
        "action": "undo",
        "target": preview["target"],
        "restored": restored,
        "restores": UNDO_RESTORES,
        "does_not_delete_history": UNDO_DOES_NOT_DELETE_HISTORY,
        "cannot_reach_external_actions": UNDO_CANNOT_REACH_EXTERNAL,
        "also_true": preview["also_true"],
        "committed": bool(restored.get("committed")),
    }


__all__ = [
    "ACTIONS",
    "ACTION_RETIRE",
    "ACTION_SUPERSEDE",
    "ACTOR",
    "APPLIED_PARTIAL",
    "PARTIAL_STEP_ARCHIVE",
    "PARTIAL_UNDO_UNAVAILABLE",
    "AmbiguousTargetError",
    "CorrectionError",
    "CorrectionRefused",
    "REVISION_BASIS",
    "SOURCE_CANDIDATE",
    "SOURCE_USER",
    "StaleRevisionError",
    "TargetNotFoundError",
    "TargetRequiredError",
    "apply_correction",
    "apply_undo",
    "candidates_naming",
    "claims_in",
    "dismiss_candidate",
    "file_revision",
    "find_candidate",
    "preview_correction",
    "preview_undo",
    "referencing_records",
    "resolve_claim",
    "retained_copies",
    "retained_copies_reported",
    "resolve_target",
    "transcript_availability",
]
