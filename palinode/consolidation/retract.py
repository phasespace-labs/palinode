"""On-demand mention-level RETRACT for one named memory.

The archive op (:mod:`palinode.consolidation.archive`) retires a *file*; this
module retires a *strand inside* a file. The mismatch it exists for:
forgetting is fact/entity-shaped, archival is file-shaped, and on a
consolidated store a person-directed forget request resolves to dense shared
memories where the forgotten preference is a few mentions among hundreds of
content words. Archiving those whole removes unrelated recall; skipping them
leaves the mentions live. Striking exactly the mentioning sentences does
neither.

The treatment is the executor's RETRACT shape
(:func:`palinode.consolidation.executor._retract_fact`) generalized to spans
that carry no ``<!-- fact:… -->`` anchor: each matched sentence or list item
becomes ``~~text~~ [RETRACTED <date> r:<id>].`` in place, with everything
around it untouched. The marker is deliberately OPAQUE — it never repeats the
pref (marker text feeding back into coverage/matching is how a re-run flips a
protected file into a whole-file archive) — and TERMINATOR-FINAL, so the
sentence splitter still finds the boundary to the next sentence on later
passes. The pref itself lives in the history-sibling entry tagged with the
same ``r:<id>``. Struck text stays in the file and in the index — the
validated forgetting shape is a *visible* retraction (silent full removal
measured worse than doing nothing), and the marker is what makes a wrong
strike reviewable and reversible by hand.

IDEMPOTENCE IS TRACKED STATE, NOT MARKUP SNIFFING. Each applied retraction
records its normalized pref in the file's ``retracted_prefs`` frontmatter
list; a repeat request for the same pref returns ``already_retracted``
without touching the file, and resolution
(:func:`palinode.consolidation.forget.resolve_forget_targets`) uses the same
record to keep the file from consuming target slots on later same-pref
requests. Only a span already carrying a retraction *marker* is refused a
second wrap — user-authored ``~~strikethrough~~`` in a sentence does not make
it immune.

MATCHING IS DETERMINISTIC, sentence-granular, and biased the same way the
detector is: precision over recall, because the retained forget-request
memory covers what matching misses. A sentence is struck when it contains an
*entity token* from the pref phrase (a Titlecase content word after the
phrase's first word — one hit suffices, names are high-signal; ALL-CAPS
emphasis does not qualify) or, for common-word prefs, when it shares at
least two content words with the phrase. Sentences are assembled across soft
line breaks inside a paragraph, so hard-wrapped prose matches the same as
flowing prose. Headings, code fences (backtick and tilde), table rows, and
the ``<!-- palinode-auto-footer -->`` block are never struck: what cannot be
struck cleanly is left for the caller to escalate loudly rather than mangled
in place. Living documents (``update_policy: replace``) are refused
entirely — their next replace-save would silently regenerate the body and
erase every marker (ADR-015 §2.2's replace-guard, same rationale as the
executor's).

The write follows the archive module's mutation contract: path guard via
:func:`palinode.consolidation.archive.resolve_memory_ref`, audit entry via
the executor's history writer, and one commit staging exactly the files
touched. The commit lands BEFORE re-indexing so an index failure can never
leave an uncommitted body mutation on disk; the index outcome is surfaced in
the result (``indexed_vec`` / ``indexed_fts`` / ``index_error``) exactly as
the save path surfaces it, because ``index_file`` reports embedder outages
in its return value rather than raising.

UNRETRACT IS THE INVERSE. :func:`unretract_mentions` un-strikes every span
carrying this pref's ``r:<id>`` marker — the id is a stable function of the
normalized pref, so the pref names exactly its own markers and never another
pref's — and removes the pref from ``retracted_prefs``, which is the record
resolution consults. Both halves matter: the markers are what the reader
sees, the record is what keeps a later same-pref request from striking the
file again. A record with no surviving markers (a hand edit removed them) is
still cleared, so the file ends consistent either way. ``status`` is never
touched — un-striking a strand in an archived file does not unarchive it;
that is :func:`palinode.consolidation.archive.restore_memory`'s job.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import frontmatter

from palinode.consolidation.archive import resolve_memory_ref
from palinode.core import git_tools
from palinode.core.config import config
from palinode.core.hashing import stable_md5_hexdigest

logger = logging.getLogger("palinode.retract")

# Opaque, terminator-final retraction marker. `r:<id>` keys the history-
# sibling entry carrying the pref; the marker itself must never contain pref
# words (module docstring). The trailing period is load-bearing: it is the
# sentence boundary later passes split on.
_MARKER_RE = re.compile(r"\[RETRACTED \d{4}-\d{2}-\d{2} r:[0-9a-f]{8}\]\.")

# Sentence boundary; separators (including soft line breaks) are captured so
# a paragraph can be reassembled byte-identically around the struck spans.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(\s+)")
_LIST_ITEM_RE = re.compile(r"^(\s*[-*]\s+)(.*?)(\s*<!-- fact:[^>]+ -->)?\s*$")
_FENCE_RE = re.compile(r"^(?:`{3,}|~{3,})")
_WORD_RE = re.compile(r"[A-Za-z]+")

_AUTO_FOOTER_MARKER = "<!-- palinode-auto-footer -->"


def normalize_pref(pref: str) -> str:
    """Canonical spelling for the ``retracted_prefs`` frontmatter record."""
    return " ".join(pref.lower().split())


def retraction_id(pref: str) -> str:
    """Stable 8-hex id tying markers to their history entry, same pref → same id."""
    return stable_md5_hexdigest(normalize_pref(pref))[:8]


def _entity_tokens(pref: str, stopwords: frozenset[str]) -> set[str]:
    """Titlecase content words after the phrase's first word, lowercased.

    Mid-phrase Titlecase is the deterministic proper-noun signal: the
    extracted pref preserves the request's casing, and a name token alone is
    enough to mark a sentence as carrying the pref. The phrase's first word
    is excluded (sentence-initial capitalization is ambiguous), and ALL-CAPS
    emphasis ("I HATE …") does not qualify — only Titlecase does.
    """
    words = _WORD_RE.findall(pref)
    return {
        w.lower()
        for w in words[1:]
        if w[0].isupper() and w[1:].islower() and len(w) > 2
        and w.lower() not in stopwords
    }


def _span_matches(
    span: str,
    pref_words: set[str],
    entity_tokens: set[str],
    min_shared: int,
) -> bool:
    if _MARKER_RE.search(span):
        return False  # already carries a retraction marker — never double-wrap
    span_words = {w.lower() for w in _WORD_RE.findall(span)}
    if entity_tokens & span_words:
        return True
    return len(span_words & pref_words) >= min_shared


def _strike(text: str, marker: str) -> str:
    return f"~~{text}~~ {marker}"


def _strike_paragraph(
    paragraph: str,
    pref_words: set[str],
    entity_tokens: set[str],
    min_shared: int,
    marker: str,
) -> tuple[str, int]:
    """Strike matching sentences in one prose paragraph (soft breaks intact)."""
    parts = _SENTENCE_SPLIT_RE.split(paragraph)
    count = 0
    for i, part in enumerate(parts):
        if i % 2 == 1 or not part.strip():
            continue  # whitespace separator, or blank fragment
        if _span_matches(part.strip(), pref_words, entity_tokens, min_shared):
            lead = part[: len(part) - len(part.lstrip())]
            trail = part[len(part.rstrip()):]
            parts[i] = f"{lead}{_strike(part.strip(), marker)}{trail}"
            count += 1
    return "".join(parts), count


def _retract_in_body(
    body: str,
    pref_words: set[str],
    entity_tokens: set[str],
    min_shared: int,
    marker: str,
) -> tuple[str, int]:
    """Strike every matching sentence/list item in ``body``; return (new_body, count).

    The strikeable surface is prose paragraphs (assembled across soft line
    breaks) and list items. Headings, fenced code (backtick or tilde), table
    rows, blank lines, and everything from the auto-footer marker onward pass
    through untouched.
    """
    out: list[str] = []
    paragraph: list[str] = []
    count = 0
    in_fence = False
    in_footer = False

    def flush() -> None:
        nonlocal count
        if not paragraph:
            return
        struck, n = _strike_paragraph(
            "".join(paragraph), pref_words, entity_tokens, min_shared, marker
        )
        out.append(struck)
        count += n
        paragraph.clear()

    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if not in_footer and stripped.startswith(_AUTO_FOOTER_MARKER):
            in_footer = True
        if in_footer:
            flush()
            out.append(line)
            continue
        if _FENCE_RE.match(stripped):
            flush()
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not stripped or stripped.startswith("#") \
                or stripped.startswith("|"):
            flush()
            out.append(line)
            continue

        m = _LIST_ITEM_RE.match(line.rstrip("\n"))
        if m:
            flush()
            item_marker, item_text = m.group(1), m.group(2)
            fact_comment = m.group(3) or ""
            if item_text and _span_matches(
                item_text, pref_words, entity_tokens, min_shared
            ):
                newline = "\n" if line.endswith("\n") else ""
                out.append(
                    f"{item_marker}{_strike(item_text, marker)}"
                    f"{fact_comment}{newline}"
                )
                count += 1
            else:
                out.append(line)
            continue

        paragraph.append(line)
    flush()
    return "".join(out), count


def retract_mentions(
    file_path: str,
    pref: str,
    superseded_by: str | None = None,
) -> dict[str, Any]:
    """Strike every sentence/list item in one memory that carries ``pref``.

    Returns a summary dict keyed by ``status``:

    - ``"retracted"`` — spans were struck (``mentions`` > 0); the file was
      written, audited, committed, and re-indexed, with the index outcome in
      ``indexed_vec`` / ``indexed_fts`` / ``index_error``.
    - ``"already_retracted"`` — this pref is already in the file's
      ``retracted_prefs`` record; nothing touched.
    - ``"replace_doc"`` — the target is a living ``update_policy: replace``
      document; striking is refused (the next replace-save would silently
      erase the markers). Nothing touched.
    - ``"no_mentions"`` — no unstruck strikeable span matched. Nothing
      touched; the caller decides how loudly to escalate.

    The file's ``status`` frontmatter is never changed — the memory stays
    active, which is the point.

    Raises:
        ValueError: the path is malformed or escapes ``memory_dir``.
        FileNotFoundError: no such memory file.
    """
    from palinode.consolidation.executor import (
        _is_replace_policy,
        _utc_now,
        append_to_history,
    )
    from palinode.consolidation.forget import _STOPWORDS, _content_words

    rel, abs_path = resolve_memory_ref(file_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(rel)

    with open(abs_path, encoding="utf-8") as f:
        raw = f.read()
    post = frontmatter.loads(raw)

    norm = normalize_pref(pref)
    recorded = post.metadata.get("retracted_prefs") or []
    if isinstance(recorded, list) and norm in recorded:
        return {"file": rel, "status": "already_retracted", "mentions": 0}

    if _is_replace_policy(raw):
        return {"file": rel, "status": "replace_doc", "mentions": 0}

    pref_words = _content_words(pref)
    entities = _entity_tokens(pref, _STOPWORDS)
    min_shared = min(2, len(pref_words)) or 1
    rid = retraction_id(pref)
    marker = f"[RETRACTED {_utc_now().strftime('%Y-%m-%d')} r:{rid}]."

    new_body, mentions = _retract_in_body(
        post.content, pref_words, entities, min_shared, marker
    )
    if mentions == 0:
        return {"file": rel, "status": "no_mentions", "mentions": 0}

    post.content = new_body
    if isinstance(recorded, list):
        post["retracted_prefs"] = [*recorded, norm]
    else:
        post["retracted_prefs"] = [norm]
    git_tools.write_memory_file(abs_path, frontmatter.dumps(post) + "\n")

    audit_id = str(post.metadata.get("id") or "").strip() or (
        os.path.splitext(os.path.basename(rel))[0]
    )
    entry = f'Retracted {mentions} mention(s) [r:{rid}]: "{pref}"'
    if superseded_by:
        entry = f"{entry} — superseded by {superseded_by}"
    history_abs = append_to_history(abs_path, audit_id, entry)
    history_rel = os.path.relpath(history_abs, config.memory_dir)

    # Commit BEFORE re-indexing: provenance must not depend on the index
    # round-trip succeeding (an uncommitted body mutation is the failure mode
    # every-write-is-committed exists to prevent).
    message = f"{config.git.commit_prefix} retract: {rel} ({mentions} mentions)"
    if superseded_by:
        message = f"{message} <- {superseded_by}"
    committed = git_tools.commit_memory_files([abs_path, history_abs], message)

    # The body content-hash moved, so this is a real re-index, not the
    # frontmatter-only status push the archive op gets away with. index_file
    # reports failure in its return value (embedder outages never raise), so
    # surface its outcome the same way the save path does.
    from palinode.indexer.index_file import index_file

    outcome = index_file(abs_path)

    # Memories whose `backed_by` cites this one are flagged for review (one
    # hop, flag-only, its own commit). The reason is the opaque `r:<id>`, never
    # the pref text — the same discipline as the in-body marker, so a flag can
    # never feed the pref back into a later forget pass's matching.
    from palinode.consolidation.propagate import flag_dependents

    review_flagged = flag_dependents(
        abs_path, ops=["retract"], reason=f"retracted {mentions} mention(s) r:{rid}"
    )

    logger.info("Retracted %d mention(s) in %s [r:%s]", mentions, rel, rid)
    result: dict[str, Any] = {
        "file": rel,
        "status": "retracted",
        "mentions": mentions,
        "retraction_id": rid,
        "history_file": history_rel,
        "committed": committed,
        "indexed_vec": bool(outcome.get("indexed_vec", True)),
        "indexed_fts": bool(outcome.get("indexed_fts", True)),
        "review_flagged": review_flagged,
    }
    if outcome.get("error"):
        result["index_error"] = outcome["error"]
    return result


def _unstrike_re(rid: str) -> re.Pattern[str]:
    """Match one struck span carrying this retraction id, capturing the text."""
    return re.compile(
        r"~~(.+?)~~ \[RETRACTED \d{4}-\d{2}-\d{2} r:" + re.escape(rid) + r"\]\."
    )


def unretract_mentions(
    file_path: str,
    pref: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Withdraw one pref's retraction from one memory: un-strike its markers,
    clear its ``retracted_prefs`` record.

    Returns a summary dict keyed by ``status``:

    - ``"unretracted"`` — the record was cleared and ``mentions`` spans were
      un-struck (possibly 0 when a hand edit already removed the markers);
      the file was written, audited, committed and re-indexed, with the
      index outcome in ``indexed_vec`` / ``indexed_fts`` / ``index_error``.
    - ``"not_retracted"`` — this pref is not in the file's ``retracted_prefs``
      record; nothing touched. Markers are never sniffed as a substitute
      for the record (the same tracked-state rule as retraction).

    The file's ``status`` frontmatter is never changed.

    Raises:
        ValueError: the path is malformed or escapes ``memory_dir``.
        FileNotFoundError: no such memory file.
    """
    from palinode.consolidation.executor import append_to_history

    rel, abs_path = resolve_memory_ref(file_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(rel)

    with open(abs_path, encoding="utf-8") as f:
        post = frontmatter.load(f)

    norm = normalize_pref(pref)
    recorded = post.metadata.get("retracted_prefs") or []
    if not isinstance(recorded, list) or norm not in recorded:
        return {"file": rel, "status": "not_retracted", "mentions": 0}

    rid = retraction_id(pref)
    new_body, mentions = _unstrike_re(rid).subn(r"\1", post.content)

    post.content = new_body
    remaining = [p for p in recorded if p != norm]
    if remaining:
        post["retracted_prefs"] = remaining
    else:
        post.metadata.pop("retracted_prefs", None)
    git_tools.write_memory_file(abs_path, frontmatter.dumps(post) + "\n")

    audit_id = str(post.metadata.get("id") or "").strip() or (
        os.path.splitext(os.path.basename(rel))[0]
    )
    entry = f'Unretracted {mentions} mention(s) [r:{rid}]: "{pref}"'
    if reason:
        entry = f"{entry} (reason: {reason})"
    history_abs = append_to_history(abs_path, audit_id, entry)
    history_rel = os.path.relpath(history_abs, config.memory_dir)

    # Same contract as retract_mentions: commit before re-index.
    message = f"{config.git.commit_prefix} unretract: {rel} ({mentions} mentions)"
    committed = git_tools.commit_memory_files([abs_path, history_abs], message)

    from palinode.indexer.index_file import index_file

    outcome = index_file(abs_path)

    logger.info("Unretracted %d mention(s) in %s [r:%s]", mentions, rel, rid)
    result: dict[str, Any] = {
        "file": rel,
        "status": "unretracted",
        "mentions": mentions,
        "retraction_id": rid,
        "reason": reason,
        "history_file": history_rel,
        "committed": committed,
        "indexed_vec": bool(outcome.get("indexed_vec", True)),
        "indexed_fts": bool(outcome.get("indexed_fts", True)),
    }
    if outcome.get("error"):
        result["index_error"] = outcome["error"]
    return result


__all__ = [
    "normalize_pref",
    "retract_mentions",
    "retraction_id",
    "unretract_mentions",
]
