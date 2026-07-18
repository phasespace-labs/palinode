"""Claim-level source anchors (#508; public issue #65 Q1 — unsigned claim_id).

A ``claims:`` frontmatter entry binds one claim *inside* a memory to the
source span that justifies it, so ``blame`` can answer "which source span
justifies this claim," not just "when was this line written"::

    claims:
      - claim_id: 0f3a9b2c1d4e5f6a          # stable id, content-addressed
        text: "the claim as stated in the memory"
        source_id: research/some-paper.md    # a sources[].ref-style path
        span:
          quote: "the exact passage cited from the source"
          quote_hash: "<md5 of normalize_quote(quote)>"
        anchor_id: "optional-opaque-pointer"  # interop; carried verbatim

This layer COMPOSES with the existing identity/integrity layers — it
replaces neither:

- File/fact identity (``category/slug``) is unchanged. ``claim_id`` is a
  finer-grained, sub-fact pointer: new optional frontmatter, sticky,
  absence-is-neutral (mirrors how ``sources:`` and ``contradicts``/
  ``backed_by`` were added).
- ``span`` reuses the ``sources:`` quote anchor exactly
  (``quote_verify.normalize_quote`` / ``quote_hash``). quote_hash is the
  *integrity* half (does the cited text still hash-match); claim_id is the
  *addressing* half (a stable name for the claim→span binding).

Derivation is content-addressed and salted with the parent memory ref —
``sha256("<memory_ref>:<normalize_quote(text)>")`` truncated to 16 hex — so
identical claim text in two different memories gets two ids (no accidental
cross-memory aliasing) while identical claims within one memory dedup
naturally. The claim text is stored alongside the id so the opaque
``claim_id`` stays resolvable to the claim it names. Known trade-off:
content-addressed ids are not edit-stable (re-wording the claim mints a new
id) and renaming the memory file re-salts them.

Unsigned only: stable identifier + pointer. No envelope, no signing — the
attested form is a separate, gated concern.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from palinode.core.quote_verify import (
    QuoteStatus,
    normalize_quote,
    quote_hash,
    verify_quote,
)

#: Truncation length of the derived sha256 hex digest.
CLAIM_ID_HEX_LEN = 16

# Same ref discipline as the typed-link fields: a source_id is a path-relative
# ref under the memory dir. Reject traversal, absolute paths, and control
# characters so a malformed ref can never escape the memory dir when resolved.
_SOURCE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

_CLAIM_ID_RE = re.compile(r"^[0-9a-f]+$")


class ClaimError(ValueError):
    """Raised when a claims list is malformed (caller maps to HTTP 400)."""


def derive_claim_id(memory_ref: str, text: str) -> str:
    """Derive the content-addressed id for a claim inside a memory.

    ``memory_ref`` is the memory's path-relative identity (e.g.
    ``insights/foo.md``); ``text`` is the claim as stated in the memory. The
    text is canonicalized with the same ``normalize_quote`` used by the quote
    anchors, so cosmetic differences (smart punctuation, whitespace runs)
    do not mint a new id.
    """
    normalized = normalize_quote(text)
    digest = hashlib.sha256(f"{memory_ref}:{normalized}".encode("utf-8")).hexdigest()
    return digest[:CLAIM_ID_HEX_LEN]


def _validate_source_ref(ref: Any, label: str) -> str:
    if not isinstance(ref, str) or not ref.strip():
        raise ClaimError(f"{label} missing non-empty 'source_id'")
    r = ref.strip()
    if ".." in r or r.startswith("/") or "\n" in r or not _SOURCE_REF_RE.match(r):
        raise ClaimError(f"{label} 'source_id' is not a well-formed ref: {ref!r}")
    return r


def normalize_claims(raw: Any, memory_ref: str) -> list[dict[str, Any]]:
    """Validate and normalize a ``claims`` list for save.

    Each entry must be a dict with non-empty ``text``, a well-formed
    ``source_id`` ref, and a ``span`` object with a non-empty ``quote``. The
    span's ``quote_hash`` is computed when absent and validated when present
    (mirroring the ``sources:`` anchors); ``claim_id`` is derived when absent
    and, when supplied, must match the derived value — the id is
    content-addressed, so a non-matching supplied id is an inconsistent
    anchor, not an alternative name. ``anchor_id`` is carried verbatim when
    present (interop pointer; nullable).

    Exact duplicate bindings (same claim_id, source_id, quote_hash) are
    dropped; the same claim backed by different spans is kept. Raises
    :class:`ClaimError` on any malformed input — the save surface wraps that
    as HTTP 400.
    """
    if not isinstance(raw, list):
        raise ClaimError(f"claims must be a list (got {type(raw).__name__})")

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for i, entry in enumerate(raw):
        label = f"claims[{i}]"
        if not isinstance(entry, dict):
            raise ClaimError(f"{label} must be an object")

        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ClaimError(f"{label} missing non-empty 'text' (the claim itself)")

        source_id = _validate_source_ref(entry.get("source_id"), label)

        span = entry.get("span")
        if not isinstance(span, dict):
            raise ClaimError(f"{label} missing 'span' object ({{quote, quote_hash}})")
        quote = span.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise ClaimError(f"{label} span missing non-empty 'quote'")
        computed_hash = quote_hash(quote)
        supplied_hash = span.get("quote_hash")
        if supplied_hash is not None and str(supplied_hash).strip():
            if str(supplied_hash).strip() != computed_hash:
                raise ClaimError(
                    f"{label} span quote_hash does not match its quote "
                    "(inconsistent anchor)"
                )

        derived = derive_claim_id(memory_ref, text)
        supplied_id = entry.get("claim_id")
        if supplied_id is not None and str(supplied_id).strip():
            sid = str(supplied_id).strip().lower()
            if not _CLAIM_ID_RE.match(sid) or sid != derived:
                raise ClaimError(
                    f"{label} claim_id does not match the content-addressed "
                    f"derivation for this memory + text (expected {derived})"
                )

        out: dict[str, Any] = {
            "claim_id": derived,
            "text": text,
            "source_id": source_id,
            "span": {"quote": quote, "quote_hash": computed_hash},
        }
        anchor_id = entry.get("anchor_id")
        if anchor_id is not None:
            if not isinstance(anchor_id, str) or not anchor_id.strip():
                raise ClaimError(f"{label} 'anchor_id' must be a non-empty string when present")
            out["anchor_id"] = anchor_id.strip()

        key = (derived, source_id, computed_hash)
        if key not in seen:
            seen.add(key)
            normalized.append(out)
    return normalized


def parse_claims(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Soft-fail accessor for reading ``claims`` from parsed frontmatter.

    Consistent with the parser's soft-fail style (``parse_sources`` /
    ``parse_link_refs``): malformed entries are dropped, a missing/non-list
    field returns ``[]`` so a file with no claims round-trips as a clean
    no-op. Validation belongs at the save surface; reads never raise.
    """
    raw = metadata.get("claims")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        source_id = entry.get("source_id")
        span = entry.get("span")
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(source_id, str) or not source_id.strip():
            continue
        if not isinstance(span, dict) or not str(span.get("quote", "")).strip():
            continue
        parsed: dict[str, Any] = {
            "claim_id": str(entry.get("claim_id", "")).strip(),
            "text": text,
            "source_id": source_id.strip(),
            "span": {
                "quote": str(span.get("quote", "")),
                "quote_hash": str(span.get("quote_hash", "")).strip(),
            },
        }
        anchor_id = entry.get("anchor_id")
        if isinstance(anchor_id, str) and anchor_id.strip():
            parsed["anchor_id"] = anchor_id.strip()
        out.append(parsed)
    return out


