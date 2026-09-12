"""Write-time forgetting: turn an explicit "please forget X" into archival.

Evaluation found that forgetting compliance is a *write-time* problem: read-time exclusion
of ``status: archived`` already exists and is on by default, but nothing ever
turned a user's forget request into that status. It also found the shape that
works — and the shape that backfires:

- archiving the pref-carrying memories while the forget request itself stays
  retrievable doubled compliance (3/18 → 6/18 paired, nothing broken);
- archiving every trace of the pref scored 0/18, *worse than doing nothing*,
  because the model re-personalizes on residual or neighboring preferences the
  moment the visible "user asked to forget X" constraint disappears.

So the transaction this module implements is a retraction, not an erasure: the
request memory is saved and indexed normally *first* (the save-path hook runs
after ``index_file``), and only the resolved pref-carrying memories are
retired — at the granularity that fits each one. A memory mostly *about* the
pref archives whole via
:func:`palinode.consolidation.archive.archive_memory`, with ``superseded_by``
pointing back at the request file so ``palinode trace`` reads the retirement
as the forget request superseding the preference; a dense shared memory that
merely mentions the pref keeps its file and loses exactly the mentioning
sentences (:func:`palinode.consolidation.retract.retract_mentions`, the
visible-strikethrough shape). Whole-file pref coverage routes between the two
(:func:`pref_coverage`).

DETECTION IS DETERMINISTIC, BY DESIGN. A false positive here archives someone's
real memories on an innocent sentence, so v0 fires only on explicit,
first-person request forms ("please forget that I…", "could you forget my…",
sentence-initial "Forget the detail about me…"). Assistant acknowledgements
("Got it — I'll forget that you…") and self-reports ("I always forget my keys")
must not fire; the object of the verb is required to be first-person, and the
trigger verb is required to be adjacent to its politeness/aux marker, which is
what keeps "please don't forget…" and "could you not forget…" out. Paraphrase
detection ("stop bringing up…") is a possible future extension and is
deliberately absent from v0.

RESOLUTION REUSES SEARCH — FOR RANKING, NOT FOR SCORING. The benchmark showed
retrieval already surfaces the right pref material, so the resolver is the
store's own hybrid search over the extracted pref phrase
(``record_access=False`` — resolution is a maintenance scan and must not
inflate recall counts, same rationale as ``store.search_internal``). But
post-RRF scores are RANK ARTIFACTS: on the measurement rig, two unrelated
requests produced byte-identical score sequences (1.0, 0.4919, 0.4841, …), so
an absolute score threshold is meaningless and archived cross-pref noise —
including *other prefs' forget requests*, destroying their tombstones. The
precision guards are therefore rank + lexical: a tight ``max_targets`` cap
(the validated intervention archived exactly the two establishing messages)
and a shared-content-word floor with the pref phrase, which keeps paraphrased
establishing evidence while dropping template-similar unrelated requests
(their only common words are stopwords). Resolution will still miss scattered
restatements — measured, not assumed — which
is exactly why the retained request memory is load-bearing: it is the safety
net for what resolution misses.

A REQUEST RESOLVES ONLY WHAT EXISTED WHEN IT WAS MADE. The same request text
reaches the save path more than once — the SessionEnd floor hook re-captures
a transcript the user already saved a message from, a session-end summary
lands on the same slug twice — and each arrival re-runs detection. The
retraction path was already idempotent (``retracted_prefs``); the archival
path was not: a re-capture would archive pref memories the user created
*after* the original request, which nobody asked to forget. The boundary
that closes this is a timestamp, not a new ledger: the request memory's own
``created_at`` (preserved across re-saves of the same slug) and, when the
same normalized pref already has an earlier retained request record, that
record's ``created_at`` — the earliest wins. A candidate whose effective
creation (``restored_at`` when it has been restored, else ``created_at``)
is later than the boundary is not this request's to retire. Same-pref
request records are never targets either; they are the tombstones.

The corollary is the honest one: to forget a memory created after the
request, make a new request. And to take a request back,
:func:`withdraw_forget_request` restores what it archived, un-strikes what it
retracted, and archives the request records themselves so they stop acting
as tombstones and stop bounding. The limit of the timestamp rule follows
from that: a same-pref request arriving *after* a withdrawal is a new
request — its ``created_at`` postdates the restorations — whether the user
re-asked or a floor-hook re-capture of the withdrawn transcript landed late.
The two are indistinguishable by text; the save result names what it
retired, and a second withdrawal reverses it.
"""
from __future__ import annotations