def resolve_memory_claims(file_path: str, memory_dir: str) -> list[dict[str, Any]]:
    """Resolve every ``claims:`` entry in one memory file to its source span.

    For each claim, re-checks the three live properties an auditor cares
    about:

    - ``span_status`` — the quote-anchor integrity check, reusing the
      ``sources:`` verifier semantics: ``ok`` / ``anchor_tampered`` /
      ``source_drifted`` / ``source_missing``.
    - ``claim_id_status`` — ``ok`` when the stored id equals the
      content-addressed derivation for this memory + text, else ``mismatch``
      (the binding was edited, or the file was renamed out from under it).
    - ``source_declared`` — whether ``source_id`` also appears among the
      memory's ``sources[].ref`` anchors (advisory: a claim normally cites a
      source the memory already tracks).

    Returns ``[]`` when the file carries no claims — a clean no-op on
    memories that never opted in.
    """
    from palinode.core.parser import parse_markdown

    full_path = file_path if os.path.isabs(file_path) else os.path.join(memory_dir, file_path)
    with open(full_path, encoding="utf-8") as f:
        metadata, _ = parse_markdown(f.read())

    claims = parse_claims(metadata)
    if not claims:
        return []

    memory_ref = os.path.relpath(os.path.realpath(full_path), os.path.realpath(memory_dir))
    declared_refs = {
        str(entry.get("ref", "")).strip()
        for entry in metadata.get("sources") or []
        if isinstance(entry, dict)
    }

    resolved: list[dict[str, Any]] = []
    for claim in claims:
        source_id = claim["source_id"]
        quote = claim["span"]["quote"]
        expected_hash = claim["span"]["quote_hash"]

        src_path = os.path.join(memory_dir, source_id)
        if not os.path.exists(src_path):
            span_status = QuoteStatus.SOURCE_MISSING.value
            span_detail = f"cited source not found: {source_id}"
        else:
            with open(src_path, encoding="utf-8") as sf:
                result = verify_quote(quote, expected_hash, sf.read(), source_id)
            span_status = result.status.value
            span_detail = result.message

        stored_id = claim.get("claim_id", "")
        derived = derive_claim_id(memory_ref, claim["text"])
        claim_id_status = "ok" if stored_id == derived else "mismatch"

        entry: dict[str, Any] = {
            **claim,
            "span_status": span_status,
            "span_detail": span_detail,
            "claim_id_status": claim_id_status,
            "source_declared": source_id in declared_refs,
        }
        resolved.append(entry)
    return resolved


def format_claims_resolution(file_path: str, resolutions: list[dict[str, Any]]) -> str:
    """Render a claims resolution as text (shared by the CLI and MCP surfaces)."""
    lines = [f"## Claims: {file_path}"]
    if not resolutions:
        lines.append("No claims recorded in this memory.")
        return "\n".join(lines)
    for r in resolutions:
        flags = []
        if r.get("claim_id_status") != "ok":
            flags.append("claim_id mismatch")
        if not r.get("source_declared", False):
            flags.append("source not in sources[]")
        flag_note = f" ({'; '.join(flags)})" if flags else ""
        lines.append(f"[{r.get('span_status', '?')}] {r.get('claim_id', '?')}{flag_note}")
        lines.append(f"  claim:  {r.get('text', '')}")
        lines.append(f"  source: {r.get('source_id', '')} :: \"{r['span'].get('quote', '')}\"")
        if r.get("span_detail"):
            lines.append(f"  detail: {r['span_detail']}")
    return "\n".join(lines)