import glob
import logging
import os
import re
from datetime import UTC, date, datetime
from typing import Any, Callable

from palinode.core.config import config

logger = logging.getLogger("palinode.forget")

# Explicit request forms. Adjacency between the marker and "forget" is the
# negation guard: "please don't forget…" / "could you not forget…" break the
# adjacency and never reach the object check.
_REQUEST_RE = re.compile(
    r"""
    (?:
        (?:please|kindly)\s+forget
      | (?:can|could|would)\s+you\s+(?:please\s+)?forget
      | i(?:'d|\s+would)?\s+(?:like|want|need)\s+you\s+to\s+forget
      | ^\s*forget
    )
    \s+(?P<obj>[^\n]+)
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)

# The object of the verb must be first-person. This is what separates a user's
# request ("forget that I…", "forget my…", "forget the detail about me…") from
# an assistant's acknowledgement ("forget that you…", "forget your…", "forget
# that preference").
_FIRST_PERSON_OBJ_RE = re.compile(
    r"^(?:that\s+)?(?:the\s+)?(?:details?\s+(?:about|of)\s+)?(?:about\s+)?"
    r"(?:my|me|i)\b",
    re.IGNORECASE,
)

# Leading noise stripped from the extracted phrase; the remainder is the
# search query, so it should read like the preference it names.
_LEAD_STRIP_RE = re.compile(
    r"^(?:that\s+)?(?:the\s+)?(?:details?\s+(?:about|of)\s+)?(?:about\s+)?",
    re.IGNORECASE,
)
# "…from your memory." style tails add nothing to the search query.
_TAIL_STRIP_RE = re.compile(
    r"\s*(?:from\s+(?:your|the)\s+memory)?\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# Minimal stopword set for the shared-content-word resolution guard. Small on
# purpose: its only job is to keep request-template words ("please forget
# that I …") and grammatical glue from counting as evidence that a candidate
# carries the pref.
_STOPWORDS = frozenset(
    "the a an and or to of in on for with that this these those i my me is "
    "are was were be been have has had do does did as at by from about "
    "please forget you your our it its they their he she".split()
)


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower())
            if w not in _STOPWORDS and len(w) > 2}


def _file_body(path: str) -> str:
    """The file's markdown body, frontmatter stripped (best-effort)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:]
    return text


def pref_coverage(pref_words: set[str], body: str) -> float:
    """Fraction of ``body``'s content words shared with the pref phrase.

    The granularity router's metric: archival is file-shaped, so the
    question is not "does this file carry the pref" (resolution already
    answered that) but "is this file *about* the pref" — a dense
    ProjectSnapshot that mentions a person twice among hundreds of other
    content words scores ~0.01; a short establishing memory scores 0.15+.
    Measured over the whole file body, never the matching chunk, because the
    file is what the archive op removes from recall. Retraction markers are
    stripped before measuring so a previously-struck body scores the same as
    it did before striking — marker text must never feed back into the
    routing decision.
    """
    from palinode.consolidation.retract import _MARKER_RE

    body_words = _content_words(_MARKER_RE.sub(" ", body))
    if not body_words:
        return 0.0
    return len(body_words & pref_words) / len(body_words)


def detect_forget_request(text: str) -> str | None:
    """Return the forgotten-preference phrase, or ``None`` when no explicit
    first-person forget request is present.

    The phrase is the request's object, cut at the end of its sentence and
    stripped of framing ("that", "the detail about", trailing "from your
    memory") — e.g. ``"Please forget that I collect sneakers."`` →
    ``"I collect sneakers"``.
    """
    if not text or "forget" not in text.lower():
        return None
    for m in _REQUEST_RE.finditer(text):
        obj = m.group("obj").strip()
        if not _FIRST_PERSON_OBJ_RE.match(obj):
            continue
        # One request = one sentence: cut at the first terminator so a
        # multi-sentence message doesn't bleed into the search query.
        obj = re.split(r"(?<=[.!?])\s", obj, maxsplit=1)[0]
        obj = _LEAD_STRIP_RE.sub("", obj)
        obj = _TAIL_STRIP_RE.sub("", obj)
        obj = obj.strip().strip("\"'“”‘’")
        if obj:
            return obj
    return None


def _frontmatter_of(file_path: str) -> dict[str, Any]:
    """The file's frontmatter, or ``{}`` when it cannot be read or parsed.

    Fail-open by design at every call site: an unreadable candidate must not
    be silently dropped from resolution over a transient read error, and a
    missing timestamp must not bound anything.
    """
    import frontmatter

    try:
        with open(file_path, encoding="utf-8") as f:
            return dict(frontmatter.load(f).metadata)
    except (OSError, ValueError):
        return {}


def _coerce_ts(value: object) -> datetime | None:
    """A frontmatter timestamp as an aware UTC datetime, or ``None``.

    YAML hands back ``datetime``/``date`` objects for bare timestamps and
    strings for anything it does not recognize; the save path writes ISO
    strings. Naive values are taken as UTC, which is what the save path
    stamps.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def effective_created_at(meta: dict[str, Any]) -> datetime | None:
    """When this memory (re-)entered the live store.

    ``restored_at`` when the memory has been brought back from archive, else
    ``created_at``. A restored memory counts as created at its restoration:
    the request that retired it must not retire it again on re-capture,
    while a request made after the restore still can.
    """
    return _coerce_ts(meta.get("restored_at")) or _coerce_ts(meta.get("created_at"))


def is_forget_request_for(content: str, normalized_pref: str) -> bool:
    """True when ``content`` is itself a forget request for this pref."""
    from palinode.consolidation.retract import normalize_pref

    found = detect_forget_request(content)
    return found is not None and normalize_pref(found) == normalized_pref


def _search_pref(pref: str) -> list[dict[str, Any]]:
    """Hybrid-search hits for the pref phrase, in rank order (never thresholded)."""
    from palinode.core import embedder, store

    emb = embedder.embed(pref)
    return store.search_hybrid(
        pref,
        emb,
        top_k=config.consolidation.forget.search_k,
        threshold=0.0,  # RRF scores are rank artifacts; never threshold them
        record_access=False,
    )


def find_forget_requests(
    pref: str,
    exclude_paths: set[str] | None = None,
    hits: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Live request records for this pref: search hits that are themselves a
    forget request for the same normalized phrase, minus ``exclude_paths``.

    Search-bounded (``search_k``) and search-scoped: an archived record is
    not live and does not appear, which is what lets a withdrawn request
    stop bounding later ones. One entry per file, rank order.
    """
    from palinode.consolidation.retract import normalize_pref

    norm = normalize_pref(pref)
    exclude = exclude_paths or set()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for h in hits if hits is not None else _search_pref(pref):
        fp = h.get("file_path")
        if not fp or fp in exclude or fp in seen:
            continue
        if is_forget_request_for(h.get("content") or "", norm):
            seen.add(fp)
            out.append(h)
    return out


def resolution_boundary(
    request_path: str,
    pref: str,
    hits: list[dict[str, Any]] | None = None,
) -> tuple[datetime | None, list[str]]:
    """The instant this request may resolve up to, and the prior records that set it.

    The earliest of the request memory's own ``created_at`` and every live
    prior request record's ``created_at`` for the same pref. Returns
    ``(boundary, prior_request_abs_paths)``; ``boundary`` is ``None`` only
    when no timestamp is available anywhere, in which case resolution is
    unbounded (today's behaviour).
    """
    stamps: list[datetime] = []
    own = _coerce_ts(_frontmatter_of(request_path).get("created_at"))
    if own is not None:
        stamps.append(own)
    prior: list[str] = []
    for h in find_forget_requests(pref, exclude_paths={request_path}, hits=hits):
        prior.append(h["file_path"])
        ts = _coerce_ts(_frontmatter_of(h["file_path"]).get("created_at"))
        if ts is not None:
            stamps.append(ts)
    return (min(stamps) if stamps else None), prior


def resolve_forget_targets(
    pref: str,
    exclude_paths: set[str] | None = None,
    candidate_filter: Callable[[dict[str, Any]], bool] | None = None,
    before: datetime | None = None,
    hits: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve a preference phrase to the stored memories that carry it.

    Hybrid search over the phrase for *ranking* only — post-RRF scores are
    rank artifacts and are never thresholded (module docstring) — minus the
    request's own file (``exclude_paths``, absolute paths as stored in
    ``chunks.file_path``). Precision comes from two guards, both from
    ``config.consolidation.forget``: candidates must share at least
    ``min_shared_words`` content words with the pref phrase, and at most
    ``max_targets`` survivors are returned in rank order. Archived memories
    never appear (search excludes them already), so re-processing a request
    is naturally idempotent.

    A file whose ``retracted_prefs`` frontmatter already records this pref is
    not a candidate: its mentions are already struck, and letting it
    re-resolve would burn ``max_targets`` slots on every later same-pref
    request while genuinely new pref-carrying memories go unreached. A file
    that is itself a forget request for this pref is never a candidate: it
    is a tombstone, and archiving it would destroy the retained record.

    ``before`` is the resolution boundary (:func:`resolution_boundary`): a
    candidate whose :func:`effective_created_at` is later is not this
    request's to retire. ``None`` leaves resolution unbounded.

    ``candidate_filter`` is an optional predicate over result dicts; the
    replay-measurement harness uses it to restrict resolution to memories
    that existed before the request. ``hits`` lets a caller that already
    searched for the pref reuse the result instead of embedding twice.
    """
    from palinode.consolidation.retract import normalize_pref

    cfg = config.consolidation.forget
    pref_words = _content_words(pref)
    norm = normalize_pref(pref)
    if hits is None:
        hits = _search_pref(pref)
    exclude = exclude_paths or set()
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for h in hits:
        fp = h.get("file_path")
        if not fp or fp in exclude or fp in seen:
            continue
        if candidate_filter is not None and not candidate_filter(h):
            continue
        content = h.get("content") or ""
        shared = _content_words(content) & pref_words
        if len(shared) < cfg.min_shared_words:
            continue
        if is_forget_request_for(content, norm):
            seen.add(fp)
            continue
        meta = _frontmatter_of(fp)
        recorded = meta.get("retracted_prefs")
        if isinstance(recorded, list) and norm in recorded:
            seen.add(fp)
            continue
        if before is not None:
            created = effective_created_at(meta)
            if created is not None and created > before:
                seen.add(fp)
                continue
        seen.add(fp)
        targets.append(h)
        if len(targets) >= cfg.max_targets:
            break
    return targets


def check_forget_on_save(file_path: str, content: str) -> dict[str, Any] | None:
    """The save-path hook: detect → resolve → route by granularity → apply.

    ``file_path`` is the just-saved memory's absolute path (already indexed —
    the hook runs after ``index_file``, so the request itself stays active and
    retrievable, which the measurement showed is load-bearing). Returns
    ``None`` when nothing was detected, else a summary dict for the save
    response: ``archived`` (always; whole-file retirements), ``retracted``
    (mention-level strikes in dense shared memories, with counts — see
    :func:`pref_coverage` and :mod:`palinode.consolidation.retract`),
    ``skipped`` (low-coverage targets with no strikeable span), and
    ``failed`` (op errors) — the last three only when non-empty — plus
    ``resolved_before`` (the resolution boundary, see
    :func:`resolution_boundary`) and ``prior_requests`` (earlier live
    records for the same pref that set it) when present.

    Never raises on a target that fails to archive or retract: each target is
    its own mutation, and a partial retraction plus the retained request
    memory still beats a failed save.
    """
    pref = detect_forget_request(content)
    if pref is None:
        return None

    from palinode.consolidation.archive import archive_memory

    cfg = config.consolidation.forget
    base = os.path.realpath(config.memory_dir)
    request_rel = os.path.relpath(os.path.realpath(file_path), base)
    pref_words = _content_words(pref)
    hits = _search_pref(pref)
    boundary, prior = resolution_boundary(file_path, pref, hits=hits)
    targets = resolve_forget_targets(
        pref, exclude_paths={file_path}, before=boundary, hits=hits
    )

    archived: list[str] = []
    retracted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[str] = []
    for t in targets:
        rel = os.path.relpath(os.path.realpath(t["file_path"]), base)
        # Granularity router: forgetting is fact/entity-shaped, the archive
        # op is file-shaped. Coverage decides which granularity fits the
        # target: a memory mostly *about* the pref archives whole (it is the
        # pref's memory), while a dense shared memory that merely mentions it
        # gets mention-level retraction — the matching sentences are struck
        # in place and everything around them stays live. Skip is only the
        # fallback when no strikeable span exists; either way nothing is
        # silent, and the retained request memory covers whatever the finer
        # granularity misses.
        if cfg.min_target_coverage > 0.0:
            try:
                coverage = pref_coverage(pref_words, _file_body(t["file_path"]))
            except OSError:
                # Unreadable router input FAILS CLOSED: a transient read
                # error must produce the conservative outcome (skip +
                # report), never the maximal-blast-radius one (the archive's
                # own read succeeding moments later and retiring the file).
                logger.warning(
                    "forget: skipped %s — coverage unreadable, refusing to "
                    "route without it (non-fatal)", rel, exc_info=True,
                )
                skipped.append({"path": rel, "status": "unreadable"})
                continue
            if coverage < cfg.min_target_coverage:
                from palinode.consolidation.retract import retract_mentions

                try:
                    out = retract_mentions(rel, pref, superseded_by=request_rel)
                except Exception:
                    # Broad on purpose: the retract path spans file write,
                    # git, sqlite, and the embedder — any of them failing is
                    # this target's failure, not the save's.
                    logger.warning(
                        "forget: failed to retract mentions in %s (non-fatal)",
                        rel, exc_info=True,
                    )
                    failed.append(rel)
                    continue
                status = out["status"]
                if status == "retracted":
                    logger.info(
                        "forget: retracted %d mention(s) in %s — coverage "
                        "%.3f below floor %.3f, whole-file archive withheld",
                        out["mentions"], rel, coverage, cfg.min_target_coverage,
                    )
                    entry = {"path": rel, "mentions": out["mentions"]}
                    if out.get("index_error"):
                        entry["index_error"] = out["index_error"]
                    retracted.append(entry)
                elif status == "already_retracted":
                    # Belt to resolution's retracted_prefs filter; reaching
                    # here is the filter racing a concurrent retraction.
                    skipped.append({"path": rel, "status": status})
                else:
                    # "no_mentions" (resolution matched on a surface striking
                    # cannot reach — heading, fence, frontmatter prefix) or
                    # "replace_doc" (living document; strikes would be
                    # silently erased by its next replace-save). Either way
                    # the pref survives in this file UNFORGOTTEN — say so
                    # loudly rather than archiving the whole file.
                    logger.warning(
                        "forget: pref remains unforgotten in %s (%s) — "
                        "coverage %.3f below floor %.3f and mention-level "
                        "retraction could not apply",
                        rel, status, coverage, cfg.min_target_coverage,
                    )
                    skipped.append({
                        "path": rel,
                        "coverage": round(coverage, 3),
                        "status": "unforgotten",
                        "reason": status,
                    })
                continue
        try:
            out = archive_memory(
                rel,
                reason=f'forget request: "{pref}"',
                superseded_by=request_rel,
            )
            if out.get("status") in ("archived", "already_archived"):
                archived.append(rel)
        except Exception:
            # Same breadth as the retract branch: git, sqlite, or the status
            # push failing is this target's failure, not the save's.
            logger.warning(
                "forget: failed to archive %s (non-fatal)", rel, exc_info=True
            )
            failed.append(rel)

    logger.info(
        "forget request detected in %s: pref=%r archived=%d retracted=%d "
        "skipped=%d",
        request_rel, pref, len(archived), len(retracted), len(skipped),
    )
    result: dict[str, Any] = {
        "detected": True,
        "pref": pref,
        "archived": archived,
    }
    if boundary is not None:
        result["resolved_before"] = boundary.isoformat()
    if prior:
        result["prior_requests"] = [
            os.path.relpath(os.path.realpath(p), base) for p in prior
        ]
    if retracted:
        result["retracted"] = retracted
    if skipped:
        result["skipped"] = skipped
    if failed:
        result["failed"] = failed
    return result


class NotAForgetRequest(ValueError):
    """The named memory carries no explicit first-person forget request."""


def _iter_memory_files(root: str):
    for path in glob.glob(os.path.join(root, "**", "*.md"), recursive=True):
        yield path


def withdraw_forget_request(
    file_path: str, reason: str | None = None
) -> dict[str, Any]:
    """Take a forget request back: undo what it did and retire its record.

    ``file_path`` names the request memory. Its pref is re-detected from the
    body, then every memory the pref's forget retired is reversed at the
    granularity it was retired at — ``status: archived`` files whose
    ``superseded_by`` points at this request (or at any other live request
    record for the same pref) are restored via
    :func:`palinode.consolidation.archive.restore_memory`, and files whose
    ``retracted_prefs`` carries the pref are un-struck via
    :func:`palinode.consolidation.retract.unretract_mentions`. Finally the
    request records themselves — this one and the live same-pref records —
    are archived, so they stop acting as tombstones and stop bounding later
    requests. This is a composition, not a new primitive: each step is the
    on-demand op with its own audit line and commit, and a step that fails
    is reported in ``failed`` rather than aborting the rest.

    Not the same as restoring one memory: a request may have retired several
    memories at two granularities, and the request record must stop
    standing as the visible retraction — restoring a target while the record
    stays live leaves the store saying two things at once.

    Raises:
        ValueError: the path is malformed or escapes ``memory_dir``;
            :class:`NotAForgetRequest` when the body has no request.
        FileNotFoundError: no such memory file.
    """
    from palinode.consolidation.archive import (
        ARCHIVED_STATUS,
        archive_memory,
        resolve_memory_ref,
        restore_memory,
    )
    from palinode.consolidation.retract import normalize_pref, unretract_mentions

    rel, abs_path = resolve_memory_ref(file_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(rel)
    pref = detect_forget_request(_file_body(abs_path))
    if pref is None:
        raise NotAForgetRequest(rel)
    norm = normalize_pref(pref)
    base = os.path.realpath(config.memory_dir)

    def _rel(p: str) -> str:
        return os.path.relpath(os.path.realpath(p), base)

    record_rels = [rel] + [
        _rel(h["file_path"])
        for h in find_forget_requests(pref, exclude_paths={abs_path})
    ]
    record_set = set(record_rels)
    why = reason or f'forget request withdrawn: "{pref}"'

    restored: list[str] = []
    unretracted: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for path in _iter_memory_files(config.memory_dir):
        target_rel = _rel(path)
        if target_rel in record_set:
            continue
        meta = _frontmatter_of(path)
        if not meta:
            continue
        if (meta.get("status") == ARCHIVED_STATUS
                and str(meta.get("superseded_by") or "") in record_set):
            try:
                out = restore_memory(target_rel, reason=why)
                if out["status"] == "active":
                    restored.append(target_rel)
            except Exception:
                logger.warning("withdraw: failed to restore %s (non-fatal)",
                               target_rel, exc_info=True)
                failed.append({"path": target_rel, "op": "restore"})
        recorded = meta.get("retracted_prefs")
        if isinstance(recorded, list) and norm in recorded:
            try:
                out = unretract_mentions(target_rel, pref, reason=why)
                if out["status"] == "unretracted":
                    unretracted.append(
                        {"path": target_rel, "mentions": out["mentions"]}
                    )
            except Exception:
                logger.warning("withdraw: failed to unretract %s (non-fatal)",
                               target_rel, exc_info=True)
                failed.append({"path": target_rel, "op": "unretract"})

    records_archived: list[str] = []
    for record_rel in record_rels:
        try:
            out = archive_memory(record_rel, reason=why)
            if out["status"] in ("archived", "already_archived"):
                records_archived.append(record_rel)
        except Exception:
            logger.warning("withdraw: failed to archive request record %s "
                           "(non-fatal)", record_rel, exc_info=True)
            failed.append({"path": record_rel, "op": "archive_record"})

    logger.info(
        "forget request withdrawn: %s pref=%r restored=%d unretracted=%d "
        "records=%d", rel, pref, len(restored), len(unretracted),
        len(records_archived),
    )
    result: dict[str, Any] = {
        "file": rel,
        "status": "withdrawn",
        "pref": pref,
        "reason": reason,
        "restored": restored,
        "unretracted": unretracted,
        "requests_archived": records_archived,
    }
    if failed:
        result["failed"] = failed
    return result
